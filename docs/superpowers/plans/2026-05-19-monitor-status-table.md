# 结构化"监控状态"页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用按订单的结构化表格"监控状态"页替换原始运行日志页，并移除旧日志缓冲设施。

**Architecture:** 监控每个 tick 旁路记录每个相关订单的 Step1/2/3 处理结果到线程安全的进程内快照存储（覆盖式，每钱包一份）；新 `/api/monitor-status` 返回合并快照；`/logs` 页改为轮询渲染表格。删除 `BufferLogHandler`/`/api/logs` 那套。旁路记录绝不改任何下单/撤单/止盈/止损决策。

**Tech Stack:** Python `threading.Lock` / Flask / pytest。

参考 spec：`docs/superpowers/specs/2026-05-19-monitor-status-table-design.md`

---

### Task 1: 快照存储 `engine/monitor_status.py`

**Files:**
- Create: `engine/monitor_status.py`
- Test: `tests/test_monitor_status.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_monitor_status.py`:

```python
"""tests/test_monitor_status.py"""

import pytest
from engine.monitor_status import set_snapshot, get_snapshot, clear_snapshot


@pytest.fixture(autouse=True)
def _clean():
    clear_snapshot()
    yield
    clear_snapshot()


def test_empty_returns_zero_updated_and_no_rows():
    assert get_snapshot() == {"updated": 0, "rows": []}


def test_set_then_get_roundtrip():
    set_snapshot("0xAB", [{"market": "m1", "stage": "Step3"}], 100.0)
    snap = get_snapshot()
    assert snap["updated"] == 100.0
    assert snap["rows"] == [{"market": "m1", "stage": "Step3"}]


def test_same_wallet_second_set_overwrites_not_appends():
    set_snapshot("0xAB", [{"market": "old"}], 100.0)
    set_snapshot("0xAB", [{"market": "new"}], 200.0)
    snap = get_snapshot()
    assert snap["rows"] == [{"market": "new"}]
    assert snap["updated"] == 200.0


def test_multi_wallet_merge_and_updated_is_max_ts():
    set_snapshot("0xAB", [{"market": "a"}], 100.0)
    set_snapshot("0xCD", [{"market": "b"}], 250.0)
    snap = get_snapshot()
    markets = sorted(r["market"] for r in snap["rows"])
    assert markets == ["a", "b"]
    assert len(snap["rows"]) == 2
    assert snap["updated"] == 250.0


def test_clear_snapshot_empties():
    set_snapshot("0xAB", [{"market": "a"}], 100.0)
    clear_snapshot()
    assert get_snapshot() == {"updated": 0, "rows": []}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_monitor_status.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.monitor_status'`

- [ ] **Step 3: Create `engine/monitor_status.py`**

```python
# engine/monitor_status.py
"""Thread-safe in-process snapshot of per-order monitor processing.

One snapshot per wallet, overwritten each monitor tick. The 监控状态
page reads the merged view; nothing here touches trading decisions.
"""

import threading

_LOCK = threading.Lock()
# wallet_address -> {"ts": float, "rows": list[dict]}
_SNAPSHOTS: dict = {}


def set_snapshot(wallet: str, rows: list, ts: float) -> None:
    """Overwrite this wallet's snapshot (latest tick wins)."""
    with _LOCK:
        _SNAPSHOTS[wallet] = {"ts": ts, "rows": list(rows)}


def get_snapshot() -> dict:
    """Merged view across wallets: {"updated": max ts or 0, "rows": [...]}."""
    with _LOCK:
        snaps = list(_SNAPSHOTS.values())
    rows: list = []
    updated = 0.0
    for s in sorted(snaps, key=lambda s: s["ts"]):
        rows.extend(s["rows"])
        updated = max(updated, s["ts"])
    return {"updated": updated if updated else 0, "rows": rows}


def clear_snapshot() -> None:
    with _LOCK:
        _SNAPSHOTS.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_monitor_status.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Run full suite**

Run: `python -m pytest -q`
Expected: all pass (prior count + 5)

- [ ] **Step 6: Commit**

```bash
git add engine/monitor_status.py tests/test_monitor_status.py
git commit -m "feat: thread-safe per-wallet monitor status snapshot store"
```
Commit message MUST end with footer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

### Task 2: 接口切换 + 移除旧日志设施

**Files:**
- Modify: `app.py`
- Modify: `web/routes.py`
- Modify: `web/templates/base.html`
- Delete: `utils/log_buffer.py`, `tests/test_log_buffer.py`

- [ ] **Step 1: Remove BufferLogHandler from `app.py`**

`app.py` currently has (import + handlers list):
```python
from config import DB_PATH, HOST, PORT
from utils.log_buffer import BufferLogHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("market_maker.log", encoding="utf-8"),
        BufferLogHandler(),
    ],
)
```
Change to (drop the import line and the `BufferLogHandler()` handler):
```python
from config import DB_PATH, HOST, PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("market_maker.log", encoding="utf-8"),
    ],
)
```

- [ ] **Step 2: Swap routes in `web/routes.py`**

(2a) Replace the import line `from utils.log_buffer import get_logs, clear_logs` with:
```python
from engine.monitor_status import get_snapshot
```

(2b) Replace this exact block:
```python
@app.route("/api/logs", methods=["GET"])
@login_required
def api_get_logs():
    return jsonify(get_logs())


@app.route("/api/logs/clear", methods=["POST"])
@login_required
def api_clear_logs():
    clear_logs()
    return jsonify({"ok": True})
```
with:
```python
@app.route("/api/monitor-status", methods=["GET"])
@login_required
def api_monitor_status():
    return jsonify(get_snapshot())
```
Leave the `@app.route("/logs") ... def logs_page(): return render_template("logs.html")` block exactly as is.

- [ ] **Step 3: Update nav label in `web/templates/base.html`**

Replace:
```html
            <a href="{{ url_for('logs_page') }}" class="{% if request.endpoint == 'logs_page' %}active{% endif %}">运行日志</a>
```
with:
```html
            <a href="{{ url_for('logs_page') }}" class="{% if request.endpoint == 'logs_page' %}active{% endif %}">监控状态</a>
```

- [ ] **Step 4: Delete the old files**

```bash
git rm utils/log_buffer.py tests/test_log_buffer.py
```

- [ ] **Step 5: Verify imports + route map**

Run: `python -c "import app, web.routes as r; print(sorted({x.rule for x in r.app.url_map.iter_rules() if 'log' in x.rule or 'monitor' in x.rule}))"`
Expected: contains `/api/monitor-status` and `/logs`; does NOT contain `/api/logs` or `/api/logs/clear`. (`/login`,`/logout` may appear — they contain "log".)

- [ ] **Step 6: Run full suite**

Run: `python -m pytest -q`
Expected: all pass; `tests/test_log_buffer.py` is gone so count drops by 5 from its Task-1-era peak. 0 failures, no `ModuleNotFoundError`.

- [ ] **Step 7: Commit**

```bash
git add app.py web/routes.py web/templates/base.html utils/log_buffer.py tests/test_log_buffer.py
git commit -m "refactor: replace /api/logs + BufferLogHandler with /api/monitor-status"
```
Footer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

### Task 3: 监控旁路记录 + tick 边界

**Files:**
- Modify: `engine/monitor.py`
- Modify: `engine/manager.py` (`WalletWorker._run`)
- Test: `tests/test_monitor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_monitor.py`. First ensure these imports exist at the top (add only if missing; do not duplicate):
```python
from engine.monitor_status import get_snapshot, clear_snapshot
```
Append this test class:

```python
class TestMonitorStatusSnapshot:
    @pytest.fixture(autouse=True)
    def _clean_snap(self):
        clear_snapshot()
        yield
        clear_snapshot()

    def _ob(self):
        return {
            "bids": [{"price": "0.48", "size": "1000"}],
            "asks": [{"price": "0.52", "size": "1000"}],
            "tick_size": "0.01",
        }

    def test_step3_keep_records_row(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {"id": "o1", "side": "BUY", "asset_id": "tok1", "market": "cid1",
             "size_matched": "0", "price": "0.48", "original_size": "500"}
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.begin_status_tick()
        with patch("engine.monitor.needs_replace", return_value="keep"), patch(
            "engine.monitor.determine_order_price", return_value=0.48
        ):
            monitor.check_sell_orders()
        monitor.publish_status()
        rows = get_snapshot()["rows"]
        r = next(x for x in rows if x.get("stage") == "Step3")
        assert r["wallet"] == "0xABC"
        assert "keep" in r["action"]
        assert "cid1" in r["market"]

    def test_step3_skip_empty_orderbook_records_row(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {"id": "o1", "side": "BUY", "asset_id": "tok1", "market": "cid1",
             "size_matched": "0", "price": "0.40", "original_size": "500"}
        ]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        monitor.begin_status_tick()
        monitor.check_sell_orders()
        monitor.publish_status()
        rows = get_snapshot()["rows"]
        assert any("盘口为空" in x.get("action", "") for x in rows)

    def test_sell_order_gets_a_row(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {"id": "s1", "side": "SELL", "asset_id": "tok1", "market": "cid1",
             "size_matched": "0", "price": "0.60", "original_size": "200"}
        ]
        monitor.begin_status_tick()
        monitor.check_sell_orders()
        monitor.publish_status()
        rows = get_snapshot()["rows"]
        r = next(x for x in rows if x["side"] == "卖出")
        assert r["stage"] == "止盈卖单"
        assert r["action"] == "挂单中"

    def test_step1_fill_records_row(self):
        monitor, api, db = _make_monitor()
        db.get_settings.return_value = {"cooldown_minutes": 20,
                                        "rewards_cache_ttl_sec": 600,
                                        "stop_loss_pct": 15.0}
        monitor.begin_status_tick()
        monitor._handle_fill(
            {"size": 120, "price": 0.60, "asset_id": "tok1",
             "market": "cid1", "order_id": "o9"},
            set(),
        )
        monitor.publish_status()
        rows = get_snapshot()["rows"]
        r = next(x for x in rows if x["stage"] == "Step1")
        assert "成交" in r["action"]

    def test_begin_tick_clears_previous_rows(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {"id": "s1", "side": "SELL", "asset_id": "t", "market": "c",
             "size_matched": "0", "price": "0.6", "original_size": "1"}
        ]
        monitor.begin_status_tick()
        monitor.check_sell_orders()
        monitor.publish_status()
        first = len(get_snapshot()["rows"])
        monitor.begin_status_tick()
        monitor.check_sell_orders()
        monitor.publish_status()
        assert len(get_snapshot()["rows"]) == first  # overwrite, not grow
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_monitor.py::TestMonitorStatusSnapshot -v`
Expected: FAIL — `AttributeError: 'OrderMonitor' object has no attribute 'begin_status_tick'`

- [ ] **Step 3: Add status plumbing to `OrderMonitor.__init__`**

`engine/monitor.py` — at the top, the imports include `import time`. Add after the existing `from engine.rewards import extract_max_spread` line:
```python
from engine import monitor_status
```
In `OrderMonitor.__init__`, after the line `self._max_spread_cache: dict = {}` add:
```python
        self._status_rows: list = []
        self._tick_ts: float = 0.0
```

- [ ] **Step 4: Add the status helper methods**

In `engine/monitor.py`, add these three methods to `OrderMonitor` (place them right after `__init__`, before `init_watermark`):
```python
    def begin_status_tick(self) -> None:
        self._status_rows = []
        self._tick_ts = time.time()

    def _status_add(self, **fields) -> None:
        try:
            row = {"ts": self._tick_ts, "wallet": self.wallet_address}
            row.update(fields)
            self._status_rows.append(row)
        except Exception as e:  # never break Step1/2/3
            logger.warning("status_add failed: %s", e)

    def publish_status(self) -> None:
        try:
            monitor_status.set_snapshot(
                self.wallet_address, self._status_rows, self._tick_ts
            )
        except Exception as e:
            logger.warning("publish_status failed: %s", e)
```

- [ ] **Step 5: Record Step 1 fill row**

In `_handle_fill`, after the `if order_id and order_id not in cancelled_orders:` block (i.e. at the very end of the method, after the cancel try/except), add:
```python
        self._status_add(
            market=market_id, side="买入", price=f"{price:.4f}",
            size=str(size), matched=str(size), stage="Step1",
            action="成交→挂止盈+撤余", detail=f"成交{size} 止盈卖{price:.4f}",
        )
```

- [ ] **Step 6: Record Step 2 stop-loss row**

In `_check_pos_sl`, after the final `logger.warning("Stop-loss executed: ...", ...)` call (end of method), add:
```python
        self._status_add(
            market=pos.get("conditionId", ""), side="卖出",
            price=f"{cur:.4f}", size=str(size), matched="-",
            stage="Step2", action="止损→市价平仓",
            detail=f"cur{cur:.4f}<avg{avg:.4f} 触发",
        )
```

- [ ] **Step 7: Record Step 3 rows (5 branches) + non-buy/partial rows**

(7a) In `check_sell_orders`, change the loop body. Current:
```python
        for o in open_orders:
            if o.get("side") != "BUY":
                continue
            if float(o.get("size_matched", 0) or 0) > 0:
                continue
            try:
                self._check_compliance(o)
            except Exception as e:
                logger.error("Compliance error on %s: %s", o.get("id"), e)
```
Replace with:
```python
        for o in open_orders:
            price_s = f"{float(o.get('price', 0) or 0):.4f}"
            size_s = str(o.get("original_size", ""))
            matched_s = str(o.get("size_matched", "0"))
            if o.get("side") != "BUY":
                self._status_add(
                    market=o.get("market", ""), side="卖出",
                    price=price_s, size=size_s, matched=matched_s,
                    stage="止盈卖单", action="挂单中", detail="",
                )
                continue
            if float(o.get("size_matched", 0) or 0) > 0:
                self._status_add(
                    market=o.get("market", ""), side="买入",
                    price=price_s, size=size_s, matched=matched_s,
                    stage="Step1", action="部分成交",
                    detail=f"已成交{matched_s}",
                )
                continue
            try:
                self._check_compliance(o)
            except Exception as e:
                logger.error("Compliance error on %s: %s", o.get("id"), e)
```

(7b) In `_check_compliance`, add a `_status_add` next to each existing `logger.*` decision point.

After the empty-orderbook `logger.info("[Step3] 单 %s 市场 %s | 盘口为空，本轮跳过", ...)` block, before its `return`, add:
```python
            self._status_add(
                market=o.get("market", ""), side="买入",
                price=f"{float(o.get('price', 0) or 0):.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3", action="跳过(盘口为空)", detail="盘口为空",
            )
```

After the max_spread-None `logger.info("[Step3] ... 取不到 rewards_max_spread ...", ...)` block, before its `return`, add:
```python
            self._status_add(
                market=o.get("market", ""), side="买入",
                price=f"{float(o.get('price', 0) or 0):.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3", action="跳过(取不到max_spread)",
                detail="取不到 rewards_max_spread",
            )
```

Immediately after the big comprehensive `logger.info("[Step3] 单 %s 市场 %s 现价 ...", ...)` call (and before `if action == "keep":`), add:
```python
        self._status_add(
            market=o.get("market", ""), side="买入",
            price=f"{float(o.get('price', 0) or 0):.4f}",
            size=str(o.get("original_size", "")),
            matched=str(o.get("size_matched", "0")),
            stage="Step3", action=action_zh,
            detail=(
                f"bid{best_bid:.4f} ask{best_ask:.4f} mid{midpoint:.4f} "
                f"ms{max_spread} 区间[{rmin:.4f},{rmax:.4f}] 应挂{want_str}"
            ),
        )
```
Do NOT add status rows inside the `determine_order_price` `except` branch (that path just `logger.warning`+`return`; leaving it without a row is acceptable — it is rare and already file-logged). Leave all cancel/replace/keep control flow unchanged.

- [ ] **Step 8: Add tick boundary to `WalletWorker._run` (`engine/manager.py`)**

Current `_run` loop body:
```python
        while not self._stop_event.is_set():
            self.monitor.check_buy_orders()
            self.monitor.check_stop_loss()
            self.monitor.check_sell_orders()
            self._stop_event.wait(timeout=check_interval)
```
Replace with:
```python
        while not self._stop_event.is_set():
            self.monitor.begin_status_tick()
            self.monitor.check_buy_orders()
            self.monitor.check_stop_loss()
            self.monitor.check_sell_orders()
            self.monitor.publish_status()
            self._stop_event.wait(timeout=check_interval)
```

- [ ] **Step 9: Run the new tests**

Run: `python -m pytest tests/test_monitor.py::TestMonitorStatusSnapshot -v`
Expected: PASS (5 passed)

- [ ] **Step 10: Run full suite (regression — existing Step1/2/3 tests must still pass)**

Run: `python -m pytest -q`
Expected: all pass (no regressions; the existing `TestCheckSellOrders`/`TestCheckBuyOrders`/`TestStopLoss` assertions are about cancel/place/sell calls which are unchanged by the added `_status_add` side-records).

- [ ] **Step 11: Commit**

```bash
git add engine/monitor.py engine/manager.py tests/test_monitor.py
git commit -m "feat: monitor records per-order Step1/2/3 status snapshot each tick"
```
Footer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

### Task 4: 结构化表格页（重写 `logs.html`）

**Files:**
- Modify: `web/templates/logs.html` (full rewrite)

- [ ] **Step 1: Rewrite `web/templates/logs.html`**

Replace the entire file content with:

```html
{% extends "base.html" %}
{% block content %}
<h1>监控状态</h1>

<div class="filter-bar">
    <span id="updated">最后更新：-</span>
    <button class="btn btn-sm" onclick="refreshStatus()">刷新</button>
</div>

<table class="data-table">
    <thead>
        <tr>
            <th>时间</th><th>钱包</th><th>市场</th><th>方向</th>
            <th>价格</th><th>数量</th><th>已成交</th>
            <th>阶段</th><th>判定/动作</th><th>详情/原因</th>
        </tr>
    </thead>
    <tbody id="status-body"></tbody>
</table>
{% endblock %}

{% block scripts %}
<script>
function escapeHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function shortWallet(w) {
    return w && w.length > 12 ? w.slice(0, 6) + '..' + w.slice(-4) : (w || '');
}

function actionClass(a) {
    a = a || '';
    if (/replace|cancel|止损|跳过/.test(a)) return 'loss';
    if (/成交/.test(a)) return 'profit';
    return '';
}

function refreshStatus() {
    fetch('/api/monitor-status').then(r => r.json()).then(data => {
        const upd = data.updated
            ? new Date(data.updated * 1000).toLocaleString('zh-CN') : '-';
        document.getElementById('updated').textContent = '最后更新：' + upd;
        const rows = data.rows || [];
        const body = document.getElementById('status-body');
        if (!rows.length) {
            body.innerHTML =
                '<tr><td colspan="10">暂无在挂订单或监控未运行</td></tr>';
            return;
        }
        body.innerHTML = rows.map(r => {
            const t = r.ts
                ? new Date(r.ts * 1000).toLocaleString('zh-CN') : '';
            return `<tr>
                <td>${escapeHtml(t)}</td>
                <td title="${escapeHtml(r.wallet)}">${escapeHtml(shortWallet(r.wallet))}</td>
                <td>${escapeHtml(r.market)}</td>
                <td>${escapeHtml(r.side)}</td>
                <td>${escapeHtml(r.price)}</td>
                <td>${escapeHtml(r.size)}</td>
                <td>${escapeHtml(r.matched)}</td>
                <td>${escapeHtml(r.stage)}</td>
                <td class="${actionClass(r.action)}">${escapeHtml(r.action)}</td>
                <td>${escapeHtml(r.detail)}</td>
            </tr>`;
        }).join('');
    });
}

refreshStatus();
setInterval(refreshStatus, 4000);
</script>
{% endblock %}
```

- [ ] **Step 2: Verify app imports / template path**

Run: `python -c "import web.routes"`
Expected: exit 0, no output.

- [ ] **Step 3: Run full suite (sanity)**

Run: `python -m pytest -q`
Expected: all pass (same count as end of Task 3).

Note: page is HTML/JS; no automated UI test (consistent with other templates). Data path covered by `tests/test_monitor_status.py` + `TestMonitorStatusSnapshot`.

- [ ] **Step 4: Commit**

```bash
git add web/templates/logs.html
git commit -m "feat: structured 监控状态 table page (replaces raw log view)"
```
Footer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

## Self-Review

**Spec coverage:**
- §1 共享存储 set/get/clear、覆盖、合并、updated=max → Task 1 ✓
- §2 旁路记录 begin/_status_add/publish、Step1/2/3 各点、非买/部分行 → Task 3 ✓
- §3 `_run` tick 边界 begin/publish → Task 3 Step 8 ✓
- §4 路由 `/api/monitor-status`、删 `/api/logs(/clear)`、改 import → Task 2 ✓
- §5 页面表格（列/缩写/配色/空态/转义/4s 轮询）→ Task 4 ✓
- §6 删 `utils/log_buffer.py`/`tests/test_log_buffer.py`、`app.py` 去 handler、base.html 标签 → Task 2 ✓
- 测试：`test_monitor_status.py` + `TestMonitorStatusSnapshot`，删 test_log_buffer → Task 1/2/3 ✓
- 不改决策、`market_maker.log`/现有 logger 保留 → 计划仅加旁路 `_status_add` 与删 BufferLogHandler，未动下单/撤单/止盈/止损与文件 handler ✓

**Placeholder scan:** 无 TBD/TODO；每步含完整代码与精确命令、预期输出。pytest 计数以 `-q` 实际 0-失败为准（未写死绝对数）。

**Type consistency:** `set_snapshot(wallet, rows, ts)` / `get_snapshot()->{"updated","rows"}` / `clear_snapshot()`（Task 1）被 Task 2 路由 `get_snapshot`、Task 3 `monitor_status.set_snapshot`、测试 `clear_snapshot` 一致使用。行 dict 键 `ts,wallet,market,side,price,size,matched,stage,action,detail` 在 Task 3 产生、Task 4 模板按同名键渲染、`TestMonitorStatusSnapshot` 按同名键断言，一致。`begin_status_tick`/`_status_add`/`publish_status` 在 Task 3 定义并在 `_run`（Task 3 Step 8）调用，名称一致。`logs_page`/`/logs` 保留不变。
