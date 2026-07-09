# 全局黑名单 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 condition_id 全局拉黑某市场:引擎运行期任何钱包都不再挂它的 YES/NO 买单;加入黑名单时撤掉所有钱包挂在该市场的买单(持仓/止盈卖单不动);提供独立的黑名单管理页 + 当前挂单行的「加入黑名单」按钮。

**Architecture:** 新建全局 `blacklist` 表(condition_id 主键)+ DB 方法。拦截在 `place_orders`(权威闸门)和 `scanner`(保持列表干净)两处,各自一次性加载 `get_blacklist_ids()` 集合按 market_id 跳过。加入黑名单的 API 写表 + 遍历所有钱包撤该市场买单(撤单过滤抽成纯函数 `engine/blacklist_ops.py`)。前端新增 `/blacklist` 导航页 + orders 页按钮。

**Tech Stack:** Python 3, Flask, SQLite, pytest + unittest.mock, 原生 JS。

参考 spec:`docs/superpowers/specs/2026-05-23-global-blacklist-design.md`

---

## File Structure

- Modify `models/database.py`:新增 `blacklist` 表 + `add_to_blacklist`/`remove_from_blacklist`/`get_blacklist`/`get_blacklist_ids`。
- Modify `engine/manager.py`:`place_orders` 加载黑名单 set 并跳过黑名单市场。
- Modify `engine/scanner.py`:`scan` 加载黑名单 set 并把黑名单 condition_id 排除出 eligible。
- Create `engine/blacklist_ops.py`:纯函数 `buy_order_ids_for_condition`。
- Modify `web/routes.py`:`/blacklist` 页面路由 + `GET/POST/DELETE /api/blacklist`。
- Modify `web/templates/base.html`:导航加「黑名单」入口。
- Create `web/templates/blacklist.html`:管理页。
- Modify `web/templates/orders.html`:当前挂单行加「加入黑名单」按钮。
- Test:`tests/test_database.py`、`tests/test_place_orders.py`、`tests/test_scanner.py`、`tests/test_blacklist_ops.py`(新建)。

依赖顺序:Task1 → Task2、Task3(依赖 Task1)→ Task4(独立)→ Task5(依赖 Task1+4)→ Task6(依赖 Task5)。

---

### Task 1: DB — blacklist 表与方法

**Files:**
- Modify: `models/database.py`
- Test: `tests/test_database.py`

- [ ] **Step 1: 写失败测试** — 追加到 `tests/test_database.py` 末尾:

```python
class TestBlacklist:
    def test_add_and_get_ids(self, db):
        db.add_to_blacklist("0xc1", "spam market")
        assert db.get_blacklist_ids() == {"0xc1"}

    def test_get_blacklist_returns_note_and_time(self, db):
        db.add_to_blacklist("0xc1", "spam")
        bl = db.get_blacklist()
        assert len(bl) == 1
        assert bl[0]["condition_id"] == "0xc1"
        assert bl[0]["note"] == "spam"
        assert "added_at" in bl[0]

    def test_remove(self, db):
        db.add_to_blacklist("0xc1", "")
        db.remove_from_blacklist("0xc1")
        assert db.get_blacklist_ids() == set()

    def test_add_dedups_and_updates_note(self, db):
        db.add_to_blacklist("0xc1", "first")
        db.add_to_blacklist("0xc1", "second")
        bl = db.get_blacklist()
        assert len(bl) == 1
        assert bl[0]["note"] == "second"

    def test_empty_condition_id_skipped(self, db):
        db.add_to_blacklist("", "x")
        assert db.get_blacklist_ids() == set()

    def test_empty_when_none(self, db):
        assert db.get_blacklist_ids() == set()
        assert db.get_blacklist() == []
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_database.py::TestBlacklist -v`
Expected: FAIL(`AttributeError: ... 'add_to_blacklist'`)。

- [ ] **Step 3: 建表** — 在 `models/database.py` `_create_tables` 的 `executescript("""...""")` 里,`market_meta` 表之后、闭合 `"""` 之前加入:

```sql
            CREATE TABLE IF NOT EXISTS blacklist (
                condition_id TEXT PRIMARY KEY,
                note TEXT NOT NULL DEFAULT '',
                added_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
```

- [ ] **Step 4: 加方法** — 在 `models/database.py` 的 `# --- Market Meta ...` 区块之后(`get_market_meta` 方法之后)追加:

```python
    # --- Blacklist (global, by condition_id) ---

    def add_to_blacklist(self, condition_id: str, note: str = ""):
        """加入(或更新)一个 condition_id 到全局黑名单。空 id 跳过。"""
        if not condition_id:
            return
        c = self.conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO blacklist (condition_id, note, added_at) "
            "VALUES (?, ?, ?)",
            (condition_id, note or "", time.time()),
        )
        self.conn.commit()

    def remove_from_blacklist(self, condition_id: str):
        c = self.conn.cursor()
        c.execute("DELETE FROM blacklist WHERE condition_id = ?", (condition_id,))
        self.conn.commit()

    def get_blacklist(self) -> list[dict]:
        """全部黑名单条目(最新在前),供管理界面用。"""
        c = self.conn.cursor()
        c.execute(
            "SELECT condition_id, note, added_at FROM blacklist ORDER BY added_at DESC"
        )
        return [dict(row) for row in c.fetchall()]

    def get_blacklist_ids(self) -> set:
        """黑名单 condition_id 集合,供拦截热路径快速 membership 判断。"""
        c = self.conn.cursor()
        c.execute("SELECT condition_id FROM blacklist")
        return {row["condition_id"] for row in c.fetchall()}
```

（`time` 已在文件顶部 import。）

- [ ] **Step 5: 运行,确认通过**

Run: `pytest tests/test_database.py::TestBlacklist -v`
Expected: 6 个 PASS。

- [ ] **Step 6: 提交**

```bash
git add models/database.py tests/test_database.py
git commit -m "feat: blacklist 全局表(condition_id)+ 增删查方法

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: place_orders 拦截黑名单市场

**Files:**
- Modify: `engine/manager.py`(`place_orders`)
- Test: `tests/test_place_orders.py`

- [ ] **Step 1: 改测试 helper + 写失败测试**

`tests/test_place_orders.py` 的两个 helper 给 db mock 补一个默认空黑名单(否则新代码调 `get_blacklist_ids()` 行为不明确)。在 `_worker` 里 `db.get_settings.return_value = {...}` 之后加一行:

```python
    db.get_blacklist_ids.return_value = set()
```

在 `_worker_capped` 里 `db.get_settings.return_value = {...}` 之后同样加一行:

```python
    db.get_blacklist_ids.return_value = set()
```

然后追加测试到文件末尾:

```python
def test_skips_blacklisted_market():
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    db.get_blacklist_ids.return_value = {"m0"}  # m0 在黑名单
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders([_market(0)])
    api.place_limit_buy.assert_not_called()


def test_non_blacklisted_market_still_placed():
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    db.get_blacklist_ids.return_value = {"mOTHER"}  # 别的市场在黑名单
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders([_market(0)])
    api.place_limit_buy.assert_called_once()
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_place_orders.py::test_skips_blacklisted_market -v`
Expected: FAIL（当前没有黑名单拦截,m0 会被下单 → `assert_not_called` 失败）。

- [ ] **Step 3: 实现** — 在 `engine/manager.py` `place_orders` 里,找到 `for market in eligible_markets:`(约 141 行),在它**之前**加载黑名单集合:

把:
```python
        effective_limit = slots if limit is None else min(limit, slots)

        for market in eligible_markets:
            if self.db.is_in_cooldown(self.wallet_address, market["market_id"]):
                continue
```

改为:
```python
        effective_limit = slots if limit is None else min(limit, slots)

        # 全局黑名单:任何钱包都不再挂这些 condition_id 的买单(一次性加载)。
        blacklist = self.db.get_blacklist_ids()

        for market in eligible_markets:
            if market["market_id"] in blacklist:
                continue
            if self.db.is_in_cooldown(self.wallet_address, market["market_id"]):
                continue
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_place_orders.py -v`
Expected: 全部 PASS(原有 + 2 个新增;helper 补了空黑名单,原有测试不受影响)。

- [ ] **Step 5: 提交**

```bash
git add engine/manager.py tests/test_place_orders.py
git commit -m "feat: place_orders 跳过黑名单市场(任何钱包不再挂其买单)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: scanner 排除黑名单市场

**Files:**
- Modify: `engine/scanner.py`(`scan`)
- Test: `tests/test_scanner.py`

- [ ] **Step 1: 改测试 helper + 写失败测试**

`tests/test_scanner.py` 的 `_make_scanner` 给 db mock 补默认空黑名单。在 `db.is_in_cooldown.return_value = False` 之后加一行:

```python
    db.get_blacklist_ids.return_value = set()
```

然后追加测试到文件末尾:

```python
def test_scan_excludes_blacklisted_market():
    scanner, api, db = _make_scanner()
    db.get_blacklist_ids.return_value = {"0xabc123"}  # _sample_market 的 condition_id
    api.get_rewards_markets.return_value = [_sample_market()]
    api.get_orderbook.return_value = _sample_orderbook()
    results = scanner.scan()
    assert len(results) == 0
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_scanner.py::test_scan_excludes_blacklisted_market -v`
Expected: FAIL（当前无排除,该市场仍进 eligible → len==1 ≠ 0）。

- [ ] **Step 3: 实现** — 在 `engine/scanner.py` `scan` 里:

(3a) 在预筛 `candidates = []` 之前加载黑名单。找到(约 77-80 行):
```python
        eligible = []

        # Pre-filter: reward + settlement date + cooldown
        candidates = []
```
改为:
```python
        eligible = []

        # 全局黑名单:这些 condition_id 不进 eligible(一次性加载)。
        blacklist = self.db.get_blacklist_ids()

        # Pre-filter: reward + settlement date + cooldown
        candidates = []
```

(3b) 在预筛循环里,找到(约 94-96 行):
```python
            condition_id = market.get("condition_id", "")
            if self.db.is_in_cooldown(self.wallet_address, condition_id):
                continue
```
改为:
```python
            condition_id = market.get("condition_id", "")
            if condition_id in blacklist:
                continue
            if self.db.is_in_cooldown(self.wallet_address, condition_id):
                continue
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_scanner.py -v`
Expected: 全部 PASS(原有 + 1 个新增)。

- [ ] **Step 5: 提交**

```bash
git add engine/scanner.py tests/test_scanner.py
git commit -m "feat: scanner 把黑名单 condition_id 排除出 eligible 列表

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 纯函数 `buy_order_ids_for_condition`

**Files:**
- Create: `engine/blacklist_ops.py`
- Test: `tests/test_blacklist_ops.py`(新建)

- [ ] **Step 1: 写失败测试** — 新建 `tests/test_blacklist_ops.py`:

```python
"""tests/test_blacklist_ops.py"""

from engine.blacklist_ops import buy_order_ids_for_condition


def _orders():
    return [
        {"id": "b1", "side": "BUY", "market": "0xc1"},
        {"id": "s1", "side": "SELL", "market": "0xc1"},    # SELL: 不挑
        {"id": "b2", "side": "BUY", "market": "0xOTHER"},  # 别的市场: 不挑
        {"id": "b3", "side": "BUY", "market": "0xc1"},
    ]


def test_picks_only_matching_buys():
    assert buy_order_ids_for_condition(_orders(), "0xc1") == ["b1", "b3"]


def test_excludes_sell_orders():
    assert "s1" not in buy_order_ids_for_condition(_orders(), "0xc1")


def test_excludes_other_markets():
    assert "b2" not in buy_order_ids_for_condition(_orders(), "0xc1")


def test_no_match_returns_empty():
    assert buy_order_ids_for_condition(_orders(), "0xNONE") == []


def test_skips_orders_without_id():
    orders = [{"side": "BUY", "market": "0xc1"}]  # 无 id
    assert buy_order_ids_for_condition(orders, "0xc1") == []
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_blacklist_ops.py -v`
Expected: FAIL(`ModuleNotFoundError: No module named 'engine.blacklist_ops'`)。

- [ ] **Step 3: 实现** — 新建 `engine/blacklist_ops.py`:

```python
"""engine/blacklist_ops.py — 黑名单相关纯逻辑(无 I/O,便于单测)。"""


def buy_order_ids_for_condition(orders: list, condition_id: str) -> list:
    """从一个钱包的 open orders 里挑出该 condition_id 的 BUY 单 id。

    只挑 side==BUY 且 market==condition_id 且有 id 的单;
    SELL(止盈卖单)和其它市场的单不挑。
    """
    return [
        o["id"]
        for o in orders
        if o.get("side") == "BUY"
        and o.get("market") == condition_id
        and o.get("id")
    ]
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_blacklist_ops.py -v`
Expected: 5 个 PASS。

- [ ] **Step 5: 提交**

```bash
git add engine/blacklist_ops.py tests/test_blacklist_ops.py
git commit -m "feat: buy_order_ids_for_condition 纯函数(挑该市场的买单 id)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: API 接口 + 页面路由

**Files:**
- Modify: `web/routes.py`

无路由级单测(项目无基建);核心逻辑由 Task1/4 单测覆盖。验证:导入冒烟 + 全套 pytest。

- [ ] **Step 1: 加 import**

在 `web/routes.py` 顶部 import 区(`from engine.market_links import ...` 一行附近)加:

```python
from engine.blacklist_ops import buy_order_ids_for_condition
```

- [ ] **Step 2: 加页面路由**

在 `web/routes.py` 的 `# --- Pages ---` 区,`logs_page`(`/logs`)之后加:

```python
@app.route("/blacklist")
@login_required
def blacklist_page():
    return render_template("blacklist.html")
```

- [ ] **Step 3: 加 API 接口**

在 API 区(例如 `# --- API: Eligible Markets ---` 之前,或文件 API 段落任意合适处)加:

```python
# --- API: Blacklist ---


@app.route("/api/blacklist", methods=["GET"])
@login_required
def api_get_blacklist():
    rows = db.get_blacklist()
    _enrich_rows(rows, "condition_id")
    return jsonify(rows)


@app.route("/api/blacklist", methods=["POST"])
@login_required
def api_add_blacklist():
    data = request.get_json(silent=True) or {}
    cid = (data.get("condition_id") or "").strip()
    note = (data.get("note") or "").strip()
    if not cid:
        return jsonify({"error": "condition_id 不能为空"}), 400
    db.add_to_blacklist(cid, note)
    # 撤掉所有钱包挂在该 condition_id 的买单(止盈卖单/持仓不动)
    cancelled = 0
    for addr, api in _wallet_apis().items():
        try:
            ids = buy_order_ids_for_condition(api.get_open_orders(), cid)
            if ids:
                api.cancel_orders(ids)
                cancelled += len(ids)
        except Exception as e:
            app.logger.error("blacklist cancel for %s failed: %s", addr, e)
    return jsonify({"ok": True, "cancelled": cancelled})


@app.route("/api/blacklist/<condition_id>", methods=["DELETE"])
@login_required
def api_remove_blacklist(condition_id):
    db.remove_from_blacklist(condition_id)
    return jsonify({"ok": True})
```

- [ ] **Step 4: 导入冒烟 + 全套测试**

Run: `python -c "import web.routes; print('import ok')"`
Expected: `import ok`。

Run: `pytest -q`
Expected: 全部 PASS（不回归;本任务未加 Python 测试）。

- [ ] **Step 5: 提交**

```bash
git add web/routes.py
git commit -m "feat: 黑名单 API(增删查)+ 加入即撤该市场所有钱包买单 + 页面路由

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 前端 — 导航 + 管理页 + 挂单行按钮

**Files:**
- Modify: `web/templates/base.html`
- Create: `web/templates/blacklist.html`
- Modify: `web/templates/orders.html`

无自动化测试。验证:启动后人工查看。

- [ ] **Step 1: 导航加入口**

在 `web/templates/base.html` 的 `nav-links` 里,「监控状态」那行之后、「退出」那行之前,插入:

```html
            <a href="{{ url_for('blacklist_page') }}" class="{% if request.endpoint == 'blacklist_page' %}active{% endif %}">黑名单</a>
```

- [ ] **Step 2: 新建管理页** — 创建 `web/templates/blacklist.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>黑名单</h1>
<p class="hint">加入黑名单的市场,引擎运行期任何钱包都不会再挂它的买单(YES/NO 都拦)。加入时会撤掉所有钱包挂在该市场的买单;已成交持仓不受影响。</p>

<div class="filter-bar">
  <label>condition ID：</label>
  <input type="text" id="bl-cid" placeholder="粘贴完整 condition_id (0x...)" style="width:420px">
  <label>备注：</label>
  <input type="text" id="bl-note" placeholder="可选">
  <button class="btn btn-sm btn-danger" onclick="addBlacklist()">加入黑名单</button>
</div>

<table class="data-table">
  <thead><tr><th>市场</th><th>加入时间</th><th>备注</th><th>操作</th></tr></thead>
  <tbody id="blacklist-body"></tbody>
</table>
{% endblock %}

{% block scripts %}
<script>
function refreshBlacklist() {
  fetch('/api/blacklist').then(r => r.json()).then(rows => {
    const body = document.getElementById('blacklist-body');
    if (!rows.length) { body.innerHTML = '<tr><td colspan="4">黑名单为空</td></tr>'; return; }
    body.innerHTML = rows.map(r => `
      <tr>
        <td>${marketCell(r.market_name, r.condition_id, r.market_url)}</td>
        <td>${r.added_at ? new Date(r.added_at*1000).toLocaleString('zh-CN') : '-'}</td>
        <td>${escapeHtml(r.note || '')}</td>
        <td><button class="btn btn-sm" onclick="removeBlacklist('${escapeHtml(r.condition_id)}')">移除</button></td>
      </tr>`).join('');
  });
}

function addBlacklist() {
  const cid = document.getElementById('bl-cid').value.trim();
  const note = document.getElementById('bl-note').value.trim();
  if (!cid) { alert('请粘贴 condition_id'); return; }
  if (!confirm('加入黑名单将撤掉所有钱包挂在该市场的买单,确定?')) return;
  fetch('/api/blacklist', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({condition_id: cid, note: note})})
    .then(r => r.json()).then(d => {
      if (d.error) { alert(d.error); return; }
      if (d.cancelled) alert('已加入黑名单,撤掉了 ' + d.cancelled + ' 笔买单');
      document.getElementById('bl-cid').value = '';
      document.getElementById('bl-note').value = '';
      refreshBlacklist();
    });
}

function removeBlacklist(cid) {
  if (!confirm('从黑名单移除该市场?(以后会重新参与挂单)')) return;
  fetch('/api/blacklist/' + encodeURIComponent(cid), {method:'DELETE'})
    .then(() => refreshBlacklist());
}

refreshBlacklist();
</script>
{% endblock %}
```

- [ ] **Step 3: 挂单行加按钮** — 在 `web/templates/orders.html` 的 `renderOrders()` 里,把操作列(约第 99-100 行):

```javascript
    <td><button class="btn btn-sm btn-danger"
        onclick="cancelOne('${o.order_id}','${o.wallet}')">撤单</button></td></tr>`).join('');
```

改为(撤单旁加「加入黑名单」按钮):

```javascript
    <td><button class="btn btn-sm btn-danger"
        onclick="cancelOne('${o.order_id}','${o.wallet}')">撤单</button>
        <button class="btn btn-sm btn-warning"
        onclick="blacklistMarket('${o.market}')">加入黑名单</button></td></tr>`).join('');
```

并在 `orders.html` 的 `<script>` 里(例如 `cancelAllBuys` 函数之后)加:

```javascript
function blacklistMarket(cid){
  if(!cid){alert('该订单无市场信息');return;}
  if(!confirm('加入黑名单将撤掉所有钱包在该市场的买单,确定?'))return;
  fetch('/api/blacklist',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({condition_id:cid})}).then(r=>r.json()).then(d=>{
      if(d.error){alert(d.error);return;}
      if(d.cancelled)alert('已加入黑名单,撤掉了 '+d.cancelled+' 笔买单');
      refreshOrders();
    });
}
```

- [ ] **Step 4: 人工验证**

启动 `python app.py`,登录后:
Expected:
- 顶部导航出现「黑名单」,点进去是管理页;粘贴一个 condition_id + 备注 → 加入 → 列表出现该市场(市场名为链接,有 📋),「移除」可删。
- 「订单管理」当前挂单每行「撤单」旁多了「加入黑名单」按钮;点它 confirm 后,该市场所有钱包的买单被撤、并出现在黑名单页;之后引擎不再挂该市场。
- (引擎运行中)被拉黑的市场不再出现在仪表盘 eligible、不再被分发挂单。

- [ ] **Step 5: 提交**

```bash
git add web/templates/base.html web/templates/blacklist.html web/templates/orders.html
git commit -m "feat: 黑名单管理页 + 导航入口 + 当前挂单行加入黑名单按钮

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec 覆盖:**
- 数据层(blacklist 表 + 4 方法) → Task 1 ✓
- 拦截 place_orders(权威闸门,按 market_id 跳过) → Task 2 ✓
- 拦截 scanner(排除出 eligible) → Task 3 ✓
- 加入即撤该市场所有钱包买单(纯函数 + 遍历钱包) → Task 4 + Task 5 ✓
- API GET/POST/DELETE,GET 用 _enrich_rows 补名 → Task 5 ✓
- 前端独立导航页 + 添加/列表/移除 + 挂单行按钮(带 confirm) → Task 6 ✓
- 持仓/止盈卖单不动 → 纯函数只挑 BUY(`test_excludes_sell_orders`)+ API 只撤买单 ✓
- 全局生效 → 表无 wallet 列;place_orders/scanner 都用 get_blacklist_ids;API 遍历所有钱包 ✓

**2. 占位符扫描:** 无 TBD/TODO;每步给完整代码 + 确切命令 + 预期。✓

**3. 类型/签名一致:**
- `add_to_blacklist(condition_id, note="")` / `remove_from_blacklist(condition_id)` / `get_blacklist() -> list[dict]` / `get_blacklist_ids() -> set` — Task 1 定义;Task 2/3 用 `get_blacklist_ids()`、Task 5 用 `get_blacklist`/`add_to_blacklist`/`remove_from_blacklist`,一致。✓
- `buy_order_ids_for_condition(orders, condition_id) -> list` — Task 4 定义,Task 5 以 `(api.get_open_orders(), cid)` 调用,一致。✓
- 前端 `marketCell(name, conditionId, url)` 在 blacklist.html 以 `(r.market_name, r.condition_id, r.market_url)` 调用;`_enrich_rows(rows, "condition_id")`(Task 5)正好补这三个字段到 GET /api/blacklist 的行上(get_blacklist 行已带 condition_id)。✓
- orders.html 按钮用 `o.market`(= condition_id,/api/orders 已透出),传给 POST /api/blacklist 的 `condition_id`,一致。✓
- `db`、`_wallet_apis`、`_enrich_rows`、`request`、`jsonify`、`render_template` 均为 routes.py 既有模块级符号。✓
