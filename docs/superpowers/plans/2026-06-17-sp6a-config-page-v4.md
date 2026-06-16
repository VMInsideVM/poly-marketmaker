# SP6a 配置页对齐 v4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把配置页 `config.html` 对齐 v4——删 4 个已退役字段、补齐全部 v4 策略参数(含结构化 list/dict/JSON)+ 引擎键 `discovery_interval_sec`,仍编默认模板。

**Architecture:** 纯前端(`config.html` 的 HTML + JS)。`/api/settings` GET/POST **不改**——GET 已返回「引擎 + 默认模板」全部键,POST 已按 `ENGINE_DEFAULTS`/`TEMPLATE_DEFAULTS` 归类并 `json.dumps` 存值,结构化值天然 round-trip。唯一可自动测的面是后端契约(Flask test client),前端靠契约测试 + 人工核对清单。

**Tech Stack:** Flask(Jinja 模板 + 原生 JS)/ pytest(Flask test client + 真 Database)。

**执行顺序:** T1 后端契约测试(确认路由已支持 v4、无需改后端)→ T2 `config.html` 重写(前端)。基线:SP5a-2 合并后 `405 passed`。

> 说明:T1 的测试对**现有路由代码**即通过(路由本就泛化处理 v4 键)——这正是结论「SP6a 不需改后端」的证据,测试作为契约/回归闸长期保留。

---

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `tests/test_settings_routes.py` | `/api/settings` GET/POST 的 v4 round-trip 契约测试 | 新建 |
| `web/templates/config.html` | 去死字段、补 v4 参数(结构化控件)、重写 load/save JS | 修改 |

---

## Task 1: 后端契约测试 `/api/settings`

**Files:** Create `tests/test_settings_routes.py`。

- [ ] **Step 1: 写测试**

新建 `tests/test_settings_routes.py`:
```python
"""tests/test_settings_routes.py — /api/settings GET/POST 的 v4 参数 round-trip 契约。

路由本就按键归类(引擎键->settings、策略键->默认模板)且 json.dumps 存值,故对
现有代码即通过。本测试把这一契约钉死,作为 SP6a 前端依赖的回归闸。"""

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


def test_get_settings_returns_v4_params(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    data = client.get("/api/settings").get_json()
    for k in (
        "theta_loss_cents", "theta_stop_cents", "case_a_mode", "tier_rules",
        "per_share_reward_thresholds", "excluded_categories", "max_exposure_usd",
        "max_exposure_shares", "max_concurrent_markets", "min_price_double_cents",
        "tiers_k", "discovery_interval_sec",
    ):
        assert k in data, f"GET /api/settings 缺 {k}"


def test_post_settings_roundtrips_structured(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    payload = {
        "theta_loss_cents": 3,
        "case_a_mode": "market",
        "excluded_categories": ["sports"],
        "per_share_reward_thresholds": {
            "20": 0.5, "50": 0.4, "100": 0.3, "200": 0.3, "250": 0.3,
        },
        "tier_rules": [
            [{"upper": None, "action": {"type": "fixed_shares", "shares": 50}}]
        ],
        "discovery_interval_sec": 7200,
    }
    resp = client.post("/api/settings", json=payload)
    assert resp.status_code == 200
    tmpl = db.get_template(db.get_default_template_id())
    assert tmpl["theta_loss_cents"] == 3
    assert tmpl["case_a_mode"] == "market"
    assert tmpl["excluded_categories"] == ["sports"]
    assert tmpl["per_share_reward_thresholds"]["20"] == 0.5
    assert tmpl["tier_rules"] == [
        [{"upper": None, "action": {"type": "fixed_shares", "shares": 50}}]
    ]
    assert db.get_settings()["discovery_interval_sec"] == 7200


def test_post_settings_routes_engine_vs_template(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    client.post("/api/settings", json={"scan_interval_sec": 45, "max_exposure_usd": 99})
    assert db.get_settings()["scan_interval_sec"] == 45
    assert db.get_template(db.get_default_template_id())["max_exposure_usd"] == 99
    # 不串味:引擎键不进模板、策略键不进引擎
    assert "scan_interval_sec" not in db.get_template(db.get_default_template_id())
    assert "max_exposure_usd" not in db.get_settings()
```

- [ ] **Step 2: 运行**

Run: `python -m pytest tests/test_settings_routes.py -v`
Expected: PASS（3 passed）。路由已支持 v4 键,无需改后端;若任一断言 FAIL,说明路由对该键归类/序列化有问题,**修 `web/routes.py` 的 `api_save_settings`/`api_get_settings`** 使其通过(预期不需要)。

- [ ] **Step 3: 全套无回归**

Run: `python -m pytest -q`
Expected: ALL PASS（405 + 3 = `408 passed`）。

- [ ] **Step 4: Commit（不 stage `.claude/settings.local.json`）**

```bash
git add tests/test_settings_routes.py
git commit -m "test(routes): /api/settings v4 参数 round-trip 契约(引擎/模板归类)"
```

---

## Task 2: config.html 对齐 v4

**Files:** Modify `web/templates/config.html`(策略参数/运行参数表单 + loadSettings + submit/input 处理器;钱包管理段、弹窗、钱包 JS **不动**)。

- [ ] **Step 1: 替换设置表单 section（HTML）**

把当前从 `<section class="config-section">`（含 `<h2>策略参数</h2>`，约第 21 行）到其闭合 `</section>`（约第 92 行,即 `保存设置` 按钮后）的**整个 section**,替换为:

```html
<section class="config-section">
    <h2>策略参数（默认模板）</h2>
    <form id="settings-form">
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

        <h2>运行参数（引擎全局）</h2>
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
        <button type="submit" class="btn btn-primary">保存设置</button>
    </form>
</section>
```

> 钱包管理 section、`#wallet-modal` 弹窗都在这个 section **之前/之后**,保持原样不动。

- [ ] **Step 2: 替换 `loadSettings`（JS）**

把现有 `loadSettings` 函数(约 115-125 行)整体替换为:
```javascript
function loadSettings() {
    fetch('/api/settings').then(r => r.json()).then(data => {
        originalSettings = {...data};
        currentSettings = {...data};
        const form = document.getElementById('settings-form');
        // 标量 + case_a_mode(select):通用回填
        Object.keys(data).forEach(key => {
            const input = form.querySelector(`[name="${key}"]`);
            if (input) input.value = data[key];
        });
        // excluded_categories(list):复选框
        const excluded = data.excluded_categories || [];
        document.querySelectorAll('#excluded-categories input[type=checkbox]').forEach(cb => {
            cb.checked = excluded.includes(cb.value);
        });
        // per_share_reward_thresholds(dict):5 档
        const ps = data.per_share_reward_thresholds || {};
        document.querySelectorAll('#per-share-thresholds input[data-bracket]').forEach(inp => {
            const b = inp.getAttribute('data-bracket');
            inp.value = (ps[b] !== undefined) ? ps[b] : 0.30;
        });
        // tier_rules(list):JSON 美化
        document.getElementById('tier-rules-json').value =
            JSON.stringify(data.tier_rules || [], null, 2);
    });
}
```

- [ ] **Step 3: 替换 submit 与 input 处理器（JS）**

把现有的 submit 监听器(约 228-243 行)与紧随其后的 input 监听器(约 245-250 行)**两段一起**替换为:
```javascript
document.getElementById('settings-form').addEventListener('submit', function(e) {
    e.preventDefault();
    const data = {};
    // 标量数字
    this.querySelectorAll('input[type=number][name]').forEach(inp => {
        data[inp.name] = parseFloat(inp.value);
    });
    // case_a_mode(select 字符串)
    const caseSel = this.querySelector('select[name="case_a_mode"]');
    if (caseSel) data.case_a_mode = caseSel.value;
    // excluded_categories -> list
    data.excluded_categories = Array.from(
        document.querySelectorAll('#excluded-categories input[type=checkbox]:checked')
    ).map(cb => cb.value);
    // per_share_reward_thresholds -> dict
    const ps = {};
    document.querySelectorAll('#per-share-thresholds input[data-bracket]').forEach(inp => {
        ps[inp.getAttribute('data-bracket')] = parseFloat(inp.value);
    });
    data.per_share_reward_thresholds = ps;
    // tier_rules -> JSON.parse(客户端校验)
    try {
        data.tier_rules = JSON.parse(document.getElementById('tier-rules-json').value);
    } catch (err) {
        alert('tier_rules JSON 格式错误，请检查后再保存');
        return;
    }
    fetch('/api/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data),
    }).then(r => r.json()).then(resp => {
        alert(resp.message);
        originalSettings = {...data};
        currentSettings = {...data};
    });
});

document.getElementById('settings-form').addEventListener('input', function() {
    // 仅跟踪标量 + case_a_mode 给「未保存离开」提醒;结构化控件无 name,不进比较
    this.querySelectorAll('input[type=number][name]').forEach(inp => {
        currentSettings[inp.name] = parseFloat(inp.value);
    });
    const caseSel = this.querySelector('select[name="case_a_mode"]');
    if (caseSel) currentSettings.case_a_mode = caseSel.value;
});
```

> 说明:结构化控件(复选框/per-share/textarea)无 `name` 属性,FormData/比较不含它们;`case_a_mode` 按字符串跟踪,修掉了旧代码对所有键 `parseFloat` 会把 `case_a_mode` 变 `NaN`、误触发未保存提醒的问题。

- [ ] **Step 4: 全套测试无回归**

Run: `python -m pytest -q`
Expected: ALL PASS（仍 `408 passed`;本任务纯前端,不改 Python,不增减测试)。

- [ ] **Step 5: 人工核对清单（前端无 JS 测试框架）**

启动 `python app.py`、登录后到「配置」页,逐项确认:
1. **死字段消失**:页面不再有「止损比例」「每钱包挂买单上限」「下单量」段(order_size_mode/custom)。
2. **新字段在并能载入**:θ_loss/θ_stop、离场方式下拉、敞口/份数/并发/双边下限/K、排除品类 3 复选、单份奖励 5 档、tier_rules JSON 文本框、市场发现间隔——刷新页面后都显示当前值。
3. **保存 round-trip**:改几个值(含勾选一个品类、改一档 per_share、改 case_a_mode)→ 保存 → 刷新 → 回填一致。
4. **tier_rules 校验**:把 JSON 改成非法(如删个括号)→ 点保存 → 弹「tier_rules JSON 格式错误」且**不提交**;改回合法 → 保存成功。
5. 钱包管理段(添加/删除/启停/存款地址弹窗)行为如常,未被影响。

- [ ] **Step 6: Commit**

```bash
git add web/templates/config.html
git commit -m "feat(config-ui): 配置页对齐 v4(去死字段+补θ/敞口/case_a/excluded/per_share/tier_rules JSON/discovery)"
```

---

## 验收 checkpoint（对应 spec §四）

1. 死字段(stop_loss_pct/max_buy_orders_per_wallet/order_size_*)从配置页消失:Task2 Step1(HTML 不含)+ Step5.1。
2. 全部 v4 策略参数 + discovery_interval_sec 可编辑/保存/回填:Task2 Step1-3 + Step5.2-3。
3. 结构化参数正确 round-trip:Task1 后端契约 + Task2 Step5.3。
4. tier_rules 非法 JSON 客户端拦截:Task2 Step3(try/catch)+ Step5.4。
5. `/api/settings` 路由未改:Task1 对现有代码即通过即证。
6. `pytest` 全绿:Task1 Step3 / Task2 Step4。

## 范围之外

SP6b 多模板 CRUD + 钱包绑定 · SP6c tier_rules 可视化编辑器 · SP6d 死字段代码清理。
