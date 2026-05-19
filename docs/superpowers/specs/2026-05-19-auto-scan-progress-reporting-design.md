# 自动扫描进度上报（共享带状态扫描）设计

日期：2026-05-19

## 背景与问题

仪表盘"扫描市场"按钮 → `scan_markets()`（`engine/manager.py`）会设 `scan_status="scanning"`、`scan_progress`、`scan_checked/scan_total`，并用 `scanner.scan(on_progress, on_found)` 回调实时更新；前端进度条据此显示并跨页续上（前序特性已就绪）。

但"全部启动"进入的自动扫描循环走 `_do_scan()`（`engine/manager.py:403`），它 `eligible = scanner.scan()` **不带回调、不设任何 scan_status/进度字段**，只在整轮后写 `eligible_markets`/`last_scan_time`。因此自动扫描期间前端永远看不到进度条（`/api/eligible` 的 `scan_status` 从不为 `"scanning"`）。这是手动/自动两条扫描路径行为不一致导致的体验缺口。

另有一个前端缺口：现有逻辑只在"页面加载"与"`last_scan_time` 变化的 10s 轮询触发 `refreshEligible`"时检查 `scan_status`。用户已停在仪表盘、自动扫描刚开始（此时 `last_scan_time` 尚未变）时，纯后端改动不足以让进度条自动出现。

## 决策（已与用户确认）

- 后端：抽一个共享"带状态扫描"私有方法，`scan_markets` 与 `_do_scan` 都用；各自独有部分（手动的 `_scanner_api` 引导、自动的扫完分发给 worker）保留在外层。
- 失败语义：扫描抛异常时该方法把 `scan_status` 复位为非 `"scanning"`（用 `"done"`），**不**更新 `last_scan_time`（延续"仅整轮成功完成才更新"），并 `raise`（上层 `_scanner_loop` 已有 try/except 记日志）。
- 前端：复用现有每 10s 的 `/api/eligible` 轮询，新增 `if (data.scan_status === 'scanning') ensureScanPolling();`，使已停在页面时自动扫描开始后最多 ~10s 出现进度条。

## 组件设计

### 后端 `engine/manager.py`

当前 `scan_markets()` 关键片段（节选）：
```python
        self.scan_status = "scanning"
        self.scan_progress = "Starting..."
        self.scan_checked = 0
        self.scan_total = 0
        self.eligible_markets = []

        def on_progress(checked, total, message):
            self.scan_checked = checked
            self.scan_total = total
            self.scan_progress = message

        def on_found(entry):
            self.eligible_markets.append(entry)

        scanner = MarketScanner(self._scanner_api, self.db, "")
        eligible = scanner.scan(on_progress=on_progress, on_found=on_found)
        self.eligible_markets = eligible
        self.last_scan_time = _time.time()
        self.scan_status = "done"
        self.scan_progress = f"Done: {len(eligible)} eligible"
        logger.info("Scanner found %d eligible markets", len(eligible))
```
（其前还有 `_scanner_api` 引导：无则用第一个启用钱包私钥建 `PolymarketAPI`。）

当前 `_do_scan()`：
```python
    def _do_scan(self):
        import time as _time
        scanner = MarketScanner(self._scanner_api, self.db, "")
        eligible = scanner.scan()
        self.eligible_markets = eligible
        self.last_scan_time = _time.time()
        logger.info("Scanner found %d eligible markets", len(eligible))
        for address, worker in self.engines.items():
            if worker.running:
                try:
                    worker.place_orders(eligible)
                except Exception as e:
                    logger.error("Error distributing to wallet %s: %s", address, e)
```

**新增私有方法**（建议置于 `scan_markets` 与 `_do_scan` 附近）：
```python
    def _scan_with_status(self) -> list:
        """Run one scan, reporting scan_status/progress; shared by manual and
        auto paths. On success sets eligible_markets/last_scan_time and
        scan_status='done' and returns the eligible list. On failure resets
        scan_status to a non-'scanning' value WITHOUT touching last_scan_time
        (a failed round did not complete), then re-raises."""
        import time as _time

        self.scan_status = "scanning"
        self.scan_progress = "Starting..."
        self.scan_checked = 0
        self.scan_total = 0
        self.eligible_markets = []

        def on_progress(checked, total, message):
            self.scan_checked = checked
            self.scan_total = total
            self.scan_progress = message

        def on_found(entry):
            self.eligible_markets.append(entry)

        try:
            scanner = MarketScanner(self._scanner_api, self.db, "")
            eligible = scanner.scan(on_progress=on_progress, on_found=on_found)
        except Exception:
            self.scan_status = "done"  # not 'scanning': progress bar won't stick
            raise
        self.eligible_markets = eligible
        self.last_scan_time = _time.time()
        self.scan_status = "done"
        self.scan_progress = f"Done: {len(eligible)} eligible"
        logger.info("Scanner found %d eligible markets", len(eligible))
        return eligible
```

**`scan_markets()`**：保留其独有的 `_scanner_api` 引导块（无 worker 时用第一个启用钱包建 api，含其错误日志/early return），其后**整段**"set scanning/progress/on_progress/on_found/scanner/eligible/last_scan_time/done/logger" 替换为：
```python
        self._scan_with_status()
```
（`scan_markets` 不需要返回值；其余原行为不变。）

**`_do_scan()`**：替换为：
```python
    def _do_scan(self):
        """Run one scan cycle: find eligible markets, distribute to wallets."""
        eligible = self._scan_with_status()
        for address, worker in self.engines.items():
            if worker.running:
                try:
                    worker.place_orders(eligible)
                except Exception as e:
                    logger.error("Error distributing to wallet %s: %s", address, e)
```
（删掉 `_do_scan` 里原 `import time as _time` / 自建 scanner / 自设 eligible_markets/last_scan_time/logger —— 均由 `_scan_with_status` 接管。分发循环保留不变。）

不改 `_scanner_loop`（其 `try/except` 包住 `_do_scan` 仍有效——`_scan_with_status` 失败会 raise 上来被它记日志）。

### 前端 `web/templates/dashboard.html`

文件底部现有 10s 轮询：
```js
let lastKnownScanTime = 0;
setInterval(() => {
    fetch('/api/eligible').then(r => r.json()).then(data => {
        if (data.last_scan_time && data.last_scan_time !== lastKnownScanTime) {
            lastKnownScanTime = data.last_scan_time;
            refreshEligible();
        }
    });
}, 10000);
```
改为（仅在 `.then` 内加一句，其余不动）：
```js
let lastKnownScanTime = 0;
setInterval(() => {
    fetch('/api/eligible').then(r => r.json()).then(data => {
        if (data.scan_status === 'scanning') ensureScanPolling();
        if (data.last_scan_time && data.last_scan_time !== lastKnownScanTime) {
            lastKnownScanTime = data.last_scan_time;
            refreshEligible();
        }
    });
}, 10000);
```
`ensureScanPolling()` 已存在且幂等（已在轮询则 return），它启动的 2s `pollScanProgress` 在扫描结束（非 scanning）时自行停。无需新增定时器或请求。

## 数据流

自动循环 `_scanner_loop` → `_do_scan()` → `_scan_with_status()`：置 `scan_status="scanning"` → scanner 回调实时刷 `scan_checked/scan_total/scan_progress` 与增量 `eligible_markets` → 成功后 `last_scan_time`/`scan_status="done"` → 返回 eligible 供 `_do_scan` 分发。`/api/eligible` 字段不变，前端 `pollScanProgress`/`refreshEligible`/10s 轮询据 `scan_status` 自动显示并续上进度条。手动 `scan_markets` 经同一 `_scan_with_status`，行为与之前一致。

## 错误处理

- `_scan_with_status` 内 scanner 抛异常 → `scan_status="done"`（非 "scanning"，前端隐藏进度条、`pollScanProgress` 停轮询）、`last_scan_time` 不变（"上次扫描"仍显示上一次成功时间）→ `raise`。
- 自动路径：`_scanner_loop` 现有 `try/except` 记 `logger.error`，循环继续。
- 手动路径：异常沿 `/api/engine/scan` 路由上抛（与现状一致；前端 `scanMarkets()` 本就不读响应）；关键是 `scan_status` 已复位，进度条不卡死。

## 测试

扩 `tests/test_manager.py`（用 `_make_manager()` + monkeypatch `engine.manager.MarketScanner`，无网络）：

- **手动**：`manager._scanner_api = MagicMock()`；`FakeScanner.scan(on_progress,on_found)` 内调 `on_progress(1,2,"x")` 并记录此刻 `manager.scan_status`，断言期间为 `"scanning"`；`scan_markets()` 返回后 `scan_status=="done"`、`last_scan_time>0`、`eligible_markets` 为返回列表。
- **自动**：构造一个 running 的 MagicMock worker 放入 `manager.engines`；`manager._scanner_api=MagicMock()`；patch `MarketScanner` 为返回 `[{...}]` 的 FakeScanner；调 `manager._do_scan()`；断言 `scan_status=="done"`、`last_scan_time>0`、该 worker `place_orders` 被以 eligible 调用一次。
- **失败**：`FakeScanner.scan` 抛 `RuntimeError`；调 `_scan_with_status()` 期望 `pytest.raises(RuntimeError)`；断言 `scan_status=="done"`（非 "scanning"）、`last_scan_time` 仍为调用前的值（未更新）。
- 现有 `tests/test_manager.py`（含 `TestScanMarketsLastScanTime` 等）保持通过；全套绿。
- 前端为纯 HTML/JS，沿用本仓库无 UI 测试惯例：靠 `python -c "import web.routes"` + `python -m pytest -q` 全绿 + 人工核对（点"全部启动"后约 ≤10s 出现进度条、扫描结束消失、"上次扫描"按整轮完成更新）。

## 不做（YAGNI）

- 不改 `engine/scanner.py` 或 `scanner.scan` 回调签名。
- 不改 `/api/eligible` 返回结构/字段或新增接口。
- 不新增前端定时器或新的 scan_status 取值（失败用既有 `"done"`，前端无需识别 "error"）。
- 不动监控/下单/止盈止损、引擎状态卡、`renderScanLine`、`last_scan_time` 仅整轮成功才更新的语义。
