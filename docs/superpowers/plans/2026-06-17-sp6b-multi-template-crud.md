# SP6b 多模板 CRUD + 钱包绑定 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 配置页支持多模板:建/改名/删/切模板、按选中模板编辑策略参数、钱包行内下拉绑定模板;引擎参数仍全局。

**Architecture:** DB 加 `rename_template`(其余方法 SP1 已有)。`web/routes.py` 加 `/api/templates` CRUD + `POST /api/wallets/<addr>/template`(`/api/settings` 不改)。`config.html` 把 SP6a 单表单拆成「策略参数(按选中模板→`/api/templates/<id>`)+ 引擎参数(全局→`/api/settings`)」,加模板管理段 + 钱包模板列。

**Tech Stack:** Flask(Jinja + 原生 JS)/ pytest(Flask test client + 真 Database)。

**执行顺序:** T1 DB(rename)→ T2 路由(用 rename)→ T3 前端(用路由)。基线:SP6a 合并后 `408 passed`。

---

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `models/database.py` | 加 `rename_template` | 修改 |
| `web/routes.py` | `import sqlite3` + `/api/templates` CRUD + 钱包绑定路由 | 修改 |
| `web/templates/config.html` | 拆策略/引擎表单 + 模板管理段 + 钱包模板列 | 重写 |
| `tests/test_database.py` | rename 单测 | 修改 |
| `tests/test_templates_routes.py` | 模板路由测试 | 新建 |

---

## Task 1: db.rename_template

**Files:** Modify `models/database.py`(`save_template` 之后)。Test: `tests/test_database.py`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_database.py` 末尾追加(文件顶部已 `import pytest` + 有 `db` fixture):
```python
def test_rename_template(db):
    tid = db.create_template("旧名")
    db.rename_template(tid, "新名")
    names = {t["name"] for t in db.list_templates()}
    assert "新名" in names and "旧名" not in names


def test_rename_template_duplicate_raises(db):
    import sqlite3
    db.create_template("A")
    tid = db.create_template("B")
    with pytest.raises(sqlite3.IntegrityError):
        db.rename_template(tid, "A")
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_database.py::test_rename_template -v`
Expected: FAIL（`AttributeError: ... no attribute 'rename_template'`）

- [ ] **Step 3: 实现**

在 `models/database.py` 的 `save_template` 方法之后加:
```python
    def rename_template(self, template_id: int, name: str):
        c = self.conn.cursor()
        c.execute("UPDATE templates SET name = ? WHERE id = ?", (name, template_id))
        self.conn.commit()
```

- [ ] **Step 4: 运行确认 PASS + 无回归**

Run: `python -m pytest tests/test_database.py -q`
Expected: ALL PASS

- [ ] **Step 5: Commit（不 stage `.claude/settings.local.json`）**

```bash
git add models/database.py tests/test_database.py
git commit -m "feat(db): rename_template(改模板名,重名 UNIQUE 抛错)"
```

---

## Task 2: /api/templates 路由 + 钱包绑定

**Files:** Modify `web/routes.py`。Test: Create `tests/test_templates_routes.py`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_templates_routes.py`:
```python
"""tests/test_templates_routes.py — /api/templates CRUD + 钱包绑定(Flask client + 真 DB)。"""

import web.routes as routes
from models.database import Database


def _client(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client, db


def test_list_templates_includes_default(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    rows = client.get("/api/templates").get_json()
    assert any(r["is_default"] for r in rows)
    assert all({"id", "name", "is_default"} <= set(r) for r in rows)


def test_create_get_save_roundtrip(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    tid = client.post("/api/templates", json={"name": "激进"}).get_json()["id"]
    got = client.get(f"/api/templates/{tid}").get_json()
    assert "max_exposure_usd" in got  # 返回合并默认值
    client.put(f"/api/templates/{tid}", json={
        "max_exposure_usd": 99,
        "tier_rules": [[{"upper": None, "action": {"type": "min_size"}}]],
    })
    saved = db.get_template(tid)
    assert saved["max_exposure_usd"] == 99
    assert saved["tier_rules"] == [[{"upper": None, "action": {"type": "min_size"}}]]


def test_create_duplicate_name_400(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    client.post("/api/templates", json={"name": "dup"})
    resp = client.post("/api/templates", json={"name": "dup"})
    assert resp.status_code == 400


def test_create_empty_name_400(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    assert client.post("/api/templates", json={"name": "  "}).status_code == 400


def test_rename_route(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    tid = client.post("/api/templates", json={"name": "x"}).get_json()["id"]
    client.put(f"/api/templates/{tid}/name", json={"name": "y"})
    assert any(t["name"] == "y" for t in db.list_templates())
    client.post("/api/templates", json={"name": "z"})
    assert client.put(f"/api/templates/{tid}/name", json={"name": "z"}).status_code == 400


def test_delete_template_and_default_guard(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    tid = client.post("/api/templates", json={"name": "tmp"}).get_json()["id"]
    assert client.delete(f"/api/templates/{tid}").status_code == 200
    assert all(t["id"] != tid for t in db.list_templates())
    default_id = db.get_default_template_id()
    assert client.delete(f"/api/templates/{default_id}").status_code == 400


def test_bind_wallet_to_template(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    db.add_wallet("0xW", "enc", "0xF", 2)
    tid = client.post("/api/templates", json={"name": "bind"}).get_json()["id"]
    client.put(f"/api/templates/{tid}", json={"max_exposure_usd": 123})
    resp = client.post("/api/wallets/0xW/template", json={"template_id": tid})
    assert resp.status_code == 200
    assert db.get_template_for("0xW")["max_exposure_usd"] == 123


def test_put_template_filters_non_strategy_keys(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    tid = client.post("/api/templates", json={"name": "f"}).get_json()["id"]
    client.put(f"/api/templates/{tid}", json={"scan_interval_sec": 99, "max_exposure_usd": 7})
    saved = db.get_template(tid)
    assert saved["max_exposure_usd"] == 7
    assert "scan_interval_sec" not in saved  # 引擎键被丢弃,不进模板
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_templates_routes.py -v`
Expected: FAIL（路由 404 / AssertionError，因 `/api/templates` 尚不存在）

- [ ] **Step 3: 实现路由**

(a) 在 `web/routes.py` 顶部 import 区(`import logging` 附近)加 `import sqlite3`。

(b) 在 `api_save_settings`（`@app.route("/api/settings", methods=["POST"])` 那个函数）之后插入:
```python
# --- API: Templates (多模板 CRUD + 钱包绑定) ---


@app.route("/api/templates", methods=["GET"])
@login_required
def api_list_templates():
    default_id = db.get_default_template_id()
    return jsonify(
        [
            {"id": t["id"], "name": t["name"], "is_default": t["id"] == default_id}
            for t in db.list_templates()
        ]
    )


@app.route("/api/templates", methods=["POST"])
@login_required
def api_create_template():
    name = ((request.get_json() or {}).get("name") or "").strip()
    if not name:
        return jsonify({"error": "模板名不能为空"}), 400
    try:
        tid = db.create_template(name)
    except sqlite3.IntegrityError:
        return jsonify({"error": "模板名已存在"}), 400
    return jsonify({"id": tid, "name": name})


@app.route("/api/templates/<int:tid>", methods=["GET"])
@login_required
def api_get_template(tid):
    return jsonify(db.get_template(tid))


@app.route("/api/templates/<int:tid>", methods=["PUT"])
@login_required
def api_save_template(tid):
    from config import TEMPLATE_DEFAULTS

    data = request.get_json() or {}
    strategy = {k: v for k, v in data.items() if k in TEMPLATE_DEFAULTS}
    db.save_template(tid, strategy)
    return jsonify({"ok": True})


@app.route("/api/templates/<int:tid>/name", methods=["PUT"])
@login_required
def api_rename_template(tid):
    name = ((request.get_json() or {}).get("name") or "").strip()
    if not name:
        return jsonify({"error": "模板名不能为空"}), 400
    try:
        db.rename_template(tid, name)
    except sqlite3.IntegrityError:
        return jsonify({"error": "模板名已存在"}), 400
    return jsonify({"ok": True})


@app.route("/api/templates/<int:tid>", methods=["DELETE"])
@login_required
def api_delete_template(tid):
    try:
        db.delete_template(tid)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/wallets/<address>/template", methods=["POST"])
@login_required
def api_set_wallet_template(address):
    tid = (request.get_json() or {}).get("template_id")
    db.set_wallet_template(address, int(tid))
    return jsonify({"ok": True})
```

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_templates_routes.py -v`
Expected: 8 passed。

- [ ] **Step 5: 全套无回归**

Run: `python -m pytest -q`
Expected: ALL PASS（408 + 2(T1) + 8(T2) = `418 passed`）。

- [ ] **Step 6: Commit**

```bash
git add web/routes.py tests/test_templates_routes.py
git commit -m "feat(routes): /api/templates CRUD + 钱包绑定模板(改名/默认删 400/重名 400)"
```

---

## Task 3: config.html 多模板重构

**Files:** Modify `web/templates/config.html`（整文件替换）。

- [ ] **Step 1: 整文件替换**

把 `web/templates/config.html` 整个文件替换为以下内容（钱包导入/弹窗 JS 保持原样,仅新增模板与绑定逻辑、拆策略/引擎表单、钱包表加模板列、去掉与单表单耦合的「未保存离开」提醒）:

```html
{% extends "base.html" %}
{% block content %}
<h1>配置</h1>

<section class="config-section">
    <h2>钱包管理</h2>
    <div class="form-inline">
        <input type="password" id="new-private-key" placeholder="钱包私钥（64位十六进制）">
        <input type="text" id="new-funder" placeholder="存款钱包地址（可选，留空自动识别）">
        <button class="btn btn-primary" onclick="addWallet()">添加钱包</button>
    </div>
    <p style="color:#888;font-size:12px;margin-top:4px;">一般只需输入私钥，存款钱包地址会自动识别；若自动识别的地址与 polymarket.com/settings 不一致，可在第二个框手动填写正确的存款地址。</p>
    <table class="data-table" id="wallet-config-table">
        <thead>
            <tr><th>地址</th><th>存款地址</th><th>模板</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody id="wallet-config-body"></tbody>
    </table>
</section>

<section class="config-section">
    <h2>模板管理</h2>
    <div class="form-inline">
        <label>当前模板 <select id="template-select"></select></label>
        <input type="text" id="new-template-name" placeholder="新模板名">
        <button class="btn" type="button" onclick="createTemplate()">新建</button>
        <button class="btn" type="button" onclick="renameTemplate()">重命名</button>
        <button class="btn btn-danger" id="delete-template-btn" type="button" onclick="deleteTemplate()">删除</button>
    </div>
    <p style="color:#888;font-size:12px;margin-top:4px;">下方「策略参数」编辑当前选中的模板；上表每个钱包各自绑定模板。默认模板不可删除。</p>
</section>

<section class="config-section">
    <h2>策略参数（当前模板）</h2>
    <form id="strategy-form">
        <div class="form-grid">
            <div class="form-group">
                <label>最低奖励金额 (USD)</label>
                <input type="number" name="min_reward_usd" step="1">
            </div>
            <div class="form-group">
                <label>买卖价差上限 (美分)</label>
                <input type="number" name="max_spread_cents" step="0.1">
            </div>
            <div class="form-group">
                <label>单价范围下限 (美分)</label>
                <input type="number" name="min_price_cents" step="1">
            </div>
            <div class="form-group">
                <label>单价范围上限 (美分)</label>
                <input type="number" name="max_price_cents" step="1">
            </div>
            <div class="form-group">
                <label>最短结算天数</label>
                <input type="number" name="min_settlement_days" step="1">
            </div>
            <div class="form-group">
                <label>浮亏阈值 θ_loss (美分)</label>
                <input type="number" name="theta_loss_cents" step="1">
            </div>
            <div class="form-group">
                <label>强平阈值 θ_stop (美分)</label>
                <input type="number" name="theta_stop_cents" step="1">
            </div>
            <div class="form-group">
                <label>成本≤买一时离场方式</label>
                <select name="case_a_mode">
                    <option value="ask">挂卖一（吃满价差，maker）</option>
                    <option value="market">市价（贴买一立即清掉）</option>
                </select>
            </div>
            <div class="form-group">
                <label>单市场最大敞口 (USD)</label>
                <input type="number" name="max_exposure_usd" step="1">
            </div>
            <div class="form-group">
                <label>单市场最大份数 (share)</label>
                <input type="number" name="max_exposure_shares" step="1">
            </div>
            <div class="form-group">
                <label>最大并发做市市场数</label>
                <input type="number" name="max_concurrent_markets" step="1">
            </div>
            <div class="form-group">
                <label>双边挂单价格下限 (美分)</label>
                <input type="number" name="min_price_double_cents" step="1">
            </div>
            <div class="form-group">
                <label>价格档数 K</label>
                <input type="number" name="tiers_k" step="1" min="1">
            </div>
        </div>

        <h3>排除品类</h3>
        <div id="excluded-categories" class="form-inline">
            <label><input type="checkbox" value="sports"> 体育</label>
            <label><input type="checkbox" value="esports"> 电竞</label>
            <label><input type="checkbox" value="weather"> 天气</label>
        </div>

        <h3>单份奖励阈值（按最低份数取档）</h3>
        <div id="per-share-thresholds" class="form-grid">
            <div class="form-group"><label>20 档</label><input type="number" step="0.01" data-bracket="20"></div>
            <div class="form-group"><label>50 档</label><input type="number" step="0.01" data-bracket="50"></div>
            <div class="form-group"><label>100 档</label><input type="number" step="0.01" data-bracket="100"></div>
            <div class="form-group"><label>200 档</label><input type="number" step="0.01" data-bracket="200"></div>
            <div class="form-group"><label>250 档</label><input type="number" step="0.01" data-bracket="250"></div>
        </div>

        <h3>多档挂单规则 tier_rules（JSON）</h3>
        <textarea id="tier-rules-json" rows="10" style="width:100%;font-family:monospace"></textarea>
        <p style="color:#888;font-size:12px;margin-top:4px;">
            JSON：档位数组，每档是若干区间 {"upper": 累加厚度上限或 null, "action": {...}}（半开升序 [前一上界, upper)）。
            动作 type 五选一：min_size（最小份数）/ fixed_shares（固定份数，带 "shares"）/ fixed_amount（固定金额，带 "usd"）/ wallet_total（钱包剩余全额）/ skip（不挂）。
            例：[[{"upper": null, "action": {"type": "min_size"}}]]。留默认即每档挂最小份数。
        </p>
        <button type="submit" class="btn btn-primary">保存策略参数</button>
    </form>
</section>

<section class="config-section">
    <h2>引擎参数（全局）</h2>
    <form id="engine-form">
        <div class="form-grid">
            <div class="form-group">
                <label>市场扫描间隔 (秒)</label>
                <input type="number" name="scan_interval_sec" step="1">
            </div>
            <div class="form-group">
                <label>成交检查间隔 (秒)</label>
                <input type="number" name="fill_check_interval_sec" step="1">
            </div>
            <div class="form-group">
                <label>成交后冷却时间 (分钟)</label>
                <input type="number" name="cooldown_minutes" step="1">
            </div>
            <div class="form-group">
                <label>奖励参数缓存 (秒)</label>
                <input type="number" name="rewards_cache_ttl_sec" step="1">
            </div>
            <div class="form-group">
                <label>市场发现间隔 (秒，默认 14400=4 小时)</label>
                <input type="number" name="discovery_interval_sec" step="1">
            </div>
        </div>
        <button type="submit" class="btn btn-primary">保存引擎参数</button>
    </form>
</section>

<div id="wallet-modal" class="modal-overlay" style="display:none">
    <div class="modal-box">
        <h2>确认存款钱包地址</h2>
        <p class="modal-hint">根据私钥自动识别出的存款地址如下，请与你在 <b>polymarket.com</b> 看到的存款（资金）地址<b>逐字比对</b>：</p>
        <div id="wm-derived" class="addr-big"></div>
        <p class="modal-hint">若一致，直接点「确定」即可。<br>若不一致（例如智能合约 / 邮箱钱包，地址通常不同），请把你在 Polymarket 上的<b>正确存款地址</b>粘贴到下面：</p>
        <input id="wm-funder-input" class="addr-input" type="text" spellcheck="false" placeholder="0x… 正确的存款钱包地址">
        <p id="wm-error" class="modal-error" style="display:none"></p>
        <div class="modal-actions">
            <button id="wm-confirm" class="btn btn-primary" type="button">确定</button>
            <button id="wm-cancel" class="btn" type="button">取消</button>
        </div>
    </div>
</div>
{% endblock %}

{% block scripts %}
<script>
let templates = [];
let defaultTemplateId = null;
let currentTemplateId = null;

function loadTemplates() {
    return fetch('/api/templates').then(r => r.json()).then(list => {
        templates = list;
        const def = list.find(t => t.is_default);
        defaultTemplateId = def ? def.id : (list[0] && list[0].id);
        const sel = document.getElementById('template-select');
        sel.innerHTML = list.map(t =>
            `<option value="${t.id}">${t.name}${t.is_default ? '（默认）' : ''}</option>`
        ).join('');
        if (currentTemplateId === null || !list.some(t => t.id === currentTemplateId)) {
            currentTemplateId = defaultTemplateId;
        }
        sel.value = currentTemplateId;
        document.getElementById('delete-template-btn').disabled =
            (currentTemplateId === defaultTemplateId);
    });
}

function loadStrategy(tid) {
    fetch(`/api/templates/${tid}`).then(r => r.json()).then(data => {
        const form = document.getElementById('strategy-form');
        Object.keys(data).forEach(key => {
            const input = form.querySelector(`[name="${key}"]`);
            if (input) input.value = data[key];
        });
        const excluded = data.excluded_categories || [];
        document.querySelectorAll('#excluded-categories input[type=checkbox]').forEach(cb => {
            cb.checked = excluded.includes(cb.value);
        });
        const ps = data.per_share_reward_thresholds || {};
        document.querySelectorAll('#per-share-thresholds input[data-bracket]').forEach(inp => {
            const b = inp.getAttribute('data-bracket');
            inp.value = (ps[b] !== undefined) ? ps[b] : 0.30;
        });
        document.getElementById('tier-rules-json').value =
            JSON.stringify(data.tier_rules || [], null, 2);
    });
}

function loadEngine() {
    fetch('/api/settings').then(r => r.json()).then(data => {
        document.querySelectorAll('#engine-form input[name]').forEach(inp => {
            if (data[inp.name] !== undefined) inp.value = data[inp.name];
        });
    });
}

document.getElementById('template-select').addEventListener('change', function() {
    currentTemplateId = parseInt(this.value, 10);
    document.getElementById('delete-template-btn').disabled =
        (currentTemplateId === defaultTemplateId);
    loadStrategy(currentTemplateId);
});

function createTemplate() {
    const name = document.getElementById('new-template-name').value.trim();
    if (!name) { alert('请输入新模板名'); return; }
    fetch('/api/templates', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name}),
    }).then(r => r.json()).then(resp => {
        if (resp.error) { alert(resp.error); return; }
        document.getElementById('new-template-name').value = '';
        currentTemplateId = resp.id;
        loadTemplates().then(() => { loadStrategy(currentTemplateId); loadWallets(); });
    });
}

function renameTemplate() {
    const t = templates.find(x => x.id === currentTemplateId);
    const name = prompt('新模板名', t ? t.name : '');
    if (!name || !name.trim()) return;
    fetch(`/api/templates/${currentTemplateId}/name`, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: name.trim()}),
    }).then(r => r.json()).then(resp => {
        if (resp.error) { alert(resp.error); return; }
        loadTemplates().then(loadWallets);
    });
}

function deleteTemplate() {
    if (currentTemplateId === defaultTemplateId) { alert('默认模板不可删除'); return; }
    if (!confirm('确定删除该模板？绑定它的钱包将回落默认模板。')) return;
    fetch(`/api/templates/${currentTemplateId}`, {method: 'DELETE'}).then(r => r.json()).then(resp => {
        if (resp.error) { alert(resp.error); return; }
        currentTemplateId = defaultTemplateId;
        loadTemplates().then(() => { loadStrategy(currentTemplateId); loadWallets(); });
    });
}

document.getElementById('strategy-form').addEventListener('submit', function(e) {
    e.preventDefault();
    const data = {};
    this.querySelectorAll('input[type=number][name]').forEach(inp => {
        data[inp.name] = parseFloat(inp.value);
    });
    const caseSel = this.querySelector('select[name="case_a_mode"]');
    if (caseSel) data.case_a_mode = caseSel.value;
    data.excluded_categories = Array.from(
        document.querySelectorAll('#excluded-categories input[type=checkbox]:checked')
    ).map(cb => cb.value);
    const ps = {};
    document.querySelectorAll('#per-share-thresholds input[data-bracket]').forEach(inp => {
        ps[inp.getAttribute('data-bracket')] = parseFloat(inp.value);
    });
    data.per_share_reward_thresholds = ps;
    try {
        data.tier_rules = JSON.parse(document.getElementById('tier-rules-json').value);
    } catch (err) {
        alert('tier_rules JSON 格式错误，请检查后再保存');
        return;
    }
    fetch(`/api/templates/${currentTemplateId}`, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data),
    }).then(r => r.json()).then(() => alert('策略参数已保存（下次引擎启动生效）'));
});

document.getElementById('engine-form').addEventListener('submit', function(e) {
    e.preventDefault();
    const data = {};
    this.querySelectorAll('input[type=number][name]').forEach(inp => {
        data[inp.name] = parseFloat(inp.value);
    });
    fetch('/api/settings', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data),
    }).then(r => r.json()).then(resp => alert(resp.message));
});

function loadWallets() {
    fetch('/api/wallets').then(r => r.json()).then(wallets => {
        const tbody = document.getElementById('wallet-config-body');
        tbody.innerHTML = wallets.map(w => {
            const opts = templates.map(t => {
                const sel = (w.template_id === t.id) ||
                    (w.template_id == null && t.id === defaultTemplateId);
                return `<option value="${t.id}" ${sel ? 'selected' : ''}>${t.name}</option>`;
            }).join('');
            return `
            <tr>
                <td title="${w.address}">${w.address.slice(0,6)}...${w.address.slice(-4)}</td>
                <td title="${w.funder || ''}">${w.funder ? w.funder.slice(0,6)+'...'+w.funder.slice(-4) : '-'}</td>
                <td><select onchange="bindWalletTemplate('${w.address}', this.value)">${opts}</select></td>
                <td>
                    <label class="switch">
                        <input type="checkbox" ${w.enabled ? 'checked' : ''}
                            onchange="toggleWallet('${w.address}', this.checked)">
                        <span class="slider"></span>
                    </label>
                </td>
                <td>
                    <button class="btn btn-sm btn-danger" onclick="removeWallet('${w.address}')">删除</button>
                </td>
            </tr>`;
        }).join('');
    });
}

function bindWalletTemplate(address, templateId) {
    fetch(`/api/wallets/${address}/template`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({template_id: parseInt(templateId, 10)}),
    });
}

const ACCT_TYPES = {0: 'EOA（直接用钱包地址）', 1: 'Proxy（邮箱/嵌入式登录）', 2: 'Safe（浏览器钱包登录）', 3: '智能合约钱包（POLY_1271）'};
let _pendingKey = null;

function addWallet() {
    const key = document.getElementById('new-private-key').value.trim();
    const funder = document.getElementById('new-funder').value.trim();
    if (!key) { alert('请输入私钥'); return; }
    if (funder) {
        finalizeImport(key, funder);
        return;
    }
    fetch('/api/wallets/preview', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({private_key: key}),
    }).then(r => r.json()).then(d => {
        if (d.error) { alert(d.error); return; }
        openWalletModal(key, d.derived_funder);
    });
}

function openWalletModal(key, derived) {
    _pendingKey = key;
    document.getElementById('wm-derived').textContent = derived;
    document.getElementById('wm-funder-input').value = derived;
    document.getElementById('wm-error').style.display = 'none';
    document.getElementById('wallet-modal').style.display = 'flex';
}

function closeWalletModal() {
    document.getElementById('wallet-modal').style.display = 'none';
    _pendingKey = null;
}

function finalizeImport(key, funder) {
    const body = funder ? {private_key: key, funder: funder} : {private_key: key};
    fetch('/api/wallets', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
    }).then(r => r.json()).then(data => {
        if (data.error) {
            const wm = document.getElementById('wallet-modal');
            if (wm.style.display !== 'none') {
                const e = document.getElementById('wm-error');
                e.textContent = data.error; e.style.display = 'block';
            } else { alert(data.error); }
            return;
        }
        closeWalletModal();
        document.getElementById('new-private-key').value = '';
        document.getElementById('new-funder').value = '';
        showToast('钱包已添加 · 账户类型：' + (ACCT_TYPES[data.signature_type] || ('sig=' + data.signature_type)));
        loadWallets();
    });
}

document.getElementById('wm-confirm').onclick = function() {
    const f = document.getElementById('wm-funder-input').value.trim();
    if (!f) { const e = document.getElementById('wm-error'); e.textContent = '请填写或确认存款钱包地址'; e.style.display = 'block'; return; }
    finalizeImport(_pendingKey, f);
};
document.getElementById('wm-cancel').onclick = closeWalletModal;

function removeWallet(address) {
    if (!confirm('确定删除该钱包？相关订单数据将保留在历史记录中。')) return;
    fetch(`/api/wallets/${address}`, {method: 'DELETE'}).then(() => loadWallets());
}

function toggleWallet(address, enabled) {
    fetch(`/api/wallets/${address}/toggle`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled}),
    });
}

loadTemplates().then(() => { loadStrategy(currentTemplateId); loadWallets(); });
loadEngine();
</script>
{% endblock %}
```

- [ ] **Step 2: 全套测试无回归**

Run: `python -m pytest -q`
Expected: 仍 `418 passed`（纯前端,不动 Python）。

- [ ] **Step 3: JS 语法检查**

提取 `<script>` 块跑 `node --check`:
```bash
python - <<'PY'
import re
html = open("web/templates/config.html", encoding="utf-8").read()
js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
open("_cfg_check.js", "w", encoding="utf-8").write(js)
PY
node --check _cfg_check.js && echo "JS OK"; rm -f _cfg_check.js
```
Expected: `JS OK`。

- [ ] **Step 4: 人工核对清单（前端无 JS 测试框架）**

启动 `python app.py`、登录 → 配置页:
1. 模板下拉列出模板（含「默认（默认）」）;输入名「新建」→ 出现并选中,策略参数显默认值。
2. 切换模板 → 策略参数载入该模板值;改值「保存策略参数」→ 切走再切回值还在;切到另一模板值不同(隔离)。
3. 「重命名」弹框改名 → 下拉名变;重名 → alert 报错。
4. 选中默认模板时「删除」按钮禁用;选中非默认 → 删除 → 消失、回到默认。
5. 钱包行「模板」下拉改选 → 刷新后保持;删除某绑定模板 → 该钱包下拉回落「默认」。
6. 引擎参数段独立「保存引擎参数」,不影响模板。

- [ ] **Step 5: Commit**

```bash
git add web/templates/config.html
git commit -m "feat(config-ui): 多模板管理(选择/建/改名/删)+ 策略按模板存取 + 钱包行绑定 + 引擎参数独立"
```

---

## 验收 checkpoint（对应 spec §六）

1. 建/改名/删/切模板:T2 路由测试 + T3 清单 1/3/4。
2. 策略参数按选中模板独立存取:T2 `create_get_save_roundtrip` + T3 清单 2。
3. 钱包行绑定、`get_template_for` 随之变:T2 `bind_wallet` + T3 清单 5。
4. 引擎参数仍全局、`/api/settings` 未改:SP6a 契约测试仍过 + T3 清单 6。
5. 默认不可删、重名拦截:T2 `delete..._guard` / `..._duplicate_400` / `rename_route`。
6. `pytest` 全绿:T2 Step5 / T3 Step2。

## 范围之外

SP6c tier_rules 可视化编辑器 · SP6d 死字段清理。
