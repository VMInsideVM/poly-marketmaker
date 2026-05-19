# 扫描时间语义 + 跨页续进度 + 可视化进度条 设计

日期：2026-05-19

## 背景与目标

三件相关的事：

1. **A — 上次扫描时间语义错误**：手动扫描 `scan_markets()` 的 `on_found(entry)` 回调里每找到一个市场就 `self.last_scan_time = _time.time()`（`engine/manager.py:277`），导致"上次扫描时间"在扫描过程中不断被刷新。期望：只在**整轮扫描完成**（初筛→列出全部符合市场→结束）那一刻更新一次。
2. **B — 跨页面跳转丢进度**：扫描进度显示是点"扫描市场"按钮才启动的纯前端 2 秒轮询（`scanMarkets()` 设 `scanPollTimer`）。本 app 是多页面整页跳转，切到别的页面再切回仪表盘时该前端状态销毁，回来不会根据后端 `scan_status` 重建——即使后端扫描仍在跑也看不到进度。
3. **C — 可视化进度条**：扫描中需要一个可视进度条（不只是文字）。

现状关键代码：

- `engine/manager.py` `scan_markets()`：`on_found` 内 `last_scan_time` 赋值（277）；`scanner.scan()` 整轮返回后 `last_scan_time`（282）+ `scan_status="done"`（283）。`_do_scan()`（自动扫描循环）只在 `scanner.scan()` 返回后设 `last_scan_time`（412），**已是整轮完成才更新，无需改**。
- `/api/eligible` 返回 `markets / last_scan_time / scan_status / scan_progress / scan_checked / scan_total`。
- `web/templates/dashboard.html`：`scanMarkets()` 设 `scanPollTimer=setInterval(pollScanProgress,2000)`；`pollScanProgress()` 轮询 `/api/eligible`，scanning 时写 `#scan-time` 文字，done 时清 `scanPollTimer`。`refreshEligible()` 仅据 `last_scan_time` 写 `#scan-time`（"上次扫描…"/"尚未扫描"）。`renderScanLine()` 当前含 `if (scanPollTimer) return;` 守卫（上一个特性为防进度文字与"上次扫描"文字争用 `#scan-time` 而加）。底部：load 时 `refreshDashboard()`+`refreshEligible()`；`setInterval(refreshDashboard,5000)`；`setInterval(renderScanLine,5000)`；及一个 10s「`last_scan_time` 变化才 `refreshEligible()`」轮询（用 `lastKnownScanTime`）。

## 决策（已与用户确认）

- A、B、C 同一轮一起做。
- 进度条形态：原生 `<progress>` + 旁边文字 `扫描中 [bar] {checked}/{total}，已找到 {N} 个`；仅扫描中显示，其他时候隐藏。
- `#scan-time`（"上次扫描"行）与新 `#scan-progress`（进度条）**分元素**；移除上一轮加的 `if (scanPollTimer) return;` 守卫（分元素后无争用，守卫反而妨碍扫描中持续刷新"上次扫描"行）。

## 组件设计

### A. 后端：`engine/manager.py` `scan_markets()`

删除 `on_found` 内的时间赋值。当前：
```python
        def on_found(entry):
            self.eligible_markets.append(entry)
            self.last_scan_time = _time.time()
```
改为：
```python
        def on_found(entry):
            self.eligible_markets.append(entry)
```
保留 `scanner.scan()` 返回后的 `self.last_scan_time = _time.time()`（282 行，紧邻 `scan_status="done"`）。`_do_scan()` 不改。

效果：`last_scan_time` 仅在整轮完成时更新；扫描进行中保持上一轮完成值。

### B. 前端：跨页续进度（`web/templates/dashboard.html`）

新增函数：
```js
function ensureScanPolling() {
    if (scanPollTimer) return;
    scanPollTimer = setInterval(pollScanProgress, 2000);
    pollScanProgress();
}
```
（`scanPollTimer` 沿用现有全局 `let scanPollTimer = null;`，不重复声明。）

`scanMarkets()` 改为复用它，去掉自带的 clearInterval+setInterval：当前
```js
function scanMarkets() {
    document.getElementById('scan-time').textContent = '正在扫描...';
    fetch('/api/engine/scan', {method: 'POST'});
    if (scanPollTimer) clearInterval(scanPollTimer);
    scanPollTimer = setInterval(pollScanProgress, 2000);
}
```
改为：
```js
function scanMarkets() {
    fetch('/api/engine/scan', {method: 'POST'});
    ensureScanPolling();
}
```
（删掉 `document.getElementById('scan-time').textContent = '正在扫描...';` —— 现在 `#scan-time` 由 `renderScanLine` 专管，进度走 `#scan-progress`。）

`refreshEligible()` 在 `.then(data => { ... })` 内，处理完 `lastScanTime/lastScanCount` 后追加：
```js
        if (data.scan_status === 'scanning') ensureScanPolling();
```
页面加载必跑一次 `refreshEligible()`，故切回仪表盘时若后端仍在扫即自动接回，由 `pollScanProgress` 驱动至 `done` 后自身清理 `scanPollTimer`（现有逻辑保留）。

### C. 前端：可视化进度条（`web/templates/dashboard.html`）

HTML——在 `<p id="scan-time">…</p>` 元素**正下方紧随**新增（`#scan-time` 元素本身保留原位不动）：
```html
<div id="scan-progress" style="display:none;"></div>
```

`pollScanProgress()` 重写其 DOM 输出：不再写 `#scan-time`，改为操作 `#scan-progress`：

```js
function pollScanProgress() {
    fetch('/api/eligible').then(r => r.json()).then(data => {
        const status = data.scan_status || 'idle';
        const checked = data.scan_checked || 0;
        const total = data.scan_total || 0;
        const markets = data.markets || [];
        const box = document.getElementById('scan-progress');

        if (status === 'scanning') {
            const bar = total > 0
                ? `<progress value="${checked}" max="${total}"></progress>`
                : `<progress></progress>`;
            const txt = total > 0
                ? `扫描中 ${bar} ${checked}/${total}，已找到 ${markets.length} 个`
                : `正在初筛… ${bar} 已找到 ${markets.length} 个`;
            box.innerHTML = txt;
            box.style.display = 'block';
        } else {
            box.style.display = 'none';
            box.innerHTML = '';
            if (status === 'done' && scanPollTimer) {
                clearInterval(scanPollTimer);
                scanPollTimer = null;
            }
        }

        eligibleData = markets;
        renderEligibleTable();
    });
}
```
要点：`done`（或非 scanning）→ 隐藏并清空进度条；`done` 时清 `scanPollTimer`（沿用原有"扫描结束停止轮询"语义）。`<progress>` 无 `value` 时浏览器原生渲染为不确定态，无需自定义 CSS/动画。`checked/total/markets.length` 均整数文本，无注入面。

移除 `renderScanLine()` 里的守卫——当前：
```js
function renderScanLine() {
    if (scanPollTimer) return;  // scan in progress: let pollScanProgress own #scan-time
    const el = document.getElementById('scan-time');
    ...
}
```
改为（删守卫那一行）：
```js
function renderScanLine() {
    const el = document.getElementById('scan-time');
    ...
}
```
分元素后 `pollScanProgress` 不再碰 `#scan-time`，无争用；移除守卫使扫描中 `#scan-time` 也持续显示"上次扫描：<上一轮完成时刻>（X 分钟前，共 N 个）"（配合 A，该值在扫描中稳定）。

## 数据流

后端 `scan_markets()` 整轮结束 → `last_scan_time` 更新一次 + `scan_status="done"`。
前端：`/api/eligible` 既有字段不变。仪表盘 load → `refreshEligible()` 检测 `scan_status==='scanning'` → `ensureScanPolling()` → `pollScanProgress()`（2s）渲染 `#scan-progress` 进度条直到 `done` 自清；`#scan-time` 始终由 `renderScanLine()`（5s ticker + refreshEligible + 10s 变化轮询）显示"上次扫描"。

## 错误处理

- `scan_total` 为 0：进度条用不确定态 `<progress>`，文字作"正在初筛…"。
- `data` 缺字段：`|| 0` / `|| []` 兜底；`#scan-progress` 默认 `display:none`。
- `pollScanProgress` 的 fetch 失败：`.then` 不执行，进度条维持上一帧；下个 2s tick 重试（与现状一致，不新增处理）。

## 测试

- 后端单元测试（扩 `tests/test_manager.py`）：用一个假的 `MarketScanner`（`scan()` 在返回前对每个 entry 调多次 `on_found`），断言 `on_found` 执行期间 `manager.last_scan_time` 不变，仅 `scan_markets()` 整体返回后 `last_scan_time` 被设为一个 > 调用前的值。用 monkeypatch/`unittest.mock` 替换 `MarketScanner` 与时间，避免任何网络。
- B/C 为纯前端 HTML/JS：本仓库前端模板（dashboard/history/config/logs）一致无自动化 UI 测试且未引入前端测试框架，保持一致——靠 `python -c "import web.routes"` 通过、`python -m pytest -q` 全绿（A 不破坏既有用例）、人工打开仪表盘核对（开始扫描→切到订单管理→切回，进度条续上；整轮结束后"上次扫描"才更新）。

## 不做（YAGNI）

- 不改 `_do_scan`/自动扫描循环（已正确）。
- 不改 `/api/eligible` 返回结构或后端进度字段。
- 不为进度条引入自定义 CSS / 动画（用原生 `<progress>`）。
- 不引入前端测试框架。
- 不动引擎状态卡、`lastKnownScanTime`、10s 变化轮询、监控/下单逻辑、总盈亏。
