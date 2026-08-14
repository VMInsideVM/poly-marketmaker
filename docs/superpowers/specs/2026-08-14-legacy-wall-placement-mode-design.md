# 老策略（v1.0.15 厚墙定价）作为可切换的挂单模式

日期：2026-08-14
状态：设计已确认，待实现

## 背景

v1.0.15 的挂单定价是「找厚墙，挂在墙的下一档」：在买单簿里找到一个挂量超过阈值的档位，把买单挂在它下面一档，让那堵墙替自己挡住砸盘。v4 那轮改造（2026-06）把它整个换成了多档做市，2026-07-05 又收敛成现在的 gap_single（断层分级 + 风险系数选档）。`determine_order_price` 在这个过程中被删除。

用户要把老策略移植回来，与现行策略并存、可切换。

两套策略的形状完全不同：老策略看**绝对挂量**找支撑墙，现行策略看**相对系数**（挂量 ÷ (最低份数 × 金额数值)）和**价差断层**。不存在把其中一个表达成另一个的办法，所以做成两个并列的挂单模式。

## 移植的定价规则

`determine_order_price(bids, max_spread, tick_size, reward_range_min, reward_range_max)`，按 tick 粗细与 `max_spread` 分三条路。`bids` 按价降序。

**A. 细 tick（`tick_size < 0.01`，即 0.1 美分盘）**
从买一往下累加挂量（`int(float(size))`），累计值首次 **>** `cumulative_threshold`（默认 6000）时停下，目标价 = **下一档**的价。没有下一档则不挂。目标价必须落在 `[reward_range_min, reward_range_max]` 内，否则不挂。累计始终没超过阈值也不挂。

**B. 粗 tick 且 `max_spread == 2`**
买一挂量 **>** `wall_threshold`（默认 2000）才挂**买二**；买一挂量 ≤ 阈值则整个市场不挂。只有一档买单时不挂。目标价必须落在奖励区间内。

**C. 粗 tick 且 `max_spread >= 3`**
先算下界 `min_price = 买一价 − max_spread × tick_size`。自上而下扫买档：价格低于 `min_price` 就停止扫描（超出可挂范围）；遇到第一个挂量 **>** `wall_threshold` 的档，目标价 = 它的下一档，要求 `目标价 >= min_price` 且落在奖励区间内。

**C 有一个容易写错的语义**：找到第一堵墙之后就定死了。如果那堵墙的下一档不满足条件（低于 `min_price`、出了奖励区间、或根本没有下一档），**直接不挂，不会继续往下找第二堵墙**。原版测试 `test_fallback_keeps_searching` 里的 "keeps searching" 指的是跳过**薄**档继续往下扫，不是墙不合格后另找一堵。

三条路的边界都是严格大于（`> 阈值`），等于阈值不算数。空买单簿不挂。

### max_spread 的取整

v1.0.15 的 scanner 调用时传的是 `int(rewards_max_spread)`，所以 `4.5` 会被截成 `4`，影响 C 路的 `min_price`。移植时**保留这个截断**，以求与原版逐字一致。

注意区分：**奖励区间本身**（`reward_price_range(midpoint, max_spread_cents)`）仍用不截断的 float 值算。`max_spread` 是美分不是 tick，这是本仓库踩过的坑（0.1 美分盘上用 `max_spread × tick_size` 会让区间窄十倍），不要因为这次移植把它带回来。

## 移植的挂单份数

`compute_order_size(mode, order_price, balance, min_size, custom_usd)`，三种模式：

- `min`：返回 `min_size`（市场的 `rewards_min_size`），恒满足奖励门槛。买不买得起由外层已有的 `min_cost` 门槛拦。
- `custom`：预算 = `min(custom_usd, balance)`，份数 = `floor(预算 ÷ 价格)`。
- `balance`：预算 = 全部余额，份数同上。

`custom` 和 `balance` 算出的份数若 **小于** `min_size`，返回 None 跳过该市场（份额不够拿奖励，挂了也吃不到）。`order_price <= 0` 一律跳过。未知模式按 `min` 兜底。

## 范围：移植什么，复用什么

移植回来的只有**定价**和**挂单份数**两件事。其余一律用当前程序的组件：

| 环节 | 用哪套 |
|---|---|
| 选品筛选 | 当前（含品类白名单、新市场保护、档位精确匹配、`min_cost` 门槛） |
| 挂单定价 | **v1.0.15 厚墙** |
| 挂单份数 | **v1.0.15 min/custom/balance** |
| 预算与敞口封顶 | 当前 |
| 买单撤改收敛 | 当前 |
| 持仓侧暂停、黑名单、UMA 结算守卫 | 当前 |
| 离场与止损 | 当前（成本逐笔重建、卖单永不低于成本） |
| 低余额清仓 | 当前 |
| 每钱包线程与代理 | 当前 |
| 盈亏台账、周报 | 当前 |

**明确不移植** `engine/risk.py`（按 Data API `avgPrice` 百分比止损）。`avgPrice` 作为成本来源在 2026-05-26 被禁用过：它在刚开仓的持仓上会给出错误值，导致低于成本卖出。现行离场从 `get_trades` 逐笔重建成本，是这个教训的产物。

### 档位模块在老策略下的角色

`size_tiers` 仍然是**选品门控**：市场的 `rewards_min_size` 必须精确等于某个已启用模块的 `size`，否则该市场不入选。这条来自当前的选品筛选，老策略模式下不变。

该模块的其余字段（`shares`、三级选档门槛、三级高位系数和门槛、金额数值表）在老策略下**全部忽略**：份数由 `order_size_mode` 决定，选档由厚墙规则决定。

实际效果：用户仍需启用至少一个档位模块来圈定「做哪些份额档的市场」，但只有 `size` 和 `enabled` 起作用。

## 新增参数（都是模板级）

| 键 | 默认 | 说明 |
|---|---|---|
| `placement_mode` | `gap_single` | `gap_single`（现行）/ `legacy_wall`（v1.0.15） |
| `legacy_wall_threshold` | `2000` | 1 美分盘的厚墙阈值 |
| `legacy_cumulative_threshold` | `6000` | 0.1 美分盘的累计阈值 |
| `order_size_mode` | `min` | `min` / `custom` / `balance` |
| `order_size_custom_usd` | `0` | `custom` 模式的美元上限 |

`placement_mode` 这个键名在 v3.3.0 存在过、v4.0.0 删除，现在按原语义复活。

默认 `gap_single` 意味着升级后不改配置行为完全不变。参数是模板级的，所以可以让一部分钱包跑老策略、另一部分跑现行策略做对比。

两个阈值默认值就是 v1.0.15 的写死常量，不改即等价于原版。

## 架构

新增 `engine/legacy_wall.py`（纯函数，不触网）：

- `determine_order_price(bids, max_spread, tick_size, reward_range_min, reward_range_max, wall_threshold=2000, cumulative_threshold=6000)`：v1.0.15 原样移植，两个常量提为带默认值的参数。
- `explain_legacy_order(...)`：返回完整决策 dict（命中哪条规则、哪一档触发阈值、累计值、目标价、份数、跳过原因），驱动记账与预演，与 gap_single 的 `explain_gap_single_order` 同构。
- `compute_market_legacy_orders(side_a, side_b, market_budget_usd, max_exposure_shares, ...)`：与现有 `compute_market_single_orders` **同形状**：每边至多一单，两边共享预算与敞口，a 先扣，封顶后不足 `min_size` 放弃该边，返回 `{"a": [(price, shares)], "b": [...]}`。

不放进 `engine/laddering.py`：该文件已经 460 行且是 gap_single 的家，两套策略混住会互相污染。

老策略与现行策略**形状本来就一致**（每边至多一单、两边共享敞口），所以 `manager.place_orders` 里按 `placement_mode` 的分支很薄，外层的预算封顶、撤改收敛、持仓侧暂停原样复用。

## 已知偏差：老模式不是 100% 的 v1.0.15

监控 Step 3 的三道复查在老策略下**仍会跑**，它们是独立风控，与用哪套定价无关：

- **悬崖复查**（奖励区间下沿往下 `cliff_probe_cents` 美分内没有买档就撤单，默认探 2¢）
- **实时奖励复查**（日奖励掉到 `min_reward_usd` 以下就撤单）
- **盘口价差复查**（价差超过 `max_spread_cents` 就撤单）

其中悬崖复查是 gap_single 时代引入的概念，v1.0.15 没有。想要纯 v1.0.15 行为，把 `cliff_probe_cents` 配成 `0` 即可关闭。

另外 Step 3 会检查在挂买单是否漂出当前的奖励带并撤单，这在 v1.0.15 也是有的（口径相同），不构成偏差。

## 记账与展示

- `place_buy` 的 `reason` / `price_basis` 由 `legacy_reason` / `legacy_price_basis` 生成，说明命中哪条规则、哪一档的挂量超过了阈值、目标价是哪一档。格式对齐现行的 `gap_single_reason` / `gap_single_price_basis`，让历史页两种模式的记录读起来一致。
- 判成不挂的边记 `gap_skip` 动作，复用现有动作类型与历史页标签，不新增类型。
- 预演页（`markets.html`）按 `placement_mode` 分支渲染：老策略显示逐档挂量、累计值、命中档、目标价，不显示断层与系数那套。
- 配置页按模式显隐参数块：`gap_single` 显示断层与三级门槛，`legacy_wall` 显示两个阈值与份数模式。

## 测试

移植 v1.0.15 `tests/test_strategy.py` 的 13 个定价用例（`TestMaxSpread2_TickSize1Cent` / `TestMaxSpread2_TickSize01Cent` / `TestMaxSpreadGE3_TickSize1Cent` / `TestMaxSpreadGE3_TickSize01Cent` 四组）。该文件里另有 5 个 `reward_price_range` 用例，当前仓库已有等价覆盖，不重复移植。

移植 v1.0.15 `tests/test_order_sizing.py` 的份数用例。

新增：

- 两个阈值可配（改阈值改变判定结果，默认值等价于 2000/6000）。
- C 路「找到第一堵墙就定死」的语义：墙的下一档超出 `min_price` 或出了奖励区间时不挂，且不继续找第二堵墙。
- `compute_market_legacy_orders` 的预算与敞口封顶、两边共享、封顶后不足 `min_size` 放弃该边。
- `place_orders` 按 `placement_mode` 分支：同一副盘口在两种模式下走不同定价路径。
- 配置页参数往返（新键存得进读得出）。
- 老策略下档位模块只用 `size` 做门控、`shares` 被忽略。

## 未决

无。
