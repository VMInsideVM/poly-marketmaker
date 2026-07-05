# 删除多档做市 / 单份奖励阈值 / 累积厚度 —— 设计

日期：2026-07-05

## 背景

用户已经把做市策略定死为 `gap_single`（断层单档）。laddering（多档做市）、单份奖励阈值筛选、以及只服务 laddering 的 `tier_match_var`（累计厚度 / 风险系数模式选择器）都不再使用，却仍散布在配置默认值、下单引擎、扫描器、路由、配置页 UI 和测试里，构成维护负担，也挡着后续「按配置提前预筛」的扫描提速。

目标：把这三块**彻底删除**，让 gap_single 成为唯一下单路径，且 gap_single 行为一字不变。

## 已定决策

- 删除深度：**彻底删**——代码路径、配置键、`TEMPLATE_DEFAULTS` 默认值、配置页 UI、相关测试全删。
- `placement_mode` 键**整个删掉**，gap_single 成为无条件路径（不保留单值选择器）。
- `amount_value_table`（金额数值表）**保留**：它是 gap_single 算系数的核心输入，与 `tier_match_var` 无关。
- 市场发现列表的「单份奖励」列**整列删掉**，`_derive_per_share` 里所有 `per_share*` 字段不再计算。
- DB 里已存的死键（模板 `template_settings` 中的 `tier_rules` / `per_share_reward_*` / `tier_match_var` / `placement_mode` / `min_price_double_cents` 行）**留着不动**——代码不再读取，惰性无害，不写迁移清理。
- 扫描提速（结算窗口/份额预筛提前到抓订单簿之前）**本次不做**，删除完成后另起一轮。

## 不变量（成功判据）

1. gap_single 的下单输出逐字节不变（`compute_market_single_orders` 及其依赖链未改）。
2. `amount_value_table` 及 gap 参数（`gap_wide_cents` / `gap_mid_cents` / `gap_high_coeff_sum_min` / `rule1~3_min_coeff`）全部保留、行为不变。
3. 删除完成后 `pytest` 全绿。
4. 配置页可正常渲染保存；市场发现「展开预演」在 gap_single 下正常出结果；引擎经 gap_single 正常下单（人工走查）。

## 保留 vs 删除（`engine/laddering.py`）

已核实 gap_single 的 `compute_market_single_orders` 与 `preview_gap_single_market` 均不依赖任何 laddering 专用函数。

保留：`amount_value`、`explain_gap_single_order`、`plan_gap_single_order`、`_GAP_RULE_LABEL`、`gap_single_reason`、`gap_single_price_basis`、`compute_market_single_orders`、`reconcile_buy_orders`（gap_single 也在用）、`preview_gap_single_market`。

删除：`build_ladder`、`_interval_action`、`resolve_tier_share`、`compute_market_ladders`、`apply_double_sided_floor`、`_verbose_levels`、`preview_market_ladders`。

## 删除清单（逐文件）

### A. 单份奖励阈值（per_share）
- `engine/scanner.py`：`reward_bracket()` 函数；`filter_for_template` 里的 per_share 检查段（`per_share_reward_enabled` 分支）。
- `config.py::TEMPLATE_DEFAULTS`：`per_share_reward_enabled`、`per_share_reward_thresholds`。
- `web/routes.py`：`from engine.scanner import reward_bracket` 导入；`_derive_per_share` 里 `per_share` / `per_share_bracket` / `per_share_threshold` / `per_share_ok` 四字段与其 `reward_bracket`/阈值取值（保留同函数里奖励范围/盘口价差/方向的展示计算，函数名酌情保留以减少改动面）。
- `web/templates/config.html`：「单份奖励阈值」整块（h3 + 启用勾选 + 档位阈值 + 说明）及其 JS（`updatePerShareEnabled`、`per-share-enabled` 相关、保存时 `per_share_reward_*` 的收集）。
- `web/templates/markets.html`：「单份奖励」列表头 + 单元格渲染（`$value 档X·≥阈值 ✓/✗`）。
- 测试：`test_scanner.py`（`reward_bracket` 用例、per_share 筛选用例）、`test_eligible_fields.py` / `test_markets_route.py` 中的 `per_share_reward_thresholds`、`test_database.py`（`per_share_reward_enabled`/`per_share_reward_thresholds` 断言）、`test_settings_routes.py` 相关键。

### B. 多档做市（laddering）+ 累积厚度（tier_match_var）
- `engine/laddering.py`：删除上节列出的 7 个 laddering 专用函数。
- `engine/manager.py::place_orders`：删除 laddering 分支（`compute_market_ladders` / `apply_double_sided_floor` 调用、`else` 支）、`placement_mode`/`tier_rules`/`tier_match_var`/`min_price_double_cents` 的读取与「空 tier_rules 闸」告警；gap_single 变为无条件执行；清理相应 import。
- `config.py::TEMPLATE_DEFAULTS`：`placement_mode`、`tier_rules`、`tier_match_var`、`min_price_double_cents`。
- `web/routes.py::_ladder_payload`：删除 laddering 分支（`preview_market_ladders`、`tier_rules`、`tier_match_var`），只留 gap_single 预演；清理相应 import。
- `web/templates/config.html`：挂单模式下拉（`placement-mode`）、区间变量下拉（`tier-match-var`）、多档挂单规则可视化编辑器、`min_price_double_cents` 输入，及其全部 JS（`updatePlacementMode`、`updateTierMatchVar`、`renderTierEditor`、`serializeTierRules`、`tierMatchVarLabel`、保存时相关键收集）。gap_single 的断层参数区块保留、并移除依赖 `placement_mode` 的显隐切换逻辑（改为常显）。
- `web/templates/markets.html`：展开预演的 laddering 表分支（`tier_match_var` / 累计厚度 / 有效价格 那套），只留 gap_single 预演表；指标说明里「梯队 / 累计厚度」措辞改写为 gap_single 口径。
- 测试：`test_laddering.py`、`test_laddering_preview.py` 整删；`test_place_orders.py`（laddering 相关：`tier_rules`/`min_price_double_cents`/空 tier_rules 闸用例）、`test_settings_routes.py` / `test_templates_routes.py` / `test_database.py` 中 `tier_rules`/`tier_match_var`/`placement_mode`/`min_price_double_cents` 相关断言。gap_single 的下单/预演测试全部保留。

## 执行顺序（分两个可独立验证的删除单元）

1. **删 per_share（A）** → 跑 `pytest`，全绿。
2. **删 laddering + tier_match_var（B）** → 跑 `pytest`，全绿。
3. **收尾**：全库 grep 已删标识符（`compute_market_ladders`/`apply_double_sided_floor`/`preview_market_ladders`/`tier_rules`/`tier_match_var`/`placement_mode`/`per_share_reward`/`reward_bracket`/`min_price_double_cents` 等）确认生产代码零残留（docs/ 历史规范文档不动）；`pytest` 全绿；人工走查配置页 + 市场发现预演 + 一轮下单。

分两单元是为了每步都能独立回到全绿，出问题时定位面小。

## 范围外（本次不做）

- 扫描提速预筛（结算窗口/份额提前）。
- 手动扫描路径的同款预筛。
- DB 死键的迁移清理（留惰性）。
- `docs/` 下历史 spec/plan（SP2/SP4/SP6 等）不改，作历史记录。

## 风险

- **误删连坐**：已核实 gap_single 链路不依赖 laddering 件；收尾 grep 兜底。
- **配置页 JS 断链**：删 UI 块要连同其 `onchange`/保存收集一起删，避免残留 JS 引用已删 DOM id 报错；人工走查配置页保存作验证。
- **`_ladder_payload` 退化**：删 laddering 分支后需确保 gap_single 分支对「DB 落库形态只有单 token_id」的降级路径仍成立（该逻辑在分支之前，已覆盖）。
