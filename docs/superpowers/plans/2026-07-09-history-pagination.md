# 历史页服务端分页 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 历史页改为服务端分页,一次只拉/渲染一页(100 条),修复动作过多时浏览器主线程卡死。

**Architecture:** `db.get_actions` 加可选 `limit/offset`(默认 None=不加 LIMIT,保持 `monitor.py` 水位播种与旧测试);新增 `db.count_actions` 复用同一 WHERE 返回总数;`/api/actions` 按 `page/page_size` 分页,响应改为 `{rows,total,page,page_size}`;历史页加上一页/下一页控件,每页只渲染 100 行。

**Tech Stack:** Python 3、pytest、Flask、SQLite、原生 JS(无前端框架)。

## Global Constraints

- `get_actions` 必须**向后兼容**:`limit is None` → **不加** `LIMIT`/`OFFSET`(`engine/monitor.py:103` 水位播种需全量,现有无 limit 单测须仍绿)。
- `/api/actions` 响应体从裸数组改为 `{"rows":[...],"total":int,"page":int,"page_size":int}`;唯一消费者是 `web/templates/history.html`,同批改到位。
- `page` 默认 1、下界钳 1;`page_size` 默认 100、钳到 [1, 500]。
- 不动 actions 记录逻辑(`cancel_remainder` 照记)、`_enrich_rows`/`ensure_market_meta`、`monitor.py`。
- `history.html` 含中文,由**主 agent 直接编辑**(subagent 易写别字+BOM);写后校验无 BOM、中文正确。
- `pytest` 全绿是每个任务的闸。

---

### Task 1: db 层分页 —— `get_actions` 加 limit/offset + `count_actions`

**Files:**
- Modify: `models/database.py`(`get_actions` 当前 `593-618`)
- Test: `tests/test_database.py`(`TestActions` 类,现有 actions 用例在 `~296-348`)

**Interfaces:**
- Produces:
  - `get_actions(self, wallet=None, start=None, end=None, action_types=None, limit=None, offset=0) -> list[dict]`(顺序 `created_at DESC, id DESC` 不变;`limit is None` 时无 LIMIT)
  - `count_actions(self, wallet=None, start=None, end=None, action_types=None) -> int`
  - 私有 `_actions_filter(self, wallet, start, end, action_types) -> (where_sql:str, params:list)` 供两者复用

- [ ] **Step 1: 写失败测试**

在 `tests/test_database.py` 的 `TestActions` 类(与现有 `test_get_actions_*` 同类)末尾追加:

```python
    def test_get_actions_limit_returns_most_recent(self, db):
        for i in range(5):
            db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.1, 1, f"a{i}", "b")
        rows = db.get_actions(limit=2)
        assert [r["reason"] for r in rows] == ["a4", "a3"]

    def test_get_actions_limit_offset_slices(self, db):
        for i in range(5):
            db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.1, 1, f"a{i}", "b")
        rows = db.get_actions(limit=2, offset=2)
        assert [r["reason"] for r in rows] == ["a2", "a1"]

    def test_get_actions_no_limit_returns_all(self, db):
        for i in range(5):
            db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.1, 1, f"a{i}", "b")
        assert len(db.get_actions()) == 5

    def test_count_actions_total_and_filters(self, db):
        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.3, 1, "r", "b")
        db.record_action("0xA", "m", "cancel_remainder", "-", -1, 0, "r", "b")
        db.record_action("0xB", "m", "cancel_remainder", "-", -1, 0, "r", "b")
        assert db.count_actions() == 3
        assert db.count_actions(wallet="0xA") == 2
        assert db.count_actions(action_types=["cancel_remainder"]) == 2
        assert db.count_actions(wallet="0xA", action_types=["cancel_remainder"]) == 1
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `pytest tests/test_database.py -k "limit or offset or count_actions" -v`
Expected: FAIL —— `get_actions() got an unexpected keyword argument 'limit'` / `AttributeError: 'Database' object has no attribute 'count_actions'`。

- [ ] **Step 3: 实现**

`models/database.py`,把现有 `get_actions`(当前 `593-618`)整段替换,并在其后新增 `_actions_filter` 与 `count_actions`:

```python
    def _actions_filter(self, wallet, start, end, action_types):
        """构造 actions 查询的 WHERE 子句 + 参数(get_actions / count_actions 共用)。"""
        clause = "WHERE 1=1"
        params = []
        if wallet:
            clause += " AND wallet = ?"
            params.append(wallet)
        if start:
            clause += " AND created_at >= ?"
            params.append(start)
        if end:
            clause += " AND created_at <= ?"
            params.append(end)
        if action_types:
            placeholders = ",".join("?" * len(action_types))
            clause += f" AND action_type IN ({placeholders})"
            params.extend(action_types)
        return clause, params

    def get_actions(
        self,
        wallet: str = None,
        start: float = None,
        end: float = None,
        action_types: list[str] = None,
        limit: int = None,
        offset: int = 0,
    ) -> list[dict]:
        clause, params = self._actions_filter(wallet, start, end, action_types)
        query = f"SELECT * FROM actions {clause} ORDER BY created_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params = params + [int(limit), int(offset)]
        c = self.conn.cursor()
        c.execute(query, params)
        return [dict(row) for row in c.fetchall()]

    def count_actions(
        self,
        wallet: str = None,
        start: float = None,
        end: float = None,
        action_types: list[str] = None,
    ) -> int:
        clause, params = self._actions_filter(wallet, start, end, action_types)
        c = self.conn.cursor()
        c.execute(f"SELECT COUNT(*) FROM actions {clause}", params)
        return c.fetchone()[0]
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `pytest tests/test_database.py -v`
Expected: PASS(新 4 例 + 现有 `test_get_actions_*` 全绿——向后兼容)。

- [ ] **Step 5: 提交**

```bash
git add models/database.py tests/test_database.py
git commit -m "feat(db): get_actions 加 limit/offset + count_actions,历史分页地基"
```

---

### Task 2: 路由分页 —— `/api/actions` 按 page/page_size 返回 {rows,total,...}

**Files:**
- Modify: `web/routes.py`(`api_get_actions` 当前 `882-892`)
- Test: `tests/test_history_routes.py`(新建)

**Interfaces:**
- Consumes: `db.count_actions`、`db.get_actions(..., limit=, offset=)`(Task 1)。
- Produces: `GET /api/actions?...&page=&page_size=` → JSON `{"rows":[...],"total":int,"page":int,"page_size":int}`;`page` 钳 ≥1,`page_size` 钳 [1,500] 默认 100。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_history_routes.py`:

```python
"""tests/test_history_routes.py — /api/actions 分页契约。"""

import web.routes as routes
from models.database import Database


def _client_with_db(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    # 避免 _enrich_rows 的 Gamma 补名打网络:桩掉返回空(走负缓存)。
    monkeypatch.setattr(routes, "_gamma_fetch", lambda cids: {})
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client, db


def _seed(db, n):
    for i in range(n):
        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.1, 1, f"a{i}", "b")


def test_actions_page1_shape_and_slice(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    _seed(db, 5)
    data = client.get("/api/actions?page=1&page_size=2").get_json()
    assert data["total"] == 5
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert [r["reason"] for r in data["rows"]] == ["a4", "a3"]


def test_actions_page2_slice(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    _seed(db, 5)
    data = client.get("/api/actions?page=2&page_size=2").get_json()
    assert [r["reason"] for r in data["rows"]] == ["a2", "a1"]


def test_actions_out_of_range_page_empty(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    _seed(db, 1)
    data = client.get("/api/actions?page=99&page_size=100").get_json()
    assert data["rows"] == []
    assert data["total"] == 1


def test_actions_default_page_size_100(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    _seed(db, 3)
    data = client.get("/api/actions").get_json()
    assert data["page"] == 1
    assert data["page_size"] == 100
    assert len(data["rows"]) == 3
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `pytest tests/test_history_routes.py -v`
Expected: FAIL —— 现路由 `jsonify(rows)` 返回裸数组,`data["total"]` 抛 `TypeError: list indices must be integers`(或 KeyError)。

- [ ] **Step 3: 实现**

`web/routes.py` 的 `api_get_actions`(当前 `882-892`)整体替换为:

```python
@app.route("/api/actions", methods=["GET"])
@login_required
def api_get_actions():
    wallet = request.args.get("wallet")
    start = request.args.get("start", type=float)
    end = request.args.get("end", type=float)
    types = request.args.get("types")
    action_types = types.split(",") if types else None
    page = max(1, request.args.get("page", default=1, type=int) or 1)
    page_size = request.args.get("page_size", default=100, type=int) or 100
    page_size = min(500, max(1, page_size))
    total = db.count_actions(wallet, start, end, action_types)
    rows = db.get_actions(
        wallet,
        start,
        end,
        action_types,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    _enrich_rows(rows, "market_id")
    return jsonify(
        {"rows": rows, "total": total, "page": page, "page_size": page_size}
    )
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `pytest tests/test_history_routes.py -v`
Expected: PASS(4 例全绿)。

- [ ] **Step 5: 全量回归**

Run: `pytest -q`
Expected: PASS(确认没有别的消费者依赖 `/api/actions` 的裸数组响应)。

- [ ] **Step 6: 提交**

```bash
git add web/routes.py tests/test_history_routes.py
git commit -m "feat(api): /api/actions 分页返回 {rows,total,page,page_size}"
```

---

### Task 3: 历史页分页控件(主 agent 直接改)

**Files:**
- Modify: `web/templates/history.html`(筛选 `onchange` 当前 `8-19`;表格容器收尾 `38`;脚本 `refreshActions` `71-102`、初始调用 `124`)

**Interfaces:**
- Consumes: `/api/actions` 返回 `{rows,total,page,page_size}`(Task 2)。

> ⚠️ 含中文,由**主 agent** 用 Edit 直接改;写后 `grep` 校验中文正确 + `head -c 3 | xxd` 确认无 BOM。前端无 JS 单测,靠人工走查/`verify`。

- [ ] **Step 1: 加分页控件 HTML**

在表格容器 `</div>`(当前 `38` 行,`<div class="table-scroll">` 的闭合)之后、`{% endblock %}`(`39`)之前插入:

```html
<div class="pager" id="pager" style="margin-top:10px;display:none">
    <button class="btn btn-sm" id="prev-page" onclick="prevPage()">上一页</button>
    <span id="page-info" style="margin:0 10px"></span>
    <button class="btn btn-sm" id="next-page" onclick="nextPage()">下一页</button>
</div>
```

- [ ] **Step 2: 筛选 onchange/按钮 改为 reload()(回第 1 页)**

把筛选栏里 4 处 `onchange="refreshActions()"`(wallet-filter / act-type / start-date / end-date)和「刷新」按钮的 `onclick="refreshActions()"` 全部改为 `reload()`(共 5 处,`replace_all` 安全,因为脚本区没有别的字面 `refreshActions()` 调用点用这个字符串——底部初始调用是单独的 `refreshActions();`,见 Step 3 不改它)。

> 注意:底部初始加载那行是 `refreshActions();`(带分号、无 `onchange/onclick`),不要误改;只改 `onchange="refreshActions()"` / `onclick="refreshActions()"` 这种带引号属性的。

- [ ] **Step 3: 替换 `refreshActions` + 加分页逻辑**

把 `refreshActions` 函数(当前 `71-102`)整体替换为下面这段(顶部加分页状态与 `reload/prevPage/nextPage`;行模板与 `escapeHtml/marketCell/ACTION_LABELS` 原样):

```javascript
const PAGE_SIZE = 100;
let currentPage = 1;
let totalPages = 1;

function reload() { currentPage = 1; refreshActions(); }
function prevPage() { if (currentPage > 1) { currentPage--; refreshActions(); } }
function nextPage() { if (currentPage < totalPages) { currentPage++; refreshActions(); } }

function refreshActions() {
    const wallet = document.getElementById('wallet-filter').value;
    const type = document.getElementById('act-type').value;
    const s = document.getElementById('start-date').value;
    const e = document.getElementById('end-date').value;
    const params = new URLSearchParams();
    if (wallet) params.set('wallet', wallet);
    if (type) params.set('types', type);
    if (s) params.set('start', new Date(s).getTime() / 1000);
    if (e) params.set('end', new Date(e + 'T23:59:59').getTime() / 1000);
    params.set('page', currentPage);
    params.set('page_size', PAGE_SIZE);

    fetch(`/api/actions?${params}`).then(r => r.json()).then(data => {
        const rows = data.rows || [];
        const total = data.total || 0;
        totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
        const body = document.getElementById('actions-body');
        const pager = document.getElementById('pager');
        if (!total) {
            body.innerHTML = '<tr><td colspan="9">暂无动作记录</td></tr>';
            pager.style.display = 'none';
            return;
        }
        body.innerHTML = rows.map(a => `
            <tr>
                <td>${escapeHtml(new Date(a.created_at * 1000).toLocaleString('zh-CN'))}</td>
                <td title="${escapeHtml(a.wallet)}">${escapeHtml(shortWallet(a.wallet))}</td>
                <td>${marketCell(a.market_name, a.market_id, a.market_url)}</td>
                <td class="${actionClass(a.action_type)}">${escapeHtml(ACTION_LABELS[a.action_type] || a.action_type)}</td>
                <td>${escapeHtml(a.side)}</td>
                <td>${a.price < 0 ? '-' : escapeHtml(a.price.toFixed(4))}</td>
                <td>${escapeHtml(a.size)}</td>
                <td>${escapeHtml(a.reason)}</td>
                <td>${escapeHtml(a.price_basis)}</td>
            </tr>
        `).join('');
        pager.style.display = '';
        document.getElementById('page-info').textContent =
            `第 ${currentPage} / ${totalPages} 页（共 ${total} 条）`;
        document.getElementById('prev-page').disabled = currentPage <= 1;
        document.getElementById('next-page').disabled = currentPage >= totalPages;
    });
}
```

- [ ] **Step 4: 校验无 BOM + 中文 + 无残留全量渲染**

Run:
```bash
head -c 3 web/templates/history.html | xxd            # 期望非 ef bb bf
grep -n "上一页\|下一页\|共 \|reload()" web/templates/history.html   # 命中新控件/函数、中文正确
grep -n "then(rows =>" web/templates/history.html      # 期望无输出:旧的 .then(rows=>) 全量渲染已替掉
```
Expected: 无 BOM;新控件/`reload()` 命中;`then(rows =>` 无残留。

- [ ] **Step 5: 全量回归 + 人工走查**

Run: `pytest -q`
Expected: PASS(前端改动不影响单测)。

人工(`verify`):启动 app、登录、开历史页 → 秒开不卡;显示「第 1 / N 页(共 M 条)」;点下一页/上一页可翻且到边界按钮禁用;按「挂买单」筛选后回第 1 页、翻页能找到具体挂单记录。

- [ ] **Step 6: 提交**

```bash
git add web/templates/history.html
git commit -m "feat(history): 历史页服务端分页控件,修动作过多卡死"
```

---

## 收尾(全部任务后)

- [ ] `pytest` 全绿。
- [ ] 人工验收:动作较多的库上开历史页秒开不卡、翻页正常、筛选后回第 1 页。
- [ ] 合并到 `main`(`superpowers:finishing-a-development-branch`)。此为 bug 修复(展示层),按 `docs/版本号规范.md` 属**修订号**;发版待用户确认。

## Self-Review 记录

- **Spec 覆盖:** 后端①(get_actions limit/offset)→ Task 1;②(count_actions)→ Task 1;③(/api/actions 分页)→ Task 2;前端④(分页控件)→ Task 3;不动项(记录/enrich/monitor)未被任何任务触碰;测试 1-7 分布于 Task 1(db)/Task 2(route)。全覆盖。
- **占位符:** 无 TBD/TODO;每步含实代码或实命令。
- **类型一致:** `get_actions(..., limit=None, offset=0)`、`count_actions(...)`、`_actions_filter` 在 Task 1 定义,Task 2 路由按 `limit=/offset=` 调用一致;响应键 `rows/total/page/page_size` 在 Task 2 产出、Task 3 前端消费一致。
