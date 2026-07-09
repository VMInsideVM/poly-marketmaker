# 展示优化（拆滚动区 + 市场名/超链接/复制）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让订单管理页（挂单/持仓）和监控状态页（监控状态/操作记录）各自独立限高滚动，并在挂单/持仓/监控状态/操作记录处显示市场名称 + Polymarket 超链接 + 一键复制完整 condition_id。

**Architecture:** 新增一张持久 `market_meta` 表（condition_id → 名称 + 两个 slug），scanner 扫描时 upsert（不随扫描清空、累积覆盖）。后端用两个纯函数 `market_url` / `enrich_with_market_meta` 把名称和链接补进 `/api/orders`、`/api/positions`、`/api/actions`、`/api/monitor-status`。前端在 `app.js` 加共用 `marketCell` 助手 + 复制逻辑，模板把表包进 `.table-scroll` 限高容器、表头 sticky。

**Tech Stack:** Python 3、Flask、SQLite（`models/database.py`）、pytest + unittest.mock、原生 JS、CSS。

参考 spec：`docs/superpowers/specs/2026-05-23-display-optimization-market-names-design.md`

---

## File Structure

- Modify `models/database.py`：新增 `market_meta` 表 + `upsert_market_meta` / `get_market_meta` 方法。
- Create `engine/market_links.py`：纯函数 `market_url` / `enrich_with_market_meta`。
- Modify `engine/scanner.py`：扫描循环里 upsert market_meta。
- Modify `web/routes.py`：4 个接口接入 enrich + `/api/positions` 补 `condition_id`。
- Modify `web/static/app.js`：`escapeHtml` / `copyCid` / `fallbackCopy` / `showToast` / `marketCell`。
- Modify `web/static/style.css`：`.table-scroll`、sticky thead、`.btn-xs`、`.app-toast`。
- Modify `web/templates/orders.html`、`web/templates/logs.html`：包滚动容器 + 用 `marketCell`。
- Modify `web/templates/dashboard.html` + `/api/eligible`（**可选** Task 8）。
- Test：`tests/test_database.py`、`tests/test_market_links.py`（新建）、`tests/test_scanner.py`。

---

### Task 1: DB — `market_meta` 表与读写方法

**Files:**
- Modify: `models/database.py`（`_create_tables` 的 executescript；新增两个方法）
- Test: `tests/test_database.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_database.py` 末尾追加（该文件已有 `db` fixture，基于 `tmp_path` 的真实 SQLite）：

```python
class TestMarketMeta:
    def test_upsert_and_get(self, db):
        db.upsert_market_meta("0xc1", "市场A", "slug-a", "evt-a")
        meta = db.get_market_meta()
        assert meta["0xc1"] == {
            "name": "市场A", "market_slug": "slug-a", "event_slug": "evt-a"
        }

    def test_upsert_updates_same_condition_id(self, db):
        db.upsert_market_meta("0xc1", "旧名", "old", "")
        db.upsert_market_meta("0xc1", "新名", "new", "evt")
        meta = db.get_market_meta()
        assert len(meta) == 1
        assert meta["0xc1"]["name"] == "新名"
        assert meta["0xc1"]["market_slug"] == "new"
        assert meta["0xc1"]["event_slug"] == "evt"

    def test_empty_condition_id_skipped(self, db):
        db.upsert_market_meta("", "x", "y", "z")
        assert db.get_market_meta() == {}

    def test_accumulates_across_calls(self, db):
        db.upsert_market_meta("0xc1", "A", "", "")
        db.upsert_market_meta("0xc2", "B", "", "")
        assert set(db.get_market_meta().keys()) == {"0xc1", "0xc2"}
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `pytest tests/test_database.py::TestMarketMeta -v`
Expected: FAIL（`AttributeError: 'Database' object has no attribute 'upsert_market_meta'`）。

- [ ] **Step 3: 建表**

在 `models/database.py` `_create_tables` 的 `executescript` 字符串里，`eligible_markets` 表定义之后、闭合 `"""` 之前，加入：

```sql
            CREATE TABLE IF NOT EXISTS market_meta (
                condition_id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                market_slug TEXT NOT NULL DEFAULT '',
                event_slug TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
```

- [ ] **Step 4: 加读写方法**

在 `models/database.py` 的 `# --- Eligible Markets ---` 区块之后（`get_eligible_markets` 方法之后）追加：

```python
    # --- Market Meta (condition_id -> name + slugs, persistent across scans) ---

    def upsert_market_meta(
        self, condition_id: str, name: str, market_slug: str = "", event_slug: str = ""
    ):
        """Insert or update market metadata. No-op for empty condition_id."""
        if not condition_id:
            return
        c = self.conn.cursor()
        c.execute(
            """INSERT OR REPLACE INTO market_meta
            (condition_id, name, market_slug, event_slug, updated_at)
            VALUES (?, ?, ?, ?, ?)""",
            (condition_id, name or "", market_slug or "", event_slug or "", time.time()),
        )
        self.conn.commit()

    def get_market_meta(self) -> dict:
        """Return {condition_id: {name, market_slug, event_slug}}."""
        c = self.conn.cursor()
        c.execute(
            "SELECT condition_id, name, market_slug, event_slug FROM market_meta"
        )
        return {
            row["condition_id"]: {
                "name": row["name"],
                "market_slug": row["market_slug"],
                "event_slug": row["event_slug"],
            }
            for row in c.fetchall()
        }
```

（`time` 已在文件顶部 import。）

- [ ] **Step 5: 运行测试,确认通过**

Run: `pytest tests/test_database.py::TestMarketMeta -v`
Expected: 4 个 PASS。

- [ ] **Step 6: 提交**

```bash
git add models/database.py tests/test_database.py
git commit -m "feat: market_meta 持久表(condition_id -> 名称+slug)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 纯函数 `market_url` / `enrich_with_market_meta`

**Files:**
- Create: `engine/market_links.py`
- Test: `tests/test_market_links.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_market_links.py`：

```python
"""tests/test_market_links.py"""

from engine.market_links import market_url, enrich_with_market_meta


def test_market_url_prefers_market_slug():
    assert (
        market_url({"market_slug": "abc", "event_slug": "xyz"})
        == "https://polymarket.com/market/abc"
    )


def test_market_url_falls_back_to_event_slug():
    assert (
        market_url({"market_slug": "", "event_slug": "xyz"})
        == "https://polymarket.com/event/xyz"
    )


def test_market_url_empty_when_no_slug():
    assert market_url({"market_slug": "", "event_slug": ""}) == ""


def test_market_url_empty_entry():
    assert market_url(None) == ""
    assert market_url({}) == ""


def test_enrich_hit_adds_name_and_url():
    rows = [{"market": "0xc1"}]
    meta = {"0xc1": {"name": "市场A", "market_slug": "a", "event_slug": ""}}
    enrich_with_market_meta(rows, meta, "market")
    assert rows[0]["market_name"] == "市场A"
    assert rows[0]["market_url"] == "https://polymarket.com/market/a"


def test_enrich_miss_blank_url_and_name():
    rows = [{"market": "0xUNKNOWN"}]
    enrich_with_market_meta(rows, {}, "market")
    assert rows[0]["market_url"] == ""
    assert rows[0]["market_name"] == ""


def test_enrich_does_not_overwrite_existing_name():
    rows = [{"market_id": "0xc1", "market_name": "持仓Title"}]
    meta = {"0xc1": {"name": "扫描名", "market_slug": "a", "event_slug": ""}}
    enrich_with_market_meta(rows, meta, "market_id")
    assert rows[0]["market_name"] == "持仓Title"
    assert rows[0]["market_url"] == "https://polymarket.com/market/a"


def test_enrich_respects_id_key():
    rows = [{"market_id": "0xc1"}]
    meta = {"0xc1": {"name": "X", "market_slug": "s", "event_slug": ""}}
    enrich_with_market_meta(rows, meta, "market_id")
    assert rows[0]["market_url"] == "https://polymarket.com/market/s"
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `pytest tests/test_market_links.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'engine.market_links'`）。

- [ ] **Step 3: 写实现**

新建 `engine/market_links.py`：

```python
"""engine/market_links.py — 解析市场展示名 + Polymarket 链接。

纯函数(无 I/O),便于单测。condition_id -> 元信息 的映射来自
Database.get_market_meta()。
"""


def market_url(meta_entry: dict) -> str:
    """从 market_meta 条目构造 Polymarket 链接。

    market_slug 优先(/market/...),否则 event_slug(/event/...)。
    条目缺失或无 slug 时返回空串。
    """
    if not meta_entry:
        return ""
    ms = (meta_entry.get("market_slug") or "").strip()
    es = (meta_entry.get("event_slug") or "").strip()
    if ms:
        return f"https://polymarket.com/market/{ms}"
    if es:
        return f"https://polymarket.com/event/{es}"
    return ""


def enrich_with_market_meta(rows: list, meta: dict, id_key: str) -> list:
    """按 row[id_key]==condition_id 给每行补 market_name + market_url。

    - market_url:meta 命中则构造链接,否则空串。
    - market_name:仅当行里还没有非空 market_name 时才用 meta 的名字填充
      (避免覆盖持仓已有的 Data API title)。
    就地修改 rows 并返回。
    """
    meta = meta or {}
    for r in rows:
        cid = r.get(id_key, "") or ""
        entry = meta.get(cid)
        r["market_url"] = market_url(entry)
        if not r.get("market_name"):
            r["market_name"] = entry.get("name", "") if entry else ""
    return rows
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `pytest tests/test_market_links.py -v`
Expected: 8 个 PASS。

- [ ] **Step 5: 提交**

```bash
git add engine/market_links.py tests/test_market_links.py
git commit -m "feat: market_links 纯函数(链接构造+按 condition_id 补名)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: scanner 扫描时 upsert market_meta

**Files:**
- Modify: `engine/scanner.py`（`scan` 的 `for market in candidates:` 循环内，约第 108 行后）
- Test: `tests/test_scanner.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_scanner.py` 末尾追加（复用文件已有的 `_make_scanner`、`_sample_market`、`_sample_orderbook`）：

```python
def test_scan_upserts_market_meta_with_name_and_slugs():
    scanner, api, db = _make_scanner()
    api.get_rewards_markets.return_value = [
        _sample_market(market_slug="ms-1", event_slug="es-1")
    ]
    api.get_orderbook.return_value = _sample_orderbook()
    scanner.scan()
    db.upsert_market_meta.assert_any_call(
        "0xabc123", "Test Market?", "ms-1", "es-1"
    )
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `pytest tests/test_scanner.py::test_scan_upserts_market_meta_with_name_and_slugs -v`
Expected: FAIL（`db.upsert_market_meta` 从未被调用 → `AssertionError`）。

- [ ] **Step 3: 写实现**

在 `engine/scanner.py` 的 `for market in candidates:` 循环里，找到这段（约 102-108 行）：

```python
        for market in candidates:
            condition_id = market.get("condition_id", "")
            tokens = market.get("tokens", [])
            rewards_config = market.get("rewards_config", [])
            total_rate = sum(rc.get("rate_per_day", 0) for rc in rewards_config)
            end_date_str = market.get("end_date", "")
            question = market.get("question", "")
```

在 `question = market.get("question", "")` 之后紧接着插入：

```python
            # 持久化市场元信息(名称+slug),供各页显示市场名与 Polymarket 链接。
            # 该表不随扫描清空,逐次累积覆盖。
            self.db.upsert_market_meta(
                condition_id,
                question,
                market.get("market_slug", ""),
                market.get("event_slug", ""),
            )
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `pytest tests/test_scanner.py::test_scan_upserts_market_meta_with_name_and_slugs -v`
Expected: PASS。

- [ ] **Step 5: 跑整个 scanner 测试,确认无回归**

Run: `pytest tests/test_scanner.py -v`
Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```bash
git add engine/scanner.py tests/test_scanner.py
git commit -m "feat: 扫描时把市场名+slug upsert 进 market_meta

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 后端 4 个接口接入 enrich

**Files:**
- Modify: `web/routes.py`（import；`/api/orders`、`/api/positions`、`/api/actions`、`/api/monitor-status`）

本任务无自动化测试（项目无 Flask 路由测试基建；核心逻辑已在 Task 2 单测覆盖）。验证用导入冒烟 + 全套 pytest + 启动后人工查看。

- [ ] **Step 1: 加 import**

在 `web/routes.py` 顶部 import 区（`from engine.monitor_status import get_snapshot` 一行下面）加：

```python
from engine.market_links import enrich_with_market_meta
```

- [ ] **Step 2: `/api/orders` 接入**

在 `api_get_orders`（约 422 行）里，把结尾的：

```python
    return jsonify({"orders": result, "errors": errors})
```

改为：

```python
    enrich_with_market_meta(result, db.get_market_meta(), "market")
    return jsonify({"orders": result, "errors": errors})
```

- [ ] **Step 3: `/api/positions` 接入（补 condition_id + enrich）**

在 `api_get_positions`（约 524 行）里，给每个 position 的 out 字典加 `condition_id` 字段。把：

```python
                out.append(
                    {
                        "wallet": addr,
                        "market_name": p.get("title", p.get("conditionId", "")),
                        "outcome": p.get("outcome", ""),
                        "buy_price": avg,
                        "size": size,
                        "current_price": cur,
                        "stop_price": avg * (1 - sl),
                        "pnl": (cur - avg) * size,
                    }
                )
```

改为（加一行 `"condition_id"`）：

```python
                out.append(
                    {
                        "wallet": addr,
                        "market_name": p.get("title", p.get("conditionId", "")),
                        "condition_id": p.get("conditionId", ""),
                        "outcome": p.get("outcome", ""),
                        "buy_price": avg,
                        "size": size,
                        "current_price": cur,
                        "stop_price": avg * (1 - sl),
                        "pnl": (cur - avg) * size,
                    }
                )
```

然后把结尾的：

```python
    return jsonify(out)
```

改为：

```python
    enrich_with_market_meta(out, db.get_market_meta(), "condition_id")
    return jsonify(out)
```

（注：`market_name` 已由 `title` 填好,enrich 不会覆盖,只补 `market_url`。）

- [ ] **Step 4: `/api/actions` 接入**

在 `api_get_actions`（约 566 行）里，把：

```python
    return jsonify(db.get_actions(wallet, start, end, action_types))
```

改为：

```python
    rows = db.get_actions(wallet, start, end, action_types)
    enrich_with_market_meta(rows, db.get_market_meta(), "market_id")
    return jsonify(rows)
```

- [ ] **Step 5: `/api/monitor-status` 接入**

在 `api_monitor_status`（约 196 行）里，把：

```python
    return jsonify(get_snapshot())
```

改为：

```python
    snap = get_snapshot()
    enrich_with_market_meta(snap.get("rows", []), db.get_market_meta(), "market")
    return jsonify(snap)
```

- [ ] **Step 6: 导入冒烟 + 全套测试**

Run: `python -c "import web.routes; print('import ok')"`
Expected: 打印 `import ok`，无异常。

Run: `pytest`
Expected: 全部 PASS（本任务未加测试，确认没改坏既有逻辑/导入）。

- [ ] **Step 7: 提交**

```bash
git add web/routes.py
git commit -m "feat: orders/positions/actions/monitor-status 接口补市场名+链接

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 前端共用助手（app.js）+ 样式（style.css）

**Files:**
- Modify: `web/static/app.js`
- Modify: `web/static/style.css`

无自动化测试。验证：启动 app 后在浏览器控制台调用 `marketCell('名','0x1234567890abcdef',' ')` 检查返回 HTML、点 📋 看复制。

- [ ] **Step 1: 写 app.js 助手**

把 `web/static/app.js` 全文替换为：

```javascript
/* web/static/app.js — Shared utilities */

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

let _toastTimer = null;
function showToast(msg) {
  let el = document.getElementById('app-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'app-toast';
    el.className = 'app-toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 1500);
}

function fallbackCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand('copy'); if (done) done(); }
  catch (e) { alert('复制失败，请从悬浮提示手动复制'); }
  document.body.removeChild(ta);
}

function copyCid(cid) {
  if (!cid) return;
  const ok = () => showToast('已复制 condition ID');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(cid).then(ok).catch(() => fallbackCopy(cid, ok));
  } else {
    fallbackCopy(cid, ok);
  }
}

// 市场单元格:名称(有链接则 <a>,无则纯文本;名称缺失显示截断 condition_id)
// + 📋 复制完整 condition_id 按钮。
function marketCell(name, conditionId, url) {
  const cid = conditionId || '';
  const label = name || (cid ? cid.slice(0, 8) + '...' + cid.slice(-6) : '');
  const safe = escapeHtml(label);
  const inner = url
    ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${safe}</a>`
    : safe;
  const copyBtn = cid
    ? ` <button class="btn-xs" title="复制完整 condition ID"` +
      ` onclick="copyCid('${escapeHtml(cid)}')">📋</button>`
    : '';
  return `<span title="${escapeHtml(cid)}">${inner}${copyBtn}</span>`;
}
```

- [ ] **Step 2: 加 CSS**

在 `web/static/style.css` 末尾追加（sticky 表头背景沿用现有 `.data-table th` 的 `#f8f9fa`）：

```css
.table-scroll { max-height: 42vh; overflow: auto; margin-bottom: 24px;
  border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.table-scroll .data-table { margin-bottom: 0; box-shadow: none; border-radius: 0; }
.data-table thead th { position: sticky; top: 0; z-index: 1; background: #f8f9fa; }
.btn-xs { padding: 0 4px; font-size: 13px; line-height: 1; cursor: pointer;
  background: transparent; border: none; color: inherit; }
.btn-xs:hover { opacity: 0.7; }
.app-toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  background: #1a1a2e; color: #fff; padding: 8px 16px; border-radius: 6px;
  opacity: 0; pointer-events: none; transition: opacity .2s; z-index: 1000;
  font-size: 13px; }
.app-toast.show { opacity: 1; }
```

- [ ] **Step 3: 人工冒烟**

启动 `python app.py`，登录后打开任意页面，在浏览器控制台执行：

```js
document.body.insertAdjacentHTML('beforeend', marketCell('测试市场','0x1234567890abcdef1234',''))
```

Expected: 页面底部出现「测试市场 📋」，点 📋 弹出「已复制 condition ID」toast，剪贴板为完整 `0x1234567890abcdef1234`。

- [ ] **Step 4: 提交**

```bash
git add web/static/app.js web/static/style.css
git commit -m "feat: 前端共用 marketCell/复制助手 + 限高滚动/sticky 表头样式

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: orders.html — 拆滚动区 + marketCell

**Files:**
- Modify: `web/templates/orders.html`

无自动化测试。验证:启动后打开「订单管理」。

- [ ] **Step 1: 包滚动容器**

把 `web/templates/orders.html` 里「当前挂单」的 `<table class="data-table"> ... </table>`（约 28-39 行）整段用 `<div class="table-scroll">` 包起来：

```html
<div class="table-scroll">
<table class="data-table">
  <thead><tr>
    <th><input type="checkbox" id="select-all" onchange="toggleAll(this)"></th>
    <th>钱包</th><th>市场</th><th>方向</th><th>Outcome</th>
    <th onclick="sortBy('price')">价格</th>
    <th onclick="sortBy('original_size')">原始数量</th>
    <th>已成交</th>
    <th onclick="sortBy('scoring')">奖励区间</th>
    <th onclick="sortBy('created_at')">时间</th><th>操作</th>
  </tr></thead>
  <tbody id="orders-body"></tbody>
</table>
</div>
```

把「当前持仓」的 `<table class="data-table"> ... </table>`（约 42-46 行）同样包起来：

```html
<div class="table-scroll">
<table class="data-table">
  <thead><tr><th>钱包</th><th>市场</th><th>买入价</th><th>当前价</th>
  <th>止损价</th><th>盈亏</th></tr></thead>
  <tbody id="positions-body"></tbody>
</table>
</div>
```

- [ ] **Step 2: 挂单市场列用 marketCell**

在 `renderOrders()` 里，把：

```javascript
    <td>${o.market||''}</td><td>${o.side}</td><td>${o.outcome||''}</td>
```

改为：

```javascript
    <td>${marketCell(o.market_name, o.market, o.market_url)}</td><td>${o.side}</td><td>${o.outcome||''}</td>
```

- [ ] **Step 3: 持仓市场列用 marketCell**

在 positions 渲染（`refreshOrders` 里的 `/api/positions` 回调）里，把：

```javascript
      <td>${p.market_name}</td><td>${(p.buy_price||0).toFixed(4)}</td>
```

改为：

```javascript
      <td>${marketCell(p.market_name, p.condition_id, p.market_url)}</td><td>${(p.buy_price||0).toFixed(4)}</td>
```

- [ ] **Step 4: 人工验证**

启动 `python app.py`，登录后打开「订单管理」。
Expected:
- 「当前挂单」「当前持仓」各在一个限高框里独立滚动，挂单很多时持仓仍一眼可见；表头滚动时固定。
- 挂单/持仓「市场」列显示市场名(有 slug 则为可点链接,新开标签到 Polymarket)，名字旁有 📋；点 📋 toast 提示已复制，粘贴得到完整 condition_id。
- 名称解析不到的(未扫描过)显示截断 id、无链接，📋 仍可复制完整 id。

- [ ] **Step 5: 提交**

```bash
git add web/templates/orders.html
git commit -m "feat: 订单管理页挂单/持仓各自限高滚动 + 市场名/链接/复制

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: logs.html — 拆滚动区 + marketCell

**Files:**
- Modify: `web/templates/logs.html`

无自动化测试。验证:启动后打开「监控状态」。

- [ ] **Step 1: 删除重复的 escapeHtml**

`web/templates/logs.html` 顶部 `<script>` 里有一份本地 `escapeHtml`（约 56-59 行）：

```javascript
function escapeHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
```

删除这 4 行——改用 app.js 里的全局 `escapeHtml`（功能相同且额外转义引号，app.js 在本页脚本之前加载）。

- [ ] **Step 2: 包滚动容器**

把「监控状态」的 `<table class="data-table"> ... </table>`（约 14-23 行）用 `<div class="table-scroll">` 包起来：

```html
<div class="table-scroll">
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
</div>
```

把「操作记录」的 `<table class="data-table" style="table-layout:fixed;width:100%"> ... </table>`（约 37-51 行，含 `<colgroup>`）整段用 `<div class="table-scroll">` 包起来（保留 table 上的 `style` 和 colgroup 不变，只在外层加 div）。

- [ ] **Step 3: 监控状态市场列用 marketCell**

在 `refreshStatus()` 里，把：

```javascript
                <td>${escapeHtml(r.market)}</td>
```

改为：

```javascript
                <td>${marketCell(r.market_name, r.market, r.market_url)}</td>
```

- [ ] **Step 4: 操作记录市场列用 marketCell**

在 `refreshActions()` 里，把：

```javascript
                <td title="${escapeHtml(a.market_id)}">${escapeHtml(a.market_id.slice(0,8) + '...' + a.market_id.slice(-6))}</td>
```

改为：

```javascript
                <td>${marketCell(a.market_name, a.market_id, a.market_url)}</td>
```

- [ ] **Step 5: 人工验证**

启动 `python app.py`，登录后打开「监控状态」（最好引擎运行中、有挂单/操作记录）。
Expected:
- 「监控状态」「操作记录」各自限高独立滚动，监控状态行多时操作记录仍一眼可见；表头固定。
- 两张表「市场」列都显示市场名(有链接可跳转 Polymarket)+ 📋 复制完整 condition_id。
- 其余列（时间/钱包/动作等）显示正常（确认删掉本地 escapeHtml 后没报错）。

- [ ] **Step 6: 提交**

```bash
git add web/templates/logs.html
git commit -m "feat: 监控状态页监控/操作记录各自限高滚动 + 市场名/链接/复制

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8（可选）: dashboard eligible 表加链接 + 复制

**Files:**
- Modify: `web/routes.py`（`/api/eligible`）、`web/templates/dashboard.html`

一致性增强；若不需要可跳过整个 Task。无自动化测试。

- [ ] **Step 1: `/api/eligible` 补 market_url**

在 `api_eligible_markets`（约 580 行）里，两个返回 `markets` 的分支（`if not manager` 分支和主分支）在 `return jsonify(...)` 之前，对 `markets` 调用 enrich。具体：在每个 `return jsonify({... "markets": markets ...})` 之前加一行

```python
    enrich_with_market_meta(markets, db.get_market_meta(), "market_id")
```

（eligible 条目的 `market_id` 即 condition_id；条目已有 `market_name`，enrich 只补 `market_url`。）

- [ ] **Step 2: dashboard 市场名称列用 marketCell**

在 `web/templates/dashboard.html` 的 `renderEligibleTable()` 里，把：

```javascript
            <td title="${m.market_id}">${m.market_name}</td>
```

改为：

```javascript
            <td>${marketCell(m.market_name, m.market_id, m.market_url)}</td>
```

- [ ] **Step 3: 导入冒烟 + 人工验证**

Run: `python -c "import web.routes; print('import ok')"`
Expected: `import ok`。

启动后打开「仪表盘」，eligible 表「市场名称」列为可点链接 + 📋。

- [ ] **Step 4: 提交**

```bash
git add web/routes.py web/templates/dashboard.html
git commit -m "feat: 仪表盘 eligible 表市场名加链接+复制(一致性)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage：**
- 第1节 数据层(market_meta 表+方法+不随扫描清空) → Task 1 + Task 3 ✓
- 第2节 后端 enrich(market_url/enrich_with_market_meta + 4 接口接入, monitor 代码不动) → Task 2 + Task 4 ✓
- 第3节 前端(marketCell+复制+限高滚动+sticky 表头;orders/logs 两页) → Task 5 + Task 6 + Task 7 ✓
- 第3节 可选 dashboard eligible → Task 8 ✓
- 第4节 边界(未命中空链接/名称回退、enrich 不覆盖已有名、复制回退) → Task 2 实现 + 测试(`test_enrich_miss_blank_url_and_name`/`test_enrich_does_not_overwrite_existing_name`) + Task 5 fallbackCopy ✓
- 测试(database/scanner/market_links) → Task 1/2/3 各自带测试 ✓

**2. 占位符扫描：** 无 TBD/TODO；每个代码步骤都给了完整代码与确切命令。CSS sticky 背景用了具体值 `#f8f9fa`（来自现有 `.data-table th`）。✓

**3. 类型/签名一致：**
- `upsert_market_meta(condition_id, name, market_slug="", event_slug="")` — Task 1 定义、Task 3 以 4 个位置参数调用、Task 1 测试以 4 位置参数断言，一致。✓
- `get_market_meta() -> {condition_id: {name, market_slug, event_slug}}` — Task 1 定义,Task 4 各接口调用,一致。✓
- `enrich_with_market_meta(rows, meta, id_key)` — Task 2 定义；Task 4 用 `"market"`/`"condition_id"`/`"market_id"`/`"market"`、Task 8 用 `"market_id"`，均与各接口数据里的 condition_id 字段名匹配（orders→`market`,positions→新增`condition_id`,actions→`market_id`,monitor-status→`market`,eligible→`market_id`）。✓
- 前端 `marketCell(name, conditionId, url)` — Task 5 定义；Task 6/7/8 调用时实参顺序一致（名称, condition_id, url）。✓
