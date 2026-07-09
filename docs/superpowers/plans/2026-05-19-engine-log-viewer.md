# 运行日志（Engine Log Viewer）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增前端"运行日志"页，实时查看引擎活动；其中 Step 3 对每个在挂买单输出"检查输入 + 判定 + 处理结果"的详细日志。

**Architecture:** 内存环形缓冲 `logging.Handler`（挂 root logger，自动捕获所有引擎日志）+ `/api/logs`、`/api/logs/clear` 路由 + `/logs` 页面轮询渲染；`engine/monitor.py` Step 3 每单一条详细 INFO 日志。

**Tech Stack:** Python `logging` / `collections.deque` / Flask / pytest（`caplog`）。

参考 spec：`docs/superpowers/specs/2026-05-19-engine-log-viewer-design.md`

---

### Task 1: 内存缓冲 Handler + 常量

**Files:**
- Modify: `config.py`
- Create: `utils/log_buffer.py`
- Test: `tests/test_log_buffer.py`

- [ ] **Step 1: Add the constant to `config.py`**

`config.py` ends with:

```python
DB_PATH = "market_maker.db"
HOST = "127.0.0.1"
PORT = 5000
SECRET_KEY = None  # Set at runtime from user password
```

Change to:

```python
DB_PATH = "market_maker.db"
HOST = "127.0.0.1"
PORT = 5000
SECRET_KEY = None  # Set at runtime from user password
LOG_BUFFER_SIZE = 1000  # max in-memory log entries for the 运行日志 page
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_log_buffer.py`:

```python
"""tests/test_log_buffer.py"""

import logging
import pytest
from config import LOG_BUFFER_SIZE
from utils.log_buffer import BufferLogHandler, get_logs, clear_logs


def _record(msg, level=logging.INFO, name="engine.test", args=()):
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=None,
    )


@pytest.fixture(autouse=True)
def _clean():
    clear_logs()
    yield
    clear_logs()


def test_emit_appends_entry_with_fields():
    h = BufferLogHandler()
    h.emit(_record("hello %s", args=("world",), level=logging.WARNING,
                    name="engine.monitor"))
    logs = get_logs()
    assert len(logs) == 1
    e = logs[0]
    assert e["message"] == "hello world"
    assert e["level"] == "WARNING"
    assert e["logger"] == "engine.monitor"
    assert isinstance(e["ts"], float)


def test_get_logs_is_time_order_oldest_first():
    h = BufferLogHandler()
    h.emit(_record("first"))
    h.emit(_record("second"))
    msgs = [e["message"] for e in get_logs()]
    assert msgs == ["first", "second"]


def test_ring_buffer_evicts_oldest_beyond_maxlen():
    h = BufferLogHandler()
    for i in range(LOG_BUFFER_SIZE + 5):
        h.emit(_record(f"m{i}"))
    logs = get_logs()
    assert len(logs) == LOG_BUFFER_SIZE
    assert logs[0]["message"] == "m5"          # m0..m4 evicted
    assert logs[-1]["message"] == f"m{LOG_BUFFER_SIZE + 4}"


def test_clear_logs_empties_buffer():
    h = BufferLogHandler()
    h.emit(_record("x"))
    clear_logs()
    assert get_logs() == []


def test_emit_never_raises_on_bad_record():
    h = BufferLogHandler()
    bad = _record("%s", args=())  # getMessage() will raise TypeError
    h.emit(bad)                   # must NOT raise
    assert get_logs() == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_log_buffer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'utils.log_buffer'`

- [ ] **Step 4: Create `utils/log_buffer.py`**

```python
# utils/log_buffer.py
"""In-memory ring buffer logging handler for the 运行日志 page."""

import logging
from collections import deque
from config import LOG_BUFFER_SIZE

_BUFFER: deque = deque(maxlen=LOG_BUFFER_SIZE)


class BufferLogHandler(logging.Handler):
    """Appends each log record to a bounded in-memory deque."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _BUFFER.append(
                {
                    "ts": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                }
            )
        except Exception:
            self.handleError(record)


def get_logs() -> list:
    """Snapshot of buffered log entries, oldest first."""
    return list(_BUFFER)


def clear_logs() -> None:
    _BUFFER.clear()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_log_buffer.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Run full suite**

Run: `python -m pytest -q`
Expected: all pass (prior count + 5 new)

- [ ] **Step 7: Commit**

```bash
git add config.py utils/log_buffer.py tests/test_log_buffer.py
git commit -m "feat: in-memory ring buffer log handler + LOG_BUFFER_SIZE"
```
Commit message MUST end with footer line: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

### Task 2: 在 `app.py` 注册 Handler

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add the import and handler**

`app.py` top is:

```python
"""app.py — Application entry point."""

import logging
import signal
import sys
import webbrowser
import threading
from models.database import Database
from engine.manager import EngineManager
from web.routes import app, init_app, init_manager, set_encryption_key
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

Change to (add the import line and the third handler):

```python
"""app.py — Application entry point."""

import logging
import signal
import sys
import webbrowser
import threading
from models.database import Database
from engine.manager import EngineManager
from web.routes import app, init_app, init_manager, set_encryption_key
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

- [ ] **Step 2: Verify the handler is attached to the root logger**

Run: `python -c "import logging, app; print([h.__class__.__name__ for h in logging.getLogger().handlers])"`
Expected output includes `BufferLogHandler` (e.g. `['StreamHandler', 'FileHandler', 'BufferLogHandler']`)

- [ ] **Step 3: Run full suite (no regressions)**

Run: `python -m pytest -q`
Expected: all pass (same count as end of Task 1)

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat: register BufferLogHandler on root logger"
```
Footer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

### Task 3: 路由 `/logs`、`/api/logs`、`/api/logs/clear` + 导航

**Files:**
- Modify: `web/routes.py`
- Modify: `web/templates/base.html`

- [ ] **Step 1: Add the import in `web/routes.py`**

Near the top of `web/routes.py` other imports exist (e.g. `import logging`). Add this import alongside them:

```python
from utils.log_buffer import get_logs, clear_logs
```

- [ ] **Step 2: Add the routes**

In `web/routes.py`, immediately AFTER the `history_page` function:

```python
@app.route("/history")
@login_required
def history_page():
    return render_template("history.html")
```

add:

```python
@app.route("/logs")
@login_required
def logs_page():
    return render_template("logs.html")


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

- [ ] **Step 3: Add the nav link in `web/templates/base.html`**

The nav block is:

```html
            <a href="{{ url_for('orders_page') }}" class="{% if request.endpoint == 'orders_page' %}active{% endif %}">订单管理</a>
            <a href="{{ url_for('history_page') }}" class="{% if request.endpoint == 'history_page' %}active{% endif %}">历史记录</a>
            <a href="{{ url_for('logout') }}">退出</a>
```

Change to (insert 运行日志 between 历史记录 and 退出):

```html
            <a href="{{ url_for('orders_page') }}" class="{% if request.endpoint == 'orders_page' %}active{% endif %}">订单管理</a>
            <a href="{{ url_for('history_page') }}" class="{% if request.endpoint == 'history_page' %}active{% endif %}">历史记录</a>
            <a href="{{ url_for('logs_page') }}" class="{% if request.endpoint == 'logs_page' %}active{% endif %}">运行日志</a>
            <a href="{{ url_for('logout') }}">退出</a>
```

- [ ] **Step 4: Verify import + route registration**

Run: `python -c "import web.routes as r; print(sorted({rule.rule for rule in r.app.url_map.iter_rules() if 'log' in rule.rule}))"`
Expected: `['/api/logs', '/api/logs/clear', '/logs']`

- [ ] **Step 5: Run full suite (sanity)**

Run: `python -m pytest -q`
Expected: all pass (same count as Task 2)

- [ ] **Step 6: Commit**

```bash
git add web/routes.py web/templates/base.html
git commit -m "feat: /logs page route + /api/logs(/clear) + nav link"
```
Footer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

### Task 4: `运行日志` 页面模板

**Files:**
- Create: `web/templates/logs.html`

- [ ] **Step 1: Create `web/templates/logs.html`**

Mirrors the existing template style (`{% extends "base.html" %}`, `{% block content %}` / `{% block scripts %}`, fetch + render, like `history.html`).

```html
{% extends "base.html" %}
{% block content %}
<h1>运行日志</h1>

<div class="filter-bar">
    <label>级别：</label>
    <select id="level-filter" onchange="renderLogs()">
        <option value="">全部</option>
        <option value="INFO">INFO</option>
        <option value="WARNING">WARNING</option>
        <option value="ERROR">ERROR</option>
    </select>
    <button class="btn btn-sm btn-danger" onclick="clearLogs()">清空</button>
    <label><input type="checkbox" id="autoscroll" checked> 自动滚动到底部</label>
</div>

<pre id="log-area" class="log-area"></pre>
{% endblock %}

{% block scripts %}
<script>
let logEntries = [];

function renderLogs() {
    const level = document.getElementById('level-filter').value;
    const area = document.getElementById('log-area');
    const rows = logEntries
        .filter(e => !level || e.level === level)
        .map(e => {
            const t = new Date(e.ts * 1000).toLocaleString('zh-CN');
            return `<span class="log-${e.level}">[${t}] [${e.level}] ${e.logger}: ${escapeHtml(e.message)}</span>`;
        });
    area.innerHTML = rows.join('\n');
    if (document.getElementById('autoscroll').checked) {
        area.scrollTop = area.scrollHeight;
    }
}

function escapeHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function refreshLogs() {
    fetch('/api/logs').then(r => r.json()).then(data => {
        logEntries = data || [];
        renderLogs();
    });
}

function clearLogs() {
    if (!confirm('确定清空运行日志缓冲？(market_maker.log 文件不受影响)')) return;
    fetch('/api/logs/clear', {method: 'POST'}).then(r => r.json()).then(() => {
        logEntries = [];
        renderLogs();
    });
}

refreshLogs();
setInterval(refreshLogs, 4000);
</script>
{% endblock %}
```

- [ ] **Step 2: Verify app still imports / template path resolvable**

Run: `python -c "import web.routes"`
Expected: exit 0, no output.

- [ ] **Step 3: Run full suite (sanity)**

Run: `python -m pytest -q`
Expected: all pass (same count as Task 3).

Note: page is JS/HTML; no automated UI test (consistent with existing `history.html`/`config.html` — they have none). The buffer logic is covered by `tests/test_log_buffer.py` and routes by Task 3's URL-map check.

- [ ] **Step 4: Commit**

```bash
git add web/templates/logs.html
git commit -m "feat: 运行日志 page template (poll, level filter, clear)"
```
Footer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

### Task 5: Step 3 逐单详细日志

**Files:**
- Modify: `engine/monitor.py` (`_check_compliance`)
- Test: `tests/test_monitor.py` (`TestCheckSellOrders` — add caplog assertions)

- [ ] **Step 1: Add failing caplog tests**

In `tests/test_monitor.py`, append these tests to `class TestCheckSellOrders` (they use pytest's `caplog`; `import logging` is already at the top of the file — verify and do not duplicate):

```python
    def test_log_replace_has_detail(self, caplog):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {"id": "o1", "side": "BUY", "asset_id": "tok1", "market": "cid1",
             "size_matched": "0", "price": "0.40", "original_size": "500",
             "neg_risk": False}
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.place_limit_buy.return_value = {"orderID": "o2"}
        with caplog.at_level(logging.INFO, logger="engine.monitor"), patch(
            "engine.monitor.needs_replace", return_value="replace"
        ), patch("engine.monitor.determine_order_price", return_value=0.48):
            monitor.check_sell_orders()
        text = caplog.text
        assert "[Step3]" in text
        assert "o1" in text and "cid1" in text
        assert "max_spread=3" in text
        assert "replace" in text

    def test_log_keep_has_detail(self, caplog):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {"id": "o1", "side": "BUY", "asset_id": "tok1", "market": "cid1",
             "size_matched": "0", "price": "0.48", "original_size": "500"}
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        with caplog.at_level(logging.INFO, logger="engine.monitor"), patch(
            "engine.monitor.needs_replace", return_value="keep"
        ), patch("engine.monitor.determine_order_price", return_value=0.48):
            monitor.check_sell_orders()
        assert "[Step3]" in caplog.text
        assert "keep" in caplog.text

    def test_log_cancel_has_detail(self, caplog):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {"id": "o1", "side": "BUY", "asset_id": "tok1", "market": "cid1",
             "size_matched": "0", "price": "0.40", "original_size": "500"}
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        with caplog.at_level(logging.INFO, logger="engine.monitor"), patch(
            "engine.monitor.needs_replace", return_value="cancel"
        ), patch("engine.monitor.determine_order_price", return_value=None):
            monitor.check_sell_orders()
        assert "[Step3]" in caplog.text
        assert "cancel" in caplog.text

    def test_log_skip_when_max_spread_unknown(self, caplog):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {"id": "o1", "side": "BUY", "asset_id": "tok1", "market": "cid1",
             "size_matched": "0", "price": "0.40", "original_size": "500"}
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{}]
        with caplog.at_level(logging.INFO, logger="engine.monitor"):
            monitor.check_sell_orders()
        assert "[Step3]" in caplog.text
        assert "rewards_max_spread" in caplog.text
        assert "跳过" in caplog.text

    def test_log_skip_when_empty_orderbook(self, caplog):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {"id": "o1", "side": "BUY", "asset_id": "tok1", "market": "cid1",
             "size_matched": "0", "price": "0.40", "original_size": "500"}
        ]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        with caplog.at_level(logging.INFO, logger="engine.monitor"):
            monitor.check_sell_orders()
        assert "[Step3]" in caplog.text
        assert "盘口为空" in caplog.text
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `python -m pytest tests/test_monitor.py::TestCheckSellOrders -v -k "log_"`
Expected: FAIL — `_check_compliance` does not yet emit `[Step3]` detail lines.

- [ ] **Step 3: Add the detailed logging to `_check_compliance`**

Current `_check_compliance` (after Task-3-of-the-prior-feature changes) reads roughly:

```python
    def _check_compliance(self, o: dict):
        token_id = o.get("asset_id", "")
        ob = self.api.get_orderbook(token_id)
        bids = sorted(ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
        if not bids or not asks:
            return
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        midpoint = (best_bid + best_ask) / 2
        tick = float(ob.get("tick_size", "0.01"))
        tick_str = ob.get("tick_size", "0.01")
        max_spread = self._market_max_spread(o.get("market", ""))
        if max_spread is None:
            return  # can't determine real max_spread: skip this tick, never mis-cancel
        rmin = midpoint - max_spread * tick
        rmax = midpoint + max_spread * tick
        try:
            want = determine_order_price(
                bids=bids,
                max_spread=max_spread,
                tick_size=tick,
                reward_range_min=rmin,
                reward_range_max=rmax,
            )
        except Exception as e:
            logger.warning("determine_order_price failed for %s: %s", o.get("id"), e)
            return
        action = needs_replace(float(o.get("price", 0)), want, tick)
        if action == "keep":
            return
        try:
            self.api.cancel_orders([o["id"]])
        except Exception as e:
            logger.warning("Cancel %s failed: %s", o.get("id"), e)
            return
        if action == "replace":
            size = int(float(o.get("original_size", 0) or 0))
            neg_risk = bool(o.get("neg_risk", False))
            self.api.place_limit_buy(
                token_id, want, size, tick_size=tick_str, neg_risk=neg_risk
            )
            logger.info("Replaced buy %s -> %.4f", o.get("id"), want)
        else:
            logger.info("Cancelled non-compliant buy %s (no valid price)", o.get("id"))
```

Make exactly these edits (read the live file first to match current text):

(3a) The empty-orderbook early return — replace:

```python
        if not bids or not asks:
            return
```

with:

```python
        if not bids or not asks:
            logger.info(
                "[Step3] 单 %s 市场 %s | 盘口为空，本轮跳过",
                o.get("id"), o.get("market", ""),
            )
            return
```

(3b) The unknown-max_spread early return — replace:

```python
        max_spread = self._market_max_spread(o.get("market", ""))
        if max_spread is None:
            return  # can't determine real max_spread: skip this tick, never mis-cancel
```

with:

```python
        max_spread = self._market_max_spread(o.get("market", ""))
        if max_spread is None:
            logger.info(
                "[Step3] 单 %s 市场 %s 现价 %.4f | 取不到 rewards_max_spread，"
                "本轮跳过（不撤不重挂）",
                o.get("id"), o.get("market", ""), float(o.get("price", 0) or 0),
            )
            return
```

(3c) Right AFTER the `action = needs_replace(...)` line and BEFORE `if action == "keep":`, insert the comprehensive per-order line:

```python
        action = needs_replace(float(o.get("price", 0)), want, tick)
        action_zh = {
            "keep": "keep → 保持不动",
            "replace": f"replace → 撤单并重挂 {want}",
            "cancel": "cancel → 撤单不重挂",
        }.get(action, action)
        logger.info(
            "[Step3] 单 %s 市场 %s 现价 %.4f | 盘口 bid %.4f ask %.4f mid %.4f "
            "tick %.4f | max_spread=%d 区间[%.4f,%.4f] | 应挂价 %s | 判定 %s",
            o.get("id"), o.get("market", ""), float(o.get("price", 0) or 0),
            best_bid, best_ask, midpoint, tick, max_spread, rmin, rmax,
            ("无" if want is None else f"{want:.4f}"), action_zh,
        )
        if action == "keep":
            return
```

Leave all other lines (cancel/replace execution, the existing `Replaced`/`Cancelled` logs) unchanged.

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python -m pytest tests/test_monitor.py::TestCheckSellOrders -v`
Expected: PASS (all TestCheckSellOrders tests, including the 5 new `log_*` ones)

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (no regressions; prior count + 5 new)

- [ ] **Step 6: Commit**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat: Step 3 per-order detailed compliance logging (inputs + decision)"
```
Footer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

## Self-Review

**Spec coverage:**
- 内存环形缓冲 handler + `LOG_BUFFER_SIZE` 常量 + get/clear → Task 1 ✓
- `app.py` 注册 handler（捕获所有引擎日志）→ Task 2 ✓
- `/logs`、`/api/logs`、`/api/logs/clear` 路由 + 导航项 → Task 3 ✓
- `运行日志` 页面（轮询、级别筛选、清空、自动滚动、简体中文）→ Task 4 ✓
- Step 3 逐单详细日志（replace/keep/cancel）+ 两个 skip 分支（取不到 max_spread / 空盘口）补日志 → Task 5 ✓
- 测试：`test_log_buffer.py`（emit/顺序/环形淘汰/clear/不抛异常）→ Task 1 ✓；`test_monitor.py` caplog 五分支 → Task 5 ✓
- 不改决策/挂单/Step1/Step2/strategy → 计划仅加日志与读路由，未触碰这些 ✓
- `market_maker.log` 文件保持原样 → 未改 FileHandler ✓

**Placeholder scan:** 无 TBD/TODO；每步含完整代码与精确命令、预期输出。pytest 计数以实际 `-q` 0-失败输出为准（未写死具体数字）。

**Type consistency:** `BufferLogHandler` / `get_logs()`（返回 list[dict]，键 `ts,level,logger,message`）/ `clear_logs()` 在 Task 1 定义，Task 2 导入 `BufferLogHandler`，Task 3 导入 `get_logs,clear_logs`，前端按相同键渲染（Task 4），一致。路由函数名 `logs_page` 在 Task 3 路由与 base.html `url_for('logs_page')` 一致。`engine.monitor` logger 名与 caplog `logger="engine.monitor"` 一致（模块用 `logging.getLogger(__name__)`，即 `engine.monitor`）。
