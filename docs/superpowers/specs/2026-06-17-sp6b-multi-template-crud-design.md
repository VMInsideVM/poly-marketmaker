# SP6b：多模板 CRUD + 钱包绑定（multi-template management）设计 / spec

> 日期：2026-06-17
> 状态：待用户评审
> SP6（模板管理 UI）的第二个子块（SP6a 配置页对齐 v4 已合并）。父背景见记忆 [[v4-strategy-integration-roadmap]]。

## 零、背景与定位

模板 DB 层 SP1 已全建好：`templates`/`template_settings` 表、`wallets.template_id`、`create_template`/`list_templates`/`get_template`/`save_template`/`delete_template`（默认模板不可删，删时把绑定钱包 `template_id` 置 NULL）/`set_wallet_template`/`get_default_template_id`/`get_template_for`（钱包 NULL→默认）。`list_wallets`/`GET /api/wallets` 已返回 `template_id`。

**现状缺口**：无 `/api/templates` 路由；`api_add_wallet` 无模板入参；配置页只有「钱包表（无模板列）+ 单一参数表单（POST `/api/settings`→引擎 settings + 默认模板）」，没有建/改/切模板、没有钱包绑定模板的入口。

**SP6b 范围**：补 `/api/templates` 路由 + `rename_template` DB 方法 + 钱包绑定路由；配置页拆成「模板管理 + 策略参数（按选中模板）+ 引擎参数（全局）+ 钱包管理（加模板列）」。**策略参数从此按选中模板存取**；引擎参数仍全局。

**不在 SP6b**：tier_rules 可视化编辑器（SP6c，仍用 SP6a 的 JSON 文本框）；死字段代码清理（SP6d）。

## 一、已确认决策

| | 决策 |
| --- | --- |
| UI 位置 | 并入现有配置页分区(不新增页面/导航) |
| 钱包绑定 | 钱包表每行一个模板下拉,onchange 即 `POST /api/wallets/<addr>/template` |
| 重命名 | 支持(加 `db.rename_template` + 路由 + UI) |

## 二、DB（`models/database.py`）

唯一新增方法，放在 `save_template` 附近：
```python
def rename_template(self, template_id: int, name: str):
    c = self.conn.cursor()
    c.execute("UPDATE templates SET name = ? WHERE id = ?", (name, template_id))
    self.conn.commit()
```
重名由表级 `name UNIQUE` 约束在 commit 时抛 `sqlite3.IntegrityError`（路由捕获 → 400）。其余 DB 方法不动。

## 三、路由（`web/routes.py`，全部 `@login_required`，新增）

| 方法 路径 | 行为 | 失败 |
| --- | --- | --- |
| `GET /api/templates` | `[{"id","name","is_default"}]`(is_default = id==get_default_template_id()) | — |
| `POST /api/templates` `{name}` | `create_template` → `{"id","name"}` | 重名/空名 → 400 |
| `GET /api/templates/<int:tid>` | `get_template(tid)`(合并 TEMPLATE_DEFAULTS 的策略参数) | — |
| `PUT /api/templates/<int:tid>` `{策略键...}` | 只取 `k in TEMPLATE_DEFAULTS` → `save_template(tid, strategy)` | — |
| `PUT /api/templates/<int:tid>/name` `{name}` | `rename_template(tid, name)` | 重名/空名 → 400 |
| `DELETE /api/templates/<int:tid>` | `delete_template(tid)` | 删默认 → `ValueError` → 400 |
| `POST /api/wallets/<address>/template` `{template_id}` | `set_wallet_template(address, int(template_id))` | — |

要点：
- 创建/重命名捕获 `sqlite3.IntegrityError`（重名）与空名（`name.strip()` 为空）→ `400 {"error": "..."}`。
- 删除捕获 `ValueError`（默认模板不可删）→ `400`。
- `PUT /api/templates/<id>` 复用 `/api/settings` POST 的策略键过滤口径（`{k: v for k, v in data if k in TEMPLATE_DEFAULTS}`）。
- `/api/settings` **不动**：仍引擎 + 默认模板（SP6a 契约测试不变）；新 UI 策略参数走 `/api/templates/<id>`、引擎参数走 `/api/settings`（只发引擎键）。

## 四、config.html 重构（分区）

把 SP6a 的单一 `<form id="settings-form">`（引擎 + 策略混在一起）拆成**策略表单（按选中模板）**与**引擎表单（全局）**，并加模板管理段与钱包模板列。

### 4.1 模板管理段（新，置于策略参数段之前）
```
<h2>模板管理</h2>
当前模板:<select id="template-select">  …选项由 GET /api/templates 填,文本=name(默认模板加「(默认)」)…
<input id="new-template-name" placeholder="新模板名"> <button 新建>
<button 重命名>  <button id="delete-template" 删除>
```
- 载入：`GET /api/templates` → 填下拉；默认选中默认模板。
- 切换下拉 → `GET /api/templates/<id>` → 载入策略表单。
- 新建：`POST /api/templates {name}` → 成功后重载下拉并选中新模板（策略表单显默认值）。
- 重命名：弹 `prompt` 取新名 → `PUT /api/templates/<id>/name` → 重载下拉。
- 删除：`DELETE /api/templates/<id>` → 重载下拉、选默认；选中默认模板时删除按钮 `disabled`。
- 出错(400)`alert(resp.error)`。

### 4.2 策略参数段（SP6a 字段，改为按选中模板）
- 字段不变（min_reward_usd / max_spread_cents / min_price_cents / max_price_cents / min_settlement_days / theta_loss_cents / theta_stop_cents / case_a_mode / max_exposure_usd / max_exposure_shares / max_concurrent_markets / min_price_double_cents / tiers_k / excluded_categories / per_share_reward_thresholds / tier_rules）。
- 载入：来自 `GET /api/templates/<选中>`（不再 `/api/settings`）。复用 SP6a 的回填逻辑（标量/select/复选框/per_share/tier_rules JSON）。
- 保存（「保存策略参数」按钮）：构造策略 data（复用 SP6a 的构造 + tier_rules JSON.parse 校验）→ `PUT /api/templates/<选中>`。

### 4.3 引擎参数段（全局,不变行为）
- 字段：scan_interval_sec / fill_check_interval_sec / cooldown_minutes / rewards_cache_ttl_sec / discovery_interval_sec。
- 载入：`GET /api/settings`（取引擎键）。保存（「保存引擎参数」按钮）：只发引擎键 → `POST /api/settings`。

### 4.4 钱包管理段（加模板列）
- 钱包表 `<thead>` 加「模板」列；每行渲染一个 `<select>`：选项为模板列表，选中项 = `w.template_id`（为空/未知 → 默认模板）。
- `onchange` → `POST /api/wallets/<addr>/template {template_id}`。
- 渲染需要模板列表：`loadWallets` 先确保已取到 `templates`（与 `loadTemplates` 共用一份缓存，或各自 fetch）。

### 4.5 JS 结构
- 一份 `templatesCache`（id→name + 默认 id），`loadTemplates()` 填下拉 + 供钱包行下拉渲染。
- `loadStrategy(tid)`：GET 模板 → 回填策略表单。
- 策略保存 / 引擎保存各自 handler。
- 「未保存离开」提醒：保留现状即可（不强求覆盖多表单；YAGNI）。

## 五、测试

### 5.1 DB（`tests/test_database.py`）
- `test_rename_template`：建模板 → rename → `list_templates` 名变。
- `test_rename_template_duplicate_raises`：rename 到已存在名 → `sqlite3.IntegrityError`。

### 5.2 路由（新 `tests/test_templates_routes.py`，Flask test client + 真 Database，仿 `test_settings_routes.py`）
- `test_list_templates_includes_default`：GET → 含默认模板、`is_default` 标对。
- `test_create_get_save_roundtrip`：POST 建「激进」→ GET 返回默认值 → PUT 存 `{max_exposure_usd: 99, tier_rules: [...]}` → GET 反映。
- `test_create_duplicate_name_400`：再建同名 → 400。
- `test_rename_route`：PUT `/name` 改名 → list 名变；改成已存在名 → 400。
- `test_delete_template`：删非默认 → list 少一个;删默认 → 400。
- `test_bind_wallet_to_template`：add_wallet → `POST /api/wallets/<addr>/template {id}` → `db.get_template_for(addr)` == 该模板参数。
- `test_put_template_filters_non_strategy_keys`：PUT 带一个引擎键(scan_interval_sec)+ 策略键 → 只策略键进模板、引擎键被丢。

### 5.3 前端（无 JS 测试框架 → `node --check` + 人工核对清单）
1. 模板下拉列出模板;新建一个 → 出现在下拉并选中。
2. 切换模板 → 策略参数表单载入该模板值;改值「保存策略参数」→ 刷新/切走再切回,值还在该模板。
3. 重命名 → 下拉名变;删除非默认 → 消失;默认模板删除按钮禁用。
4. 钱包行模板下拉改选 → 刷新后保持;该钱包据新模板运行(逻辑层 get_template_for)。
5. 引擎参数段独立保存,不影响模板。

## 六、验收 checkpoint

1. 能建/改名/删/切模板（路由测试 + 前端清单 1/3）。
2. 策略参数按选中模板独立存取（路由 create_get_save_roundtrip + 前端 2）。
3. 钱包行下拉绑定模板、`get_template_for` 随之变（路由 bind 测试 + 前端 4）。
4. 引擎参数仍全局、`/api/settings` 未改（SP6a 契约测试仍过 + 前端 5）。
5. 默认模板不可删、重名拦截（路由 delete/duplicate 测试）。
6. `pytest` 全绿。

## 七、范围之外

SP6c tier_rules 可视化编辑器 · SP6d 死字段清理（held_condition_ids / needs_replace / strategy_check）。
