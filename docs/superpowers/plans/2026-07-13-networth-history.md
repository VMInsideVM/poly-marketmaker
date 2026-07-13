# 账户净值历史 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每个钱包 worker 启动时与运行中每天各记录一次净值（现金+持仓市值），提供 `/api/networth` 查询与「资产曲线」页面（SVG 折线 + 悬停 + 按日查询）。

**Architecture:** 纯函数（跨天判断/持仓估值）放 `engine/networth.py`；快照写入由 `WalletWorker._tick` 驱动（首 tick 必记、跨天补记，内存缓存日期零轮询）；数据落 SQLite 新表 `net_worth_history`；查询按本地日期聚合每天最后一条；前端手写 SVG，零新依赖。

**Tech Stack:** Python + Flask + SQLite + pytest；前端原生 JS/SVG（不引入图表库）。

**Spec:** `docs/superpowers/specs/2026-07-13-networth-history-design.md`

## Global Constraints

- UI 文案一律简体中文。
- 持仓估值只用于展示（`currentValue` 优先，回退 `size × curPrice`），绝不进任何交易决策路径。
- 快照失败只打 WARNING，不得阻断 `_tick` 的监控步骤。
- 不新增第三方依赖；图表手写 SVG。
- 含中文的前端文件必须由主会话直接 Write（subagent 会写出别字/BOM），写后跑 node --check 与 BOM 检查（Task 5 有具体命令）。
- 提交只 stage 本任务文件，不卷入无关改动。
- 每个 Task 结束时 `pytest -q` 全绿。

---

### Task 1: `engine/networth.py` 纯函数

**Files:**
- Create: `engine/networth.py`
- Test: `tests/test_networth.py`

**Interfaces:**
- Produces: `should_snapshot(last_date: str | None, today: str) -> bool`；`positions_value(positions: list[dict]) -> float`。Task 3 的 worker 与 Task 2 的测试直接 import 这两个函数。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_networth.py`：

```python
"""tests/test_networth.py — 账户净值:纯函数 + DB 读写(不触网)。"""

from engine.networth import should_snapshot, positions_value


class TestShouldSnapshot:
    def test_first_time_none_records(self):
        assert should_snapshot(None, "2026-07-13") is True

    def test_same_day_skips(self):
        assert should_snapshot("2026-07-13", "2026-07-13") is False

    def test_new_day_records(self):
        assert should_snapshot("2026-07-13", "2026-07-14") is True


class TestPositionsValue:
    def test_current_value_preferred(self):
        assert positions_value([{"size": 10, "curPrice": 0.5, "currentValue": 6.0}]) == 6.0

    def test_fallback_size_times_cur_price(self):
        assert positions_value([{"size": 10, "curPrice": 0.5}]) == 5.0

    def test_sums_multiple_and_skips_nonpositive(self):
        ps = [
            {"size": 10, "curPrice": 0.5},          # 5.0
            {"size": "20", "curPrice": "0.25"},     # 5.0 字符串安全转换
            {"size": 0, "curPrice": 0.9},           # 忽略
            {"size": -3, "curPrice": 0.9},          # 忽略
        ]
        assert positions_value(ps) == 10.0

    def test_bad_values_skipped_not_raise(self):
        ps = [{"size": "abc"}, {"size": 5, "curPrice": None}, {"size": 5, "currentValue": "x", "curPrice": 0.2}]
        # "abc" 跳过;curPrice None 按 0;currentValue 非法回退 size×curPrice=1.0
        assert positions_value(ps) == 1.0

    def test_empty_and_none(self):
        assert positions_value([]) == 0.0
        assert positions_value(None) == 0.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_networth.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'engine.networth'`）

- [ ] **Step 3: 实现**

创建 `engine/networth.py`：

```python
"""engine/networth.py — 账户净值快照纯函数(不触网)。"""


def should_snapshot(last_date, today):
    """跨天判断:上次快照日期(YYYY-MM-DD,None=本进程内还没记过)与今天不同 -> 该记。"""
    return last_date != today


def positions_value(positions):
    """Data API /positions 的持仓市值合计。

    currentValue 优先,缺失/非法回退 size×curPrice;size<=0 或非法的项忽略。
    仅供净值展示,不进任何交易决策(与「curPrice 禁用作成本」铁律不冲突——
    那条铁律限定的是离场成本口径)。
    """
    total = 0.0
    for p in positions or []:
        try:
            size = float(p.get("size", 0) or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        cv = p.get("currentValue")
        if cv is not None:
            try:
                total += float(cv)
                continue
            except (TypeError, ValueError):
                pass
        try:
            total += size * float(p.get("curPrice", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_networth.py -q`
Expected: PASS（10 项）

- [ ] **Step 5: Commit**

```bash
git add engine/networth.py tests/test_networth.py
git commit -m "feat(networth): 净值快照纯函数(跨天判断+持仓市值合计)"
```

---

### Task 2: DB 层 `net_worth_history` 表 + 读写方法

**Files:**
- Modify: `models/database.py`（`_create_tables` 的 executescript 末尾加表 + 新增两个方法）
- Test: `tests/test_networth.py`（追加）

**Interfaces:**
- Consumes: 无（独立于 Task 1）。
- Produces: `Database.record_net_worth(wallet: str, cash: float, positions_value: float) -> None`（total 由方法内相加）；`Database.get_net_worth_daily(wallet: str, days: int = 90) -> list[dict]`，返回 `[{"date": "YYYY-MM-DD", "cash": float, "positions_value": float, "total": float}]` 按日期升序、每天取最后一条。Task 3/4 依赖这两个方法。

- [ ] **Step 1: 写失败测试**

在 `tests/test_networth.py` 顶部补 import，末尾追加：

```python
import time
from datetime import datetime

import pytest

from models.database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "networth.db"))
    d.init()
    yield d
    d.close()


def _insert(db, wallet, cash, pos, ts):
    """直插带指定 created_at 的行(测试回填历史用;total=cash+pos 与方法口径一致)。"""
    db.conn.execute(
        "INSERT INTO net_worth_history (wallet, cash, positions_value, total, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (wallet, cash, pos, cash + pos, ts),
    )
    db.conn.commit()


def _day(ts):
    """与 SQLite date(created_at,'unixepoch','localtime') 同口径的本地日期串。"""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


class TestNetWorthDB:
    # 固定时间戳(2023-11-15 前后),同天/异天关系与时区无关:
    # T0 与 T0+60 必同天,T0 与 T0+2 天必异天。
    T0 = 1_700_000_000

    def test_record_and_read_back(self, db):
        db.record_net_worth("0xW", 100.5, 20.5)
        rows = db.get_net_worth_daily("0xW", days=36500)
        assert len(rows) == 1
        r = rows[0]
        assert r["cash"] == 100.5 and r["positions_value"] == 20.5 and r["total"] == 121.0
        assert r["date"] == _day(time.time())

    def test_same_day_takes_last(self, db):
        _insert(db, "0xW", 100, 0, self.T0)
        _insert(db, "0xW", 200, 50, self.T0 + 60)  # 同一天更晚
        rows = db.get_net_worth_daily("0xW", days=36500)
        assert len(rows) == 1
        assert rows[0]["total"] == 250 and rows[0]["date"] == _day(self.T0 + 60)

    def test_multi_day_ascending(self, db):
        _insert(db, "0xW", 100, 0, self.T0)
        _insert(db, "0xW", 300, 0, self.T0 + 2 * 86400)
        rows = db.get_net_worth_daily("0xW", days=36500)
        assert [r["date"] for r in rows] == [_day(self.T0), _day(self.T0 + 2 * 86400)]

    def test_days_cutoff_and_wallet_isolation(self, db):
        _insert(db, "0xW", 100, 0, self.T0)  # 2023 年,远超 30 天窗口
        db.record_net_worth("0xW", 50, 0)
        db.record_net_worth("0xOTHER", 999, 0)
        rows = db.get_net_worth_daily("0xW", days=30)
        assert len(rows) == 1 and rows[0]["total"] == 50
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_networth.py -q`
Expected: FAIL（`no such table: net_worth_history` / `AttributeError: record_net_worth`）

- [ ] **Step 3: 实现**

`models/database.py` 的 `_create_tables` executescript 里、`template_settings` 表定义之后追加：

```sql
            CREATE TABLE IF NOT EXISTS net_worth_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                cash REAL NOT NULL,
                positions_value REAL NOT NULL,
                total REAL NOT NULL,
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_networth_wallet_time
                ON net_worth_history(wallet, created_at);
```

文件末尾（`get_blacklist_ids` 之后）加：

```python
    # --- Net worth history (每钱包净值快照:启动 + 每日) ---

    def record_net_worth(self, wallet: str, cash: float, positions_value: float):
        c = self.conn.cursor()
        c.execute(
            "INSERT INTO net_worth_history (wallet, cash, positions_value, total)"
            " VALUES (?, ?, ?, ?)",
            (wallet, cash, positions_value, cash + positions_value),
        )
        self.conn.commit()

    def get_net_worth_daily(self, wallet: str, days: int = 90) -> list[dict]:
        """按本地日期聚合的净值序列:每天取最后一条,日期升序。"""
        cutoff = time.time() - days * 86400
        c = self.conn.cursor()
        c.execute(
            "SELECT date(created_at, 'unixepoch', 'localtime') AS d,"
            " cash, positions_value, total"
            " FROM net_worth_history WHERE wallet = ? AND created_at >= ?"
            " ORDER BY created_at",
            (wallet, cutoff),
        )
        by_day = {}
        for row in c.fetchall():
            by_day[row["d"]] = row  # 升序遍历,后写覆盖 => 每天最后一条
        return [
            {
                "date": d,
                "cash": r["cash"],
                "positions_value": r["positions_value"],
                "total": r["total"],
            }
            for d, r in by_day.items()
        ]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_networth.py tests/test_database.py -q`
Expected: PASS（含既有 DB 测试无回归）

- [ ] **Step 5: Commit**

```bash
git add models/database.py tests/test_networth.py
git commit -m "feat(networth): net_worth_history 表 + 按日聚合查询"
```

---

### Task 3: WalletWorker 快照接入

**Files:**
- Modify: `engine/manager.py`（`WalletWorker.__init__` / `_tick` / 新方法 `_maybe_snapshot_networth`）
- Test: `tests/test_networth_worker.py`

**Interfaces:**
- Consumes: Task 1 `should_snapshot`/`positions_value`；Task 2 `db.record_net_worth`。
- Produces: `WalletWorker._maybe_snapshot_networth()`（`_tick` 每拍调用）。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_networth_worker.py`：

```python
"""tests/test_networth_worker.py — worker 净值快照:首 tick 必记、跨天补记、失败不阻断。"""

from unittest.mock import MagicMock

from engine.manager import WalletWorker


def _worker():
    api, db = MagicMock(), MagicMock()
    api.get_balance.return_value = 100.0
    api.get_funder.return_value = "0xF"
    api.get_user_positions.return_value = [{"size": 10, "curPrice": 0.5}]
    w = WalletWorker(api, db, "0xW", {"fill_check_interval_sec": 5})
    return w, api, db


def test_first_call_records_cash_plus_positions():
    w, api, db = _worker()
    w._maybe_snapshot_networth()
    db.record_net_worth.assert_called_once_with("0xW", 100.0, 5.0)


def test_same_day_second_call_skips():
    w, api, db = _worker()
    w._maybe_snapshot_networth()
    w._maybe_snapshot_networth()
    assert db.record_net_worth.call_count == 1


def test_failure_no_raise_and_retries_next_tick():
    w, api, db = _worker()
    api.get_balance.side_effect = RuntimeError("proxy down")
    w._maybe_snapshot_networth()  # 不抛
    db.record_net_worth.assert_not_called()
    api.get_balance.side_effect = None  # 网络恢复 -> 下一拍补记
    w._maybe_snapshot_networth()
    db.record_net_worth.assert_called_once()


def test_tick_invokes_snapshot():
    w, api, db = _worker()
    w.monitor = MagicMock()  # 只验证 _tick 链路,监控步骤全 mock
    w._tick()
    db.record_net_worth.assert_called_once()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_networth_worker.py -q`
Expected: FAIL（`AttributeError: _maybe_snapshot_networth`）

- [ ] **Step 3: 实现**

`engine/manager.py`：

1. `WalletWorker.__init__` 末尾（`self._last_gap_skip = {}` 之后）加：

```python
        # 净值快照:上次快照的本地日期(YYYY-MM-DD)。None=本进程还没记过 ->
        # 首个 tick 必记(覆盖引擎启动/单钱包启动/手动启动监控),之后跨天才再记。
        self._last_networth_date = None
```

2. `_tick` 方法体开头（`self.monitor.begin_status_tick()` 之前）加一行：

```python
        self._maybe_snapshot_networth()
```

3. `_tick` 方法之后加新方法：

```python
    def _maybe_snapshot_networth(self):
        """净值快照(现金+持仓市值):首个 tick 必记,之后每天补记一次。

        失败只打 WARNING、不置日期 -> 下个 tick 自然重试;绝不阻断监控步骤。
        持仓估值仅供展示(currentValue/curPrice),不进任何交易决策。
        """
        from engine.networth import positions_value, should_snapshot

        today = time.strftime("%Y-%m-%d")
        if not should_snapshot(self._last_networth_date, today):
            return
        try:
            cash = self.api.get_balance()
            positions = self.api.get_user_positions(self.api.get_funder())
            self.db.record_net_worth(
                self.wallet_address, cash, positions_value(positions)
            )
            self._last_networth_date = today
        except Exception as e:
            logger.warning("净值快照失败 %s: %s", self.wallet_address, e)
```

（`time` 与 `logger` 在 `engine/manager.py` 已有,无需新 import。）

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_networth_worker.py tests/test_manager.py tests/test_place_orders.py -q`
Expected: PASS（worker 相关既有测试无回归）

- [ ] **Step 5: Commit**

```bash
git add engine/manager.py tests/test_networth_worker.py
git commit -m "feat(networth): worker 首tick+每日净值快照(失败WARNING不阻断监控)"
```

---

### Task 4: `GET /api/networth` 接口

**Files:**
- Modify: `web/routes.py`（`api_dashboard` 之前加路由）
- Test: `tests/test_networth_routes.py`

**Interfaces:**
- Consumes: Task 2 `db.get_net_worth_daily`。
- Produces: `GET /api/networth?wallet=<addr>&days=<n>` → `{"wallet": str, "series": [{date, cash, positions_value, total}]}`；缺 wallet 参数 → 400。Task 5 前端消费。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_networth_routes.py`：

```python
"""tests/test_networth_routes.py — /api/networth 契约。"""

import web.routes as routes
from models.database import Database


def _client_with_db(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client, db


def test_networth_series_roundtrip(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.record_net_worth("0xW", 100.0, 20.0)
    data = client.get("/api/networth?wallet=0xW").get_json()
    assert data["wallet"] == "0xW"
    assert len(data["series"]) == 1
    row = data["series"][0]
    assert row["total"] == 120.0 and set(row) == {"date", "cash", "positions_value", "total"}


def test_networth_missing_wallet_400(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    assert client.get("/api/networth").status_code == 400


def test_networth_bad_days_falls_back_default(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.record_net_worth("0xW", 1.0, 0.0)
    data = client.get("/api/networth?wallet=0xW&days=abc").get_json()
    assert len(data["series"]) == 1  # days 非法回落 90,不 500
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_networth_routes.py -q`
Expected: FAIL（404，路由不存在）

- [ ] **Step 3: 实现**

`web/routes.py` 中 `# --- API: Dashboard Summary ---` 注释之前加：

```python
# --- API: 净值历史 ---


@app.route("/api/networth", methods=["GET"])
@login_required
def api_networth():
    wallet = (request.args.get("wallet") or "").strip()
    if not wallet:
        return jsonify({"error": "缺少 wallet 参数"}), 400
    try:
        days = int(request.args.get("days", 90))
    except (TypeError, ValueError):
        days = 90
    return jsonify({"wallet": wallet, "series": db.get_net_worth_daily(wallet, days)})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_networth_routes.py -q`
Expected: PASS（3 项）

- [ ] **Step 5: Commit**

```bash
git add web/routes.py tests/test_networth_routes.py
git commit -m "feat(networth): GET /api/networth 按日净值序列接口"
```

---

### Task 5: 「资产曲线」页面（页面路由 + 侧边栏 + SVG 折线）

⚠️ 本任务写含中文的前端文件，必须由主会话直接执行，不派 subagent。

**Files:**
- Modify: `web/routes.py`（页面路由，`help_page` 之后）
- Modify: `web/templates/base.html`（侧边栏加链接）
- Create: `web/templates/networth.html`
- Test: `tests/test_networth_routes.py`（追加页面 200 测试）

**Interfaces:**
- Consumes: Task 4 `/api/networth`、既有 `/api/wallets`。
- Produces: 页面 `GET /networth`（endpoint 名 `networth_page`）。

- [ ] **Step 1: 写失败测试**

`tests/test_networth_routes.py` 追加：

```python
def test_networth_page_renders(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    r = client.get("/networth")
    assert r.status_code == 200
    assert "资产曲线".encode() in r.data
```

Run: `pytest tests/test_networth_routes.py -q`
Expected: FAIL（404）

- [ ] **Step 2: 页面路由**

`web/routes.py` 的 `help_page` 之后加：

```python
@app.route("/networth")
@login_required
def networth_page():
    return render_template("networth.html")
```

- [ ] **Step 3: 侧边栏链接**

`web/templates/base.html` 中「历史」链接行之后加：

```html
        <a href="{{ url_for('networth_page') }}" class="{% if request.endpoint=='networth_page' %}active{% endif %}">资产曲线</a>
```

- [ ] **Step 4: 页面模板**

创建 `web/templates/networth.html`（完整内容）：

```html
{% extends "base.html" %}
{% block content %}
<h1>资产曲线</h1>
<section class="config-section">
    <div class="form-inline">
        <label>钱包 <select id="nw-wallet"></select></label>
        <label>查询某日 <input type="date" id="nw-date" onchange="queryDay()"></label>
        <span id="nw-day-result" class="hint" style="color:#888"></span>
    </div>
    <div id="nw-chart-wrap" style="position:relative;margin-top:12px">
        <svg id="nw-chart" viewBox="0 0 900 320" style="width:100%;height:320px;display:block"></svg>
        <div id="nw-tip" style="display:none;position:absolute;pointer-events:none;background:rgba(0,0,0,.78);color:#fff;padding:6px 8px;border-radius:4px;font-size:12px;white-space:nowrap"></div>
    </div>
    <div id="nw-empty" class="hint" style="display:none;color:#888">暂无净值记录：启动引擎后每天自动记录一次。</div>
    <p class="hint" style="color:#888;font-size:12px">净值 = 现金(pUSD) + 持仓市值（Data API 估值，仅供展示，不参与交易决策）。引擎启动时与运行中每天各记一次，同一天取最后一条。近一年数据。<span style="color:#4a90d9">━ 净值</span>　<span style="color:#999">━ 现金</span></p>
</section>
{% endblock %}

{% block scripts %}
<script>
let nwSeries = [];

function loadWalletList() {
    fetch('/api/wallets').then(r => r.json()).then(ws => {
        const sel = document.getElementById('nw-wallet');
        sel.innerHTML = ws.map(w =>
            `<option value="${w.address}">${w.address.slice(0,6)}...${w.address.slice(-4)}</option>`
        ).join('');
        sel.onchange = loadSeries;
        if (ws.length) loadSeries(); else showEmpty(true);
    });
}

function showEmpty(empty) {
    document.getElementById('nw-empty').style.display = empty ? '' : 'none';
    document.getElementById('nw-chart-wrap').style.display = empty ? 'none' : '';
}

function loadSeries() {
    const w = document.getElementById('nw-wallet').value;
    if (!w) { showEmpty(true); return; }
    fetch(`/api/networth?wallet=${encodeURIComponent(w)}&days=365`)
        .then(r => r.json())
        .then(d => { nwSeries = d.series || []; drawChart(nwSeries); queryDay(); });
}

const PAD = {l: 60, r: 16, t: 12, b: 26}, W = 900, H = 320;

function drawChart(series) {
    const svg = document.getElementById('nw-chart');
    svg.innerHTML = '';
    if (!series.length) { showEmpty(true); return; }
    showEmpty(false);
    const maxV = Math.max(...series.map(p => p.total), 1);
    const minV = Math.min(...series.map(p => Math.min(p.cash, p.total)), 0);
    const x = i => series.length === 1 ? (PAD.l + (W - PAD.l - PAD.r) / 2)
        : PAD.l + (W - PAD.l - PAD.r) * i / (series.length - 1);
    const y = v => H - PAD.b - (H - PAD.t - PAD.b) * (v - minV) / (maxV - minV || 1);
    const NS = 'http://www.w3.org/2000/svg';
    function el(tag, attrs) {
        const e = document.createElementNS(NS, tag);
        Object.keys(attrs).forEach(k => e.setAttribute(k, attrs[k]));
        return e;
    }
    // 网格线 + 纵轴刻度(5 条)
    for (let g = 0; g <= 4; g++) {
        const v = minV + (maxV - minV) * g / 4;
        const gy = y(v);
        svg.appendChild(el('line', {x1: PAD.l, y1: gy, x2: W - PAD.r, y2: gy,
            stroke: '#8884', 'stroke-width': 1}));
        const t = el('text', {x: PAD.l - 6, y: gy + 4, 'text-anchor': 'end',
            'font-size': 11, fill: '#888'});
        t.textContent = v.toFixed(v >= 100 ? 0 : 2);
        svg.appendChild(t);
    }
    // 首尾日期
    [[0, 'start'], [series.length - 1, 'end']].forEach(pair => {
        const t = el('text', {x: x(pair[0]), y: H - 8, 'text-anchor': pair[1],
            'font-size': 11, fill: '#888'});
        t.textContent = series[pair[0]].date;
        svg.appendChild(t);
    });
    // 折线:净值主线 + 现金辅线
    [['total', '#4a90d9', 2], ['cash', '#999', 1.2]].forEach(cfg => {
        const pts = series.map((p, i) => `${x(i)},${y(p[cfg[0]])}`).join(' ');
        svg.appendChild(el('polyline', {points: pts, fill: 'none',
            stroke: cfg[1], 'stroke-width': cfg[2]}));
    });
    // 数据点(净值线):悬停显示当天明细
    const tip = document.getElementById('nw-tip');
    series.forEach((p, i) => {
        const c = el('circle', {cx: x(i), cy: y(p.total), r: 4, fill: '#4a90d9', opacity: 0});
        c.addEventListener('mouseenter', ev => {
            c.setAttribute('opacity', 1);
            tip.innerHTML = `${p.date}<br>净值 ${p.total.toFixed(2)}<br>现金 ${p.cash.toFixed(2)} · 持仓 ${p.positions_value.toFixed(2)}`;
            tip.style.display = '';
            const box = document.getElementById('nw-chart-wrap').getBoundingClientRect();
            tip.style.left = Math.min(ev.clientX - box.left + 12, box.width - 170) + 'px';
            tip.style.top = (ev.clientY - box.top - 10) + 'px';
        });
        c.addEventListener('mouseleave', () => { c.setAttribute('opacity', 0); tip.style.display = 'none'; });
        svg.appendChild(c);
    });
}

function queryDay() {
    const d = document.getElementById('nw-date').value;
    const out = document.getElementById('nw-day-result');
    if (!d) { out.textContent = ''; return; }
    const hit = nwSeries.find(p => p.date === d);
    out.textContent = hit
        ? `${d}：净值 ${hit.total.toFixed(2)}（现金 ${hit.cash.toFixed(2)} + 持仓 ${hit.positions_value.toFixed(2)}）`
        : `${d}：无记录`;
}

loadWalletList();
</script>
{% endblock %}
```

- [ ] **Step 5: JS 语法与编码检查**

```bash
cd "C:/Users/Hank/PycharmProjects/poly简单做市"
python -c "import re,io;src=io.open('web/templates/networth.html',encoding='utf-8').read();io.open('networth_check.js','w',encoding='utf-8').write('\n'.join(re.findall(r'<script>(.*?)</script>',src,re.S)))"
node --check networth_check.js && rm networth_check.js
python -c "print('BOM' if open('web/templates/networth.html','rb').read(3)==b'\xef\xbb\xbf' else 'OK')"
```

Expected: node --check 无输出（语法通过）；BOM 检查输出 `OK`。再目检文件里的中文无别字。

- [ ] **Step 6: 跑测试确认通过 + 全量回归**

Run: `pytest tests/test_networth_routes.py -q && pytest -q`
Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add web/routes.py web/templates/base.html web/templates/networth.html tests/test_networth_routes.py
git commit -m "feat(networth): 资产曲线页(第8屏,SVG折线+悬停+按日查询)"
```

---

## 收尾

- 全量 `pytest -q` 全绿后本计划完成。
- 发版不在本计划内：与「档位模块化挂单」合并为一次主版本发布（见另一份计划），届时改 `version.py` 走 `release.ps1`。
- 人工走查（可选但建议）：`python app.py` 登录后启动引擎，确认 `net_worth_history` 出现当日行、资产曲线页有点、悬停与日期查询正常。
