# 档位模块化挂单配置 设计

日期：2026-07-13。状态：已与用户对齐口径，待实施。

## 背景与目标

挂单份数目前写死为市场最低奖励份额（`engine/laddering.py` 的 `d["shares"] = int(min_size)`），选档参数（规则 1/2/3 门槛、高位系数和门槛、金额数值表）是模板级全局一套。用户想按「市场最低奖励份额」分档：每档一个模块，自选挂单份数（≥ 档位值）和一套选档参数；勾选启用哪些档；没对上档的市场不挂。

例：启用「20 档（挂 40 份）」和「50 档（挂 50 份）」两个模块后，最低份额 20 的市场挂 40 份、用 20 档的参数判断；最低份额 50 的市场挂 50 份、用 50 档的参数；最低份额 30 的市场不挂。

## 已确认的决策

- **模块属于策略模板**：不同钱包绑不同模板即可差异化档位配置。
- **精确匹配**：市场 `rewards_min_size` 恰好等于某个已启用模块的 `size` 才挂，否则不挂。不做向上取档、不做区间。
- **每模块参数**：`size`、`enabled`、`shares`（≥ size）、`rule1_min_coeff`、`rule2_min_coeff`、`rule3_min_coeff`、`gap_high_coeff_sum_min`、`amount_value_table`。
- **仍为模板级全局**：断层分级阈值（`gap_wide_cents`/`gap_mid_cents`）、`cliff_probe_cents`、敞口/并发、筛选、离场参数——它们描述盘口形态与风险边界，与市场档位无关。
- **系数公式不变**：`coeff = 挂量 ÷ (市场最低份额 × 金额数值)`，与自己挂多少无关。
- **升级后模块列表为空 → 不挂单**，UI 醒目提示；发主版本号。

## 数据模型

模板新增键 `size_tiers`（JSON 数组），存 `template_settings`，走现有键值存储，不用改表结构：

```json
[{"size": 20, "enabled": true, "shares": 40,
  "rule1_min_coeff": 0, "rule2_min_coeff": 0, "rule3_min_coeff": 0,
  "gap_high_coeff_sum_min": 20,
  "amount_value_table": [{"upper": 0.20, "value": 1}]}]
```

校验（后端保存时 + 前端）：`size` 正整数且模板内唯一；`shares` 整数且 ≥ `size`；各门槛为数值且 ≥ 0；`amount_value_table` 沿用现有格式校验。

`config.py` `TEMPLATE_DEFAULTS`：加 `"size_tiers": []`；删 7 个被取代的全局键——`rule1_min_coeff`、`rule2_min_coeff`、`rule3_min_coeff`、`gap_high_coeff_sum_min`、`amount_value_table`、`rewards_min_size_min`、`rewards_min_size_max`（`routes.py` 按 `TEMPLATE_DEFAULTS` 白名单收发，删键即自动停收）。迁移：`models/database.py` 启动时 DELETE `template_settings` 中这 7 个旧键的行（照 SP6d 死字段清理的手法）。

## 筛选（scanner）

`engine/scanner.py` `filter_for_template`：`rewards_min_size_min/max` 范围检查（现 402-407 行）替换为 `min_size ∈ 启用模块的 size 集合`；集合为空 → 该模板 eligible 为空。

## 下单（manager / laddering）

`engine/manager.py` `place_orders`：

- 从模板读 `size_tiers`；每个市场按 `rewards_min_size` 查启用模块，查不到 → 跳过该市场（与黑名单/冷却同级的 continue）。筛选层理论上已挡住，但下单线程与配置变更、共享 eligible 列表之间有时差，照黑名单三拦截点的先例做双点防御。
- 传给断层判断的 `amount_value_table`、`gap_high_coeff_sum_min`、`rule1/2/3_min_coeff` 改从模块取；`gap_wide_cents`/`gap_mid_cents`/`cliff_probe_cents` 仍从模板取。

`engine/laddering.py` 四个纯函数（`explain_gap_single_order`、`plan_gap_single_order`、`compute_market_single_orders`、`preview_gap_single_market`）加 `shares` 形参（模块配置的挂单份数）：

- `d["shares"] = int(shares)`，替代现在的 `int(min_size)`。
- `coeff` 公式与奖励资格线仍用 `min_size`。
- `compute_market_single_orders` 预算/敞口封顶逻辑保留，放弃线仍是封顶后 `shares < side["min_size"]`——跌破市场最低份额（奖励资格线）才放弃；配置挂 40 份、预算只够 30 份时照挂 30。

## 预演

`/api/markets/<id>/ladder` 与手动扫描预演沿各自现有的模板解析路径拿到模板后，按市场 `rewards_min_size` 解析模块：有模块 → 用模块参数预演并展示配置份数；无匹配模块 → 展示跳过原因「无匹配档位模块（最低份额 X）」。

## 配置页 UI

`web/templates/config.html` 模板编辑区新增「档位模块」卡片列表（照 SP6c tier_rules 可视化编辑器的先例）：

- 每卡：档位值、启用勾选、挂单份数、规则 1/2/3 门槛、高位系数和门槛、金额数值表编辑器（复用现有金额表组件）；「添加档位」按钮与逐卡删除。
- 表单删掉被取代的全局字段：规则 1/2/3 门槛、高位系数和门槛、金额数值表、最低份额范围。
- 模板无启用模块时，配置页与仪表盘给醒目提示「未配置档位模块，不会挂单」。
- 含中文的前端文件由主会话直接 Write，写后 `node --check` + 查别字/BOM（惯例）。

## 版本与迁移

行为改变（升级后不配模块就不挂单）→ 主版本号。更新公告写明「升级后须到配置页为每个模板添加档位模块」。

## 测试

- laddering：`shares` 形参单测——挂单份数 = 配置值；封顶到 `[min_size, shares)` 区间照挂；封顶后 < `min_size` 放弃；`coeff` 仍按 `min_size` 算。
- `filter_for_template`：精确匹配命中/未命中/模块未启用/列表为空。
- routes：`size_tiers` 保存校验（size 唯一、shares ≥ size、类型错误拒收）契约测试；死键不再收发。
- 迁移：7 个旧键被删除。
- 预演：无匹配档位的跳过原因。

## 非目标

向上取档或区间匹配（用户明确选了精确匹配）、每档独立断层阈值/悬崖探测、按档位差异化敞口。
