# SP6a：配置页对齐 v4（config page ↔ v4 params）设计 / spec

> 日期：2026-06-17
> 状态：待用户评审
> SP6（模板管理 UI）的第一个子块。父背景见记忆 [[v4-strategy-integration-roadmap]]。

## 零、背景与定位

v4 接入（SP1-SP5）已把策略参数全面换成 v4 模型，但**配置页 `web/templates/config.html` 严重过时**：

- **仍编 4 个已退役字段**：`stop_loss_pct`（SP3 退役）、`max_buy_orders_per_wallet`（SP2 退役）、`order_size_mode` / `order_size_custom_usd`（SP2 退役）。
- **缺全部 v4 新参数**：`theta_loss_cents`、`theta_stop_cents`、`case_a_mode`、`max_exposure_usd`、`max_exposure_shares`、`max_concurrent_markets`、`min_price_double_cents`、`tiers_k`、`tier_rules`、`per_share_reward_thresholds`、`excluded_categories`；引擎键 `discovery_interval_sec`（SP5a-2 新增）也没有。

结果：**用户现在没法用 UI 配置 v4 策略**（默认模板有可用默认值，引擎能跑，但改不了）。

**SP6a 范围**：把配置页对齐 v4——去死字段、补齐 v4 参数（含结构化的 list/dict/JSON）。**仍编默认模板**（`/api/settings` 现有 GET/POST 不变）。

**不在 SP6a**：多模板 CRUD + 钱包绑定（SP6b）；`tier_rules` 可视化编辑器（SP6c，本期用 JSON 文本框过渡）；死字段代码清理（SP6d）。

## 一、关键事实：路由不用改

- `GET /api/settings`（`web/routes.py` `api_get_settings`）= `get_settings()`（引擎，含 `discovery_interval_sec`）`.update(get_template(默认模板))`（策略，merge `TEMPLATE_DEFAULTS` → 含全部 v4 键）。**已返回所有 v4 参数**。
- `POST /api/settings`（`api_save_settings`）按键归类：`{k in ENGINE_DEFAULTS}` → `save_settings`；`{k in TEMPLATE_DEFAULTS}` → `save_template(默认)`。`save_template` 对每个值 `json.dumps`，故 list/dict 值天然 round-trip。**新 v4 键已在两个 DEFAULTS 里，无需改路由**。

所以 SP6a = 改 `config.html`（HTML + JS）+ 一个后端契约回归测试。

## 二、config.html 改动

### 2.1 删除（死字段）
- 「策略参数」段里：`止损比例 stop_loss_pct`、`每钱包挂买单上限 max_buy_orders_per_wallet` 两个 `form-group`。
- 整个「下单量」段（`<h2>下单量</h2>` 及其 `form-grid`，含 `order_size_mode` select、`order_size_custom_usd` input）。

### 2.2 「策略参数（默认模板）」段补齐 v4
标量 `<input type="number">`（name 与 `TEMPLATE_DEFAULTS` 键一致）：
| label | name | step |
| --- | --- | --- |
| 浮亏阈值 θ_loss (美分) | theta_loss_cents | 1 |
| 强平阈值 θ_stop (美分) | theta_stop_cents | 1 |
| 单市场最大敞口 (USD) | max_exposure_usd | 1 |
| 单市场最大份数 (share) | max_exposure_shares | 1 |
| 最大并发做市市场数 | max_concurrent_markets | 1 |
| 双边挂单价格下限 (美分，<此价要求两边都挂) | min_price_double_cents | 1 |
| 价格档数 K | tiers_k | 1 |

结构化控件：
- **`case_a_mode`**：`<select name="case_a_mode">`，选项 `ask`=「挂卖一（吃满价差，maker）」、`market`=「市价（贴买一立即清掉）」。
- **`excluded_categories`**：3 个 `<input type="checkbox">`，value 分别 `sports`/`esports`/`weather`，label 体育/电竞/天气。包一个 `id="excluded-categories"` 容器便于 JS 取。
- **`per_share_reward_thresholds`**：5 个 `<input type="number" step="0.01">`，`data-bracket` 分别 `20/50/100/200/250`，label「20 档」…「250 档」。包一个 `id="per-share-thresholds"` 容器。
- **`tier_rules`**：`<textarea id="tier-rules-json" rows="10">`，下方一行灰字提示（动作类型字符串须与引擎 `resolve_tier_share` 一致）：「JSON 格式：档位数组，每档是若干区间 `{"upper": 累加厚度上限或 null, "action": {...}}`（半开升序 `[前一上界, upper)`）；动作 type 五选一：`min_size`（最小份数）/`fixed_shares`（固定份数，带 `"shares"`）/`fixed_amount`（固定金额，带 `"usd"`）/`wallet_total`（钱包剩余预算全额）/`skip`（不挂）。例：`[[{"upper": null, "action": {"type": "min_size"}}]]`。留默认即每档挂最小份数。」

### 2.3 「运行参数（引擎全局）」段补
- 标量「市场发现间隔 (秒)」`<input type="number" name="discovery_interval_sec" step="1">`，下方灰字「默认 14400 = 4 小时；昂贵的全量奖励发现按此节奏，下单仍按扫描间隔」。

### 2.4 load JS（`loadSettings`）
- 标量 + `case_a_mode`（select）：沿用现有通用循环 `form.querySelector([name=key]).value = data[key]`（select 设 value 即选中）。
- `excluded_categories`（list）：显式——遍历 3 个复选框，`checked = data.excluded_categories.includes(value)`。
- `per_share_reward_thresholds`（dict）：显式——遍历 5 个输入，`value = data.per_share_reward_thresholds[bracket]`（缺则 0.30）。
- `tier_rules`（list）：显式——`textarea.value = JSON.stringify(data.tier_rules, null, 2)`。

### 2.5 save JS（submit handler）
不再对所有键 `parseFloat`。改为显式构造 `data`：
- 标量（含 θ、敞口、并发、min_price_double、tiers_k、引擎 4 项 + discovery_interval_sec）：`parseFloat`（用一个标量 name 白名单遍历，或对 `input[type=number]` 取值）。
- `case_a_mode`：select 字符串值。
- `excluded_categories`：收集勾选的复选框 value → 数组。
- `per_share_reward_thresholds`：读 5 个输入 → `{"20": parseFloat, ...}`。
- `tier_rules`：`try { JSON.parse(textarea.value) } catch { alert("tier_rules JSON 格式错误，请检查"); return; }`，解析成功才并入 `data`、继续 POST。
- 仍 POST 到 `/api/settings`，alert(resp.message)。
- 现有「未保存离开提醒」`beforeunload`/导航拦截基于 `originalSettings`/`currentSettings` 浅比较；结构化值改为浅比较可能误判（list/dict 引用），但不影响功能（最多多弹一次确认）。**保留现状不动**（YAGNI）。

## 三、测试

### 3.1 后端契约测试（新 `tests/test_settings_routes.py`）
用 Flask test client（仿 `tests/test_wallet_routes.py`）+ 真 `Database`（tmp 文件）挂到 `routes.db`、登录态：
- `test_get_settings_returns_v4_params`：GET `/api/settings` → 返回含 `theta_loss_cents`/`theta_stop_cents`/`case_a_mode`/`tier_rules`/`per_share_reward_thresholds`/`excluded_categories`/`max_exposure_usd`/`discovery_interval_sec` 等键（默认值）。
- `test_post_settings_roundtrips_structured`：POST 一组 v4 参数（含 `tier_rules`=自定义 list-of-lists、`per_share_reward_thresholds`={"20":0.5,...}、`excluded_categories`=["sports"]、`case_a_mode`="market"、`discovery_interval_sec`=7200、`theta_loss_cents`=3）→ 然后 `db.get_template(默认)` 反映策略键、`db.get_settings()` 反映 `discovery_interval_sec`。
- `test_post_settings_routes_engine_vs_template`：POST 同时含引擎键(`scan_interval_sec`)与策略键(`max_exposure_usd`)→ 引擎键进 `settings`、策略键进默认模板、互不串味。

### 3.2 前端（无 JS 测试框架 → 人工核对清单）
实现后按清单目视核对（写进 plan 的验收步骤）：
1. 配置页不再出现：止损比例、每钱包挂买单上限、下单量段。
2. 出现并能载入/保存：θ_loss/θ_stop、case_a_mode 下拉、敞口/份数/并发/双边下限/K、excluded 3 复选、per_share 5 档、tier_rules JSON、discovery_interval。
3. tier_rules 输入非法 JSON → 点保存弹错且不提交；合法 → 保存成功、刷新后回填一致。
4. excluded 勾选/per_share 改值 → 保存 → 刷新回填一致。

## 四、验收 checkpoint

1. 死字段（stop_loss_pct/max_buy_orders_per_wallet/order_size_*）从配置页消失。
2. 全部 v4 策略参数 + discovery_interval_sec 可在配置页编辑、保存、回填。
3. 结构化参数（tier_rules / per_share / excluded_categories）正确 round-trip（后端契约测试 + 前端回填核对）。
4. tier_rules 非法 JSON 被客户端拦截。
5. `/api/settings` 路由未改（仅靠现有键归类）；仍编默认模板。
6. `pytest` 全绿。

## 五、范围之外

SP6b 多模板 CRUD + 钱包绑定 · SP6c tier_rules 可视化编辑器 · SP6d 死字段代码清理（held_condition_ids / needs_replace / strategy_check 等）。
