# 钱包地址备注 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给每个导入的钱包加一个可编辑的「备注」,并在所有显示钱包地址的界面一并显示,便于一眼认出是哪个号。

**Architecture:** 把现有「代理(proxy)」字段那套原样复刻:`wallets` 加 `remark` 列 + 幂等迁移、`set_wallet_remark`、`PUT /api/wallets/<addr>/remark`、配置页 `prompt()` 编辑。显示侧在共享 `web/static/app.js` 加两个工具 `shortAddr`/`walletLabel`,5 个模板复用;备注纯展示,不进任何交易/引擎决策。

**Tech Stack:** Python 3、Flask、SQLite、pytest;前端为 Jinja 模板内联 JS + 共享 `app.js`(base.html 引入,全页可用 `escapeHtml`/`showToast`)。

## Global Constraints

- 备注**纯展示**,不参与筛选/排序/交易/挂单决策,不碰引擎、不清 `_api_cache`。
- 备注是用户自由文本:渲染进 `innerHTML` 或 `title="…"` 前必须 `escapeHtml`(用 `textContent` 赋值的路径天然安全);后端 `.strip()` 并**截断到 40 字**。
- 显示约定:**有备注显示备注、无则短地址(`0x1234...abcd`),完整地址永远进 `title`**;下拉选项在有备注时显示 `备注 (0x12…cd)` 便于按地址对齐。
- 前端含中文模板由主会话直接改(subagent 易把中文写成别字 + 加 BOM);所有面向用户文案简体中文。
- 复刻代理那套写法,保持与现有代码风格一致(surgical:不改无关的既有 proxy/funder/address 单元格)。

---

## File Structure

- `models/database.py`(改):`wallets` 建表 + 迁移加 `remark` 列;`add_wallet` 加 `remark` 形参;新增 `set_wallet_remark`;`list_wallets` SELECT 带 `remark`。
- `web/routes.py`(改):`PUT /api/wallets/<address>/remark`;`api_add_wallet` 接可选 `remark`;`/api/dashboard` 的 `wallet_summaries` 带上 `remark`。
- `web/static/app.js`(改):新增共享 `shortAddr(addr)` + `walletLabel(remark, addr)`。
- `web/templates/config.html`(改):导入输入 + 表头「备注」列 + 行渲染 + `editRemark` + `finalizeImport` 带 remark。
- `web/templates/dashboard.html`、`networth.html`、`history.html`、`logs.html`(改):用 `walletLabel` 显示备注。
- `tests/test_database.py`(改):DB 层备注测试。
- `tests/test_wallet_remark_routes.py`(建):路由测试。

---

## Task 1: 数据层 — remark 列 + 迁移 + 读写

**Files:**
- Modify: `models/database.py`(建表约 68-76;迁移约 195-207;`add_wallet` 约 403-418;`set_wallet_proxy` 后约 424;`list_wallets` 约 438-444)
- Test: `tests/test_database.py`(钱包测试区约 162-201;迁移测试区约 203-227)

**Interfaces:**
- Produces:
  - `add_wallet(address, encrypted_key, funder="", signature_type=2, proxy="", remark="")`
  - `set_wallet_remark(address: str, remark: str) -> None`
  - `list_wallets()` 每条 dict 含 `"remark"` 键。

- [ ] **Step 1: 写失败测试(加到 `tests/test_database.py` 的 `TestWallets` 类,紧随现有 proxy 测试)**

```python
    def test_remark_defaults_to_empty(self, db):
        db.add_wallet("0xABC", "enc")
        assert db.list_wallets()[0]["remark"] == ""

    def test_add_wallet_stores_remark(self, db):
        db.add_wallet("0xABC", "enc", remark="主号")
        assert db.list_wallets()[0]["remark"] == "主号"

    def test_set_wallet_remark_updates(self, db):
        db.add_wallet("0xABC", "enc")
        db.set_wallet_remark("0xABC", "小号2")
        assert db.list_wallets()[0]["remark"] == "小号2"

    def test_migration_adds_remark_to_old_db(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "old.db")
        # 模拟老库:wallets 表没有 remark 列
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE wallets (address TEXT PRIMARY KEY, encrypted_key TEXT NOT NULL,"
            " funder TEXT NOT NULL DEFAULT '', signature_type INTEGER NOT NULL DEFAULT 2,"
            " proxy TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,"
            " created_at REAL NOT NULL DEFAULT (strftime('%s','now')))"
        )
        conn.execute("INSERT INTO wallets (address, encrypted_key) VALUES ('0xOLD', 'enc')")
        conn.commit()
        conn.close()

        database = Database(db_path)
        database.init()  # 应迁移补 remark 列,默认 ''
        try:
            w = database.list_wallets()[0]
            assert w["address"] == "0xOLD"
            assert w["remark"] == ""
            database.set_wallet_remark("0xOLD", "回填")
            assert database.list_wallets()[0]["remark"] == "回填"
        finally:
            database.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_database.py -k "remark" -v`
Expected: FAIL —— `list_wallets()` 结果无 `remark` 键(KeyError)、`set_wallet_remark`/`add_wallet(remark=...)` 不存在。

- [ ] **Step 3: 建表加列**

`models/database.py` 的 `CREATE TABLE IF NOT EXISTS wallets`,在 `proxy` 行后、`enabled` 行前加一列:

```sql
                proxy TEXT NOT NULL DEFAULT '',
                remark TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
```

- [ ] **Step 4: 迁移补列(幂等,复用已读的 `cols`)**

在 proxy 的迁移块之后(约 205-207,`if "proxy" not in cols:` 那段后面)加:

```python
        if "remark" not in cols:
            c.execute("ALTER TABLE wallets ADD COLUMN remark TEXT NOT NULL DEFAULT ''")
            self.conn.commit()
```

- [ ] **Step 5: `add_wallet` 加形参 + INSERT 带 remark**

```python
    def add_wallet(
        self,
        address: str,
        encrypted_key: str,
        funder: str = "",
        signature_type: int = 2,
        proxy: str = "",
        remark: str = "",
    ):
        c = self.conn.cursor()
        c.execute(
            "INSERT INTO wallets (address, encrypted_key, funder, signature_type, proxy, remark) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (address, encrypted_key, funder, signature_type, proxy, remark),
        )
        self.conn.commit()
```

- [ ] **Step 6: 新增 `set_wallet_remark`(放在 `set_wallet_proxy` 之后)**

```python
    def set_wallet_remark(self, address: str, remark: str):
        """更新某钱包的备注(纯展示,不影响任何交易/API 客户端)。"""
        c = self.conn.cursor()
        c.execute("UPDATE wallets SET remark = ? WHERE address = ?", (remark, address))
        self.conn.commit()
```

- [ ] **Step 7: `list_wallets` SELECT 带 remark**

```python
        c.execute(
            "SELECT address, encrypted_key, funder, signature_type, proxy, remark, enabled, "
            "created_at, template_id FROM wallets"
        )
```

- [ ] **Step 8: 跑测试确认通过 + 全量零回归**

Run: `pytest tests/test_database.py -k "remark" -v` → 期望 4 项通过。
Run: `pytest -q` → 期望全绿。

- [ ] **Step 9: 提交**

```bash
git add models/database.py tests/test_database.py
git commit -m "$(cat <<'EOF'
feat(db): wallets 加 remark 列(备注)+ 幂等迁移 + set_wallet_remark

复刻 proxy 那套:建表加列、老库 ALTER 补列、add_wallet 接 remark、list_wallets 回显。
备注纯展示,不进任何交易决策。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 路由 — PUT 备注 + 导入带备注 + dashboard 回显

**Files:**
- Modify: `web/routes.py`(`api_add_wallet` 约 523-580;`api_set_wallet_proxy` 后约 604;`api_dashboard` 的 `wallet_summaries` 约 1218-1226)
- Test: `tests/test_wallet_remark_routes.py`(新建)

**Interfaces:**
- Consumes: Task 1 的 `set_wallet_remark` / `add_wallet(remark=)` / `list_wallets` 带 `remark`。
- Produces: `PUT /api/wallets/<address>/remark`(body `{remark}`,`.strip()[:40]`);`/api/wallets` 与 `/api/dashboard` 响应每钱包含 `remark`。

- [ ] **Step 1: 写失败测试(新建 `tests/test_wallet_remark_routes.py`,仿 test_wallet_proxy_routes.py)**

```python
"""tests/test_wallet_remark_routes.py — 每钱包备注的路由:编辑 + 列表回显 + 截断。"""

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


def test_put_remark_updates_wallet(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.add_wallet("0xABC", "enc")
    r = client.put("/api/wallets/0xABC/remark", json={"remark": "主号"})
    assert r.status_code == 200
    assert db.list_wallets()[0]["remark"] == "主号"


def test_put_empty_remark_clears(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.add_wallet("0xABC", "enc", remark="主号")
    client.put("/api/wallets/0xABC/remark", json={"remark": ""})
    assert db.list_wallets()[0]["remark"] == ""


def test_put_remark_truncated_to_40(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.add_wallet("0xABC", "enc")
    client.put("/api/wallets/0xABC/remark", json={"remark": "x" * 50})
    assert db.list_wallets()[0]["remark"] == "x" * 40


def test_list_wallets_returns_remark(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.add_wallet("0xABC", "enc", remark="小号2")
    wallets = client.get("/api/wallets").get_json()
    assert wallets[0]["remark"] == "小号2"
    assert "encrypted_key" not in wallets[0]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_wallet_remark_routes.py -v`
Expected: FAIL —— `PUT /api/wallets/<addr>/remark` 路由不存在(404),`/api/wallets` 回显无 `remark`(Task 1 若已合并则 list 有 remark;本步仍因 PUT 404 失败)。

- [ ] **Step 3: 加 PUT 备注路由(放在 `api_set_wallet_proxy` 之后)**

```python
@app.route("/api/wallets/<address>/remark", methods=["PUT"])
@login_required
def api_set_wallet_remark(address):
    """设置/清空某钱包备注(纯展示,不影响任何 API 客户端;空串=清空,截断到 40 字)。"""
    remark = ((request.get_json() or {}).get("remark") or "").strip()[:40]
    db.set_wallet_remark(address, remark)
    return jsonify({"ok": True})
```

- [ ] **Step 4: 导入接可选 remark**

`api_add_wallet` 里,读 proxy 那行(约 539)之后加:

```python
    proxy = (data.get("proxy") or "").strip()
    remark = (data.get("remark") or "").strip()[:40]
```

并把落库那行(约 573)改为带 remark:

```python
        db.add_wallet(address, encrypted, funder, sig_type, proxy=proxy, remark=remark)
```

- [ ] **Step 5: `/api/dashboard` 回显 remark**

`api_dashboard` 的 `wallet_summaries.append({...})`(约 1218-1226)加一键:

```python
        wallet_summaries.append(
            {
                "address": w["address"],
                "remark": w.get("remark", ""),
                "enabled": w["enabled"],
                "running": running,
                "balance": balance,
                "open_orders": w_order_count,
                "positions": w_pos_count,
            }
        )
```

- [ ] **Step 6: 跑测试确认通过 + 全量零回归**

Run: `pytest tests/test_wallet_remark_routes.py -v` → 期望 4 项通过。
Run: `pytest -q` → 期望全绿。

- [ ] **Step 7: 提交**

```bash
git add web/routes.py tests/test_wallet_remark_routes.py
git commit -m "$(cat <<'EOF'
feat(api): PUT /api/wallets/<addr>/remark + 导入带备注 + dashboard 回显

仿 proxy 路由:编辑备注(strip+截断40)、POST 导入接可选 remark、/api/dashboard 的
wallet_summaries 带上 remark 供仪表盘显示。不清 _api_cache(备注与 API 无关)。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 前端显示 — app.js 工具 + 配置页编辑 + 4 页显示

**主会话直接改(中文模板,不派 subagent)。前端无单测,交付后实跑走查。**

**Files:**
- Modify: `web/static/app.js`、`web/templates/{config,dashboard,networth,history,logs}.html`

**Interfaces:**
- Consumes: `/api/wallets` 与 `/api/dashboard` 响应里的 `w.remark`;`PUT /api/wallets/<addr>/remark`;全局 `escapeHtml`/`showToast`。
- Produces: 全局 `shortAddr(addr)`、`walletLabel(remark, addr)`。

- [ ] **Step 1: `web/static/app.js` 加共享工具(放在 `escapeHtml` 之后)**

```javascript
// 钱包短地址 + 备注标签(备注纯展示,各页复用)。
function shortAddr(a) {
  return a && a.length > 12 ? a.slice(0, 6) + '...' + a.slice(-4) : (a || '');
}
// 钱包标签:有备注显示备注,否则短地址;完整地址请放到 title。
function walletLabel(remark, addr) {
  return (remark && String(remark).trim()) ? String(remark) : shortAddr(addr);
}
```

- [ ] **Step 2: `config.html` — 导入输入 + 表头 + 行 + 编辑 + 导入透传**

① 导入表单,`new-proxy` 输入(约 10 行)后加:
```html
        <input type="text" id="new-remark" placeholder="备注（可选，如 主号/小号2）">
```
② 表头(约 16 行)在「代理」后加「备注」:
```html
            <tr><th>地址</th><th>存款地址</th><th>代理</th><th>备注</th><th>模板</th><th>状态</th><th>操作</th></tr>
```
③ 备注缓存声明(约 482 行,`let _walletProxies = {};` 后):
```javascript
let _walletRemarks = {};
```
④ `loadWallets` 里:重置缓存、逐行填缓存、加备注 `<td>`、加「备注」按钮。把 `_walletProxies = {};`(约 506)改为:
```javascript
        _walletProxies = {};
        _walletRemarks = {};
```
把 `_walletProxies[w.address] = w.proxy || '';`(约 508)改为:
```javascript
            _walletProxies[w.address] = w.proxy || '';
            _walletRemarks[w.address] = w.remark || '';
```
在代理 `<td>`(约 518)后加备注 `<td>`:
```javascript
                <td title="${escapeHtml(w.proxy || '')}">${proxyLabel(w.proxy)}</td>
                <td title="${escapeHtml(w.address)}">${escapeHtml(w.remark || '')}</td>
```
在「代理」按钮(约 528)后加「备注」按钮:
```javascript
                    <button class="btn btn-sm" onclick="editProxy('${w.address}')">代理</button>
                    <button class="btn btn-sm" onclick="editRemark('${w.address}')">备注</button>
```
⑤ 新增 `editRemark`(放在 `editProxy` 之后,约 501):
```javascript
function editRemark(address) {
    const cur = _walletRemarks[address] || '';
    const v = prompt('备注（留空=清除）：', cur);
    if (v === null) return;
    fetch(`/api/wallets/${address}/remark`, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({remark: v.trim()}),
    }).then(r => r.json()).then(() => {
        showToast('备注已更新');
        loadWallets();
    });
}
```
⑥ `finalizeImport`:读 remark 并透传 + 导入后清空。把 proxy 那段(约 578-580)改为:
```javascript
    const proxy = document.getElementById('new-proxy').value.trim();
    const remark = document.getElementById('new-remark').value.trim();
    const body = funder ? {private_key: key, funder: funder} : {private_key: key};
    if (proxy) body.proxy = proxy;
    if (remark) body.remark = remark;
```
在清空 `new-proxy`(约 597)后加:
```javascript
        document.getElementById('new-proxy').value = '';
        document.getElementById('new-remark').value = '';
```

- [ ] **Step 3: `dashboard.html` — 地址格显示备注(约 71 行)**

```javascript
                <td title="${escapeHtml(w.address)}">${escapeHtml(walletLabel(w.remark, w.address))}</td>
```

- [ ] **Step 4: `networth.html` — 下拉带备注(约 24-27 行)**

把选项 map 改为:
```javascript
        sel.innerHTML = ws.map(w =>
            `<option value="${w.address}">${(w.remark && w.remark.trim()) ? escapeHtml(w.remark) + ' (' + shortAddr(w.address) + ')' : shortAddr(w.address)}</option>`
        ).join('');
```

- [ ] **Step 5: `history.html` — 地址→备注映射 + 下拉 + 表格列**

① 在 `shortWallet`(约 49)附近加映射声明:
```javascript
let _remarkByAddr = {};
```
② 表格「钱包」列(约 112)改用 `walletLabel`:
```javascript
                <td title="${escapeHtml(a.wallet)}">${escapeHtml(walletLabel(_remarkByAddr[a.wallet] || '', a.wallet))}</td>
```
③ 建下拉时同时建映射(约 130-138)改为:
```javascript
fetch('/api/wallets').then(r => r.json()).then(wallets => {
    const select = document.getElementById('wallet-filter');
    wallets.forEach(w => {
        _remarkByAddr[w.address] = w.remark || '';
        const opt = document.createElement('option');
        opt.value = w.address;
        opt.textContent = (w.remark && w.remark.trim())
            ? `${w.remark} (${shortAddr(w.address)})` : shortAddr(w.address);
        select.appendChild(opt);
    });
});
```
④ 现有 `shortWallet`(约 49-51)被本改动弃用(仅那一处引用),删除以免留死代码:
```javascript
（删除）function shortWallet(w) { return w && w.length > 12 ? w.slice(0, 6) + '..' + w.slice(-4) : (w || ''); }
```

- [ ] **Step 6: `logs.html` — 同 history(映射 + 下拉 + 表格列 + 删 shortWallet)**

① 在 `shortWallet`(约 31)附近加:
```javascript
let _remarkByAddr = {};
```
② 表格「钱包」列(约 59)改:
```javascript
                <td title="${escapeHtml(r.wallet)}">${escapeHtml(walletLabel(_remarkByAddr[r.wallet] || '', r.wallet))}</td>
```
③ 下拉(约 73-81)改:
```javascript
fetch('/api/wallets').then(r => r.json()).then(wallets => {
    const select = document.getElementById('wallet-filter');
    wallets.forEach(w => {
        _remarkByAddr[w.address] = w.remark || '';
        const opt = document.createElement('option');
        opt.value = w.address;
        opt.textContent = (w.remark && w.remark.trim())
            ? `${w.remark} (${shortAddr(w.address)})` : shortAddr(w.address);
        select.appendChild(opt);
    });
});
```
④ 删除弃用的 `shortWallet`(约 31-33)。

- [ ] **Step 7: 静态校验(中文模板必做:防别字/BOM/语法)**

Run:
```bash
node --check web/static/app.js
for f in config dashboard networth history logs; do node -e "require('fs').readFileSync('web/templates/$f.html','utf8')" && echo "$f ok"; done
grep -lP '\xEF\xBB\xBF' web/static/app.js web/templates/{config,dashboard,networth,history,logs}.html || echo "无 BOM"
```
Expected: `app.js` 语法通过;各模板可读;无 BOM。

- [ ] **Step 8: 实跑走查(手动,主会话)**

`python app.py` 登录后:① 配置页导入表单填备注导入 / 或对已有钱包点「备注」改;② 配置页表格「备注」列显示正确;③ 仪表盘钱包状态表显示备注;④ 净值页下拉显示 `备注 (0x…)`;⑤ 历史/日志页下拉 + 「钱包」列显示备注;⑥ 输入含 `<b>"'` 的备注,确认不破版、如实转义显示。

- [ ] **Step 9: 提交**

```bash
git add web/static/app.js web/templates/config.html web/templates/dashboard.html web/templates/networth.html web/templates/history.html web/templates/logs.html
git commit -m "$(cat <<'EOF'
feat(ui): 5 处显示钱包备注 + 配置页编辑

app.js 加共享 shortAddr/walletLabel;配置页加备注列/按钮(prompt 改)/导入输入;
仪表盘、净值下拉、历史/日志下拉+钱包列均按 walletLabel 显示备注(有则备注否则短址、
完整地址进 title),渲染前 escapeHtml。删除被弃用的 history/logs 内联 shortWallet。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**1. Spec coverage**（逐节对照 spec）:
- 数据模型 remark 列 + 迁移 → Task 1 Step 3/4。✓
- `set_wallet_remark` / `list_wallets` 回显 / `add_wallet` remark → Task 1 Step 5/6/7。✓
- `PUT .../remark`(strip+截断40)/ 导入 remark → Task 2 Step 3/4。✓
- `/api/dashboard` 回显 remark → Task 2 Step 5(dashboard 用的是该端点非 /api/wallets)。✓
- 配置页 列 + 按钮(prompt 仿 editProxy)+ 导入输入 → Task 3 Step 2。✓
- 显示约定 walletLabel + 5 处(config/dashboard/networth/history/logs)→ Task 3 Step 1/3/4/5/6。✓
- 转义(innerHTML/title 用 escapeHtml,下拉用 textContent)→ Task 3 各步已按面用 escapeHtml/textContent。✓
- 不改 markets.html、不清 _api_cache → 未列入改动。✓

**2. Placeholder scan**:无 TBD/TODO/「同上」;每个代码步骤给完整可照抄代码。✓

**3. Type consistency**:`add_wallet(..., remark="")` 形参在 Task 1 Step 5 定义、Task 2 Step 4 调用一致;`set_wallet_remark(address, remark)` 定义(Task 1 Step 6)与路由调用(Task 2 Step 3)一致;`walletLabel(remark, addr)`/`shortAddr(addr)` 定义(Task 3 Step 1)与各页调用参数序一致;`_remarkByAddr` 在 history/logs 各自声明后使用。✓
