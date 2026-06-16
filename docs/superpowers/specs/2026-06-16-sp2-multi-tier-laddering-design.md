# SP2：多档挂单（multi-tier laddering）设计 / spec

> 日期：2026-06-16
> 状态：待用户评审
> v4 做市策略接入的第二个子项目（核心 IP）。父背景见 SP1 spec 与记忆 [[v4-strategy-integration-roadmap]]。

## 零、背景与定位

SP1 已完成模板/配置解耦 + 采集器拆分。本子项目把决策核心从「单边单单」换成 v4 §5 的**多档挂单**：一侧最多 K 个买单，分布在从买一往下的 K 个有效价格上，每档的份额由「累加厚度 → 份额动作」规则表决定。这是 v4 的核心知识产权。

SP2 范围（一次端到端做完）：
- v4 §5 多档定价算法（含 §5.0 五种份额动作、§5.1 逐档判断、§5.2 K 默认 6）。
- v4 §2 单市场敞口（≤250U 且 ≤500share）+ 最大并发做市市场数。
- v4 §8 <10¢ 强制双边的跨边耦合。
- 退役老 `determine_order_price` / `compute_order_size` / 扁平挂单数上限。

**不在 SP2**：三段式离场（SP3）、单份奖励阈值+取档（SP4）、三档节奏+观察名单（SP5）、模板编辑 UI（SP6）。SP2 内置一套默认规则表让机器开箱即跑；SP6 再做逐档编辑界面。

## 一、已定的关键决策

1. **内置默认规则表**：SP2 在 `TEMPLATE_DEFAULTS` 给一套能跑的默认（每档 `min_size`，见 §五）。SP6 加编辑 UI。
2. **下单上限按 v4 模型**：`最大并发做市市场数`（新模板参数，默认 10）限市场个数；每市场最多 K 档 × 两边；资金由单市场敞口封顶。**退役扁平的 `max_buy_orders_per_wallet`**。
3. **§8 <10¢ 双边耦合放进 SP2**。
4. **档规则表用半开升序区间** `[lo, hi)` 表示（上界开，null=∞），无缝覆盖由构造保证。刻意不实现 v4 §5.1 那种逐边界 `< / ≤ / > / ≥` 的完全自由度——恰好命中整数边界的累加厚度归下一区间，命中极罕见。
5. **单市场敞口作用于整市场**：YES+NO 合计 ≤250U 且 ≤500share，两边共享一个敞口预算。
6. **跨边预算遍历顺序** = 档序升序、同档先 YES 后 NO（档1-YES → 档1-NO → 档2-YES → 档2-NO …）。
7. **买一/档1 无特殊豁免**：档1 = 从买一往下第一个「在奖励范围内且厚度≥1」的档；买一若太薄/超区间则跳过顺延，与 §1「有效价格」定义一致。
8. **架构**：纯函数 laddering 引擎（`engine/laddering.py`）+ placement 层调用；多档**定份额在下单时**做（需 live 余额给 `wallet_total`/敞口）。

## 二、数据模型（模板新增/退役的策略级键）

**新增**（存 `template_settings` 的 JSON 值，经 `TEMPLATE_DEFAULTS` 合并）：

| key | 含义 | 默认 |
| --- | --- | --- |
| `tiers_k` | 档数 K | 6 |
| `tier_rules` | 长度 K 的列表；每项 = 该档「累加厚度区间 → 份额动作」规则表 | 见 §五 |
| `max_exposure_usd` | 单市场敞口（美元，整市场） | 250 |
| `max_exposure_shares` | 单市场敞口（份数，整市场） | 500 |
| `max_concurrent_markets` | 最大并发做市市场数 | 10 |
| `min_price_double_cents` | §8 触发双边的价格下限（美分） | 10 |

**退役**（从 `TEMPLATE_DEFAULTS` 移除；老库残留键无害、无人读）：`order_size_mode`、`order_size_custom_usd`、`max_buy_orders_per_wallet`。

**保留不动**（仍由 gating/监控使用）：`min_reward_usd`、`max_spread_cents`、`min_price_cents`、`max_price_cents`、`min_settlement_days`、`stop_loss_pct`、`excluded_categories`。

**单档规则表结构**（`tier_rules[i]`）= 升序区间列表，每个区间：

```json
{"upper": <累加厚度上界 float，null=∞>, "action": {...}}
```

区间为半开 `[前一上界, upper)`。份额动作五选一：
- `{"type":"fixed_shares","shares":N}`
- `{"type":"fixed_amount","usd":M}`
- `{"type":"min_size"}`
- `{"type":"wallet_total"}`
- `{"type":"skip"}`

## 三、多档算法（`engine/laddering.py`，纯函数，不触网）

### 3.1 单边构建（build_ladder）

输入：`bids`（按价降序的 `[{price,size}]`）、`reward_range_min`、`reward_range_max`、`min_size`、`tick_size`、`tiers_k`。

1. 从最高 bid 往下遍历价位。一个价位**合格为档**当且仅当：`reward_range_min ≤ price ≤ reward_range_max` **且** `size/min_size ≥ 1`（该价位厚度≥1）。
2. 档价梯 = 前 **K** 个合格价位（档1 = 最高合格价位，通常即买一）。不合格价位（超区间/太薄/空）跳过，不占档位。
3. 档价均为盘口已有价位，K 档对应 K 个互不相同的现有价格。

### 3.2 累加厚度（cumulative_thickness）

对档 j：`ct_j = Σ (level.size / min_size)`，对从买一往下、价位 ≥ 档 j 价格的**所有** bid 价位求和（即到档 j 的累加书深，含自己已在场旧单，§5.2 同一快照口径）。

### 3.3 单档份额（resolve_tier_share）

对档 j：取 `ct_j`，在 `tier_rules[j]` 找包含它的半开区间，按其动作算份额：
- `fixed_shares` → `N`
- `fixed_amount` → `⌊usd / price⌋`；若 `< min_size` 则取 `min_size`（§5.0）
- `min_size` → `min_size`
- `wallet_total` → `⌊remaining_budget / price⌋`（`remaining_budget` 由 placement 层跨档递减传入）
- `skip` → 不挂（0）

份额结果向下取整。

### 3.4 市场级两边合成 + 敞口（compute_market_ladders）

输入：YES/NO 两边的 `bids` 等、`tier_rules`、`market_budget_usd`、`max_exposure_shares`。

1. 两边各自 build_ladder → 档价梯 + 各档累加厚度。
2. 按**档序升序、同档先 YES 后 NO**遍历两边各档；对每档调 3.3 算份额，`wallet_total` 用当前 `remaining_usd`。
3. **敞口跨档封顶（整市场）**：维护已用 `spent_usd`、`spent_shares`；每档份额下调到不超过 `min(剩余U/price, 剩余shares)` 的整数；`remaining_usd = market_budget_usd − spent_usd`。某档被敞口压到 0 即不挂。
4. 输出 `{"yes":[(price,shares),...], "no":[...]}`，仅含份额>0 的档。

> `market_budget_usd` 由 placement 层传入 = `min(live 余额, max_exposure_usd)`。

### 3.5 <10¢ 双边闸门（apply_double_sided_floor）

对 3.4 的输出：若任一已定档的 `price*100 < min_price_double_cents`，则要求 YES、NO **两边都至少有一个份额>0 的档**；不满足 → 整市场清空（两边都不挂）。

## 四、placement 层（`engine/manager.py` `place_orders` 重写）

按市场逐个处理（候选已由 `filter_for_template` gate 过并按市场分组，见 §六）：

1. 跳过：黑名单、冷却、已持仓（held condition_id）。
2. **并发市场上限**：统计本钱包当前有挂单覆盖的不同 `market_id` 数；若已达 `max_concurrent_markets` 且本市场不在其中 → 跳过（不对新市场起单；已在做的市场照常）。
3. 读 **live 余额**；`market_budget_usd = min(余额, max_exposure_usd)`。
4. 调 `compute_market_ladders(...)` → 两边档计划；调 `apply_double_sided_floor(...)`。
5. 对每个已存在买单的 (token, price) 做幂等：已在该价位挂同向单则不重复（避免重挂）；其余 (price, shares) 调 `place_limit_buy`。
6. 记 `place_buy` 到 actions（理由含档号/累加厚度/份额动作来源）。

跨市场**不递减余额**（maker 买单不锁仓，一笔余额垫付所有市场挂单，与 SP1 一致）；敞口/预算扣减只在单市场内部跨档进行。

## 五、内置默认规则表

`tier_rules` 默认 = K（6）份相同的单区间表：

```json
[ [{"upper": null, "action": {"type": "min_size"}}], ...(共 6 份) ]
```

效果：奖励区间内从买一往下，每个有效档各挂**最小份数**，最多 6 档/边，由整市场敞口（250U/500share）封顶。安全（每档恒满足奖励门槛、敞口防超支）、且真正跑起多档以验证端到端。SP6 让用户配 §5.1 那种按累加厚度分段的高级规则（引擎已支持）。

## 六、filter_for_template 改造（只 gate、不定价）

`filter_for_template` 去掉 `determine_order_price`/`min_cost`/per-token 定价；改为：对每个候选市场做 gating（品类 narrow、奖励下限、结算天数、冷却），对每个 token 做价格区间+价差 gating，产出**按市场分组的合格 token 列表**（两边），交给 placement 层。eligible 条目轻量化（market_id/token_id/outcome/tick_size/neg_risk/rewards_min_size/reward_range/bids&asks 快照/tags/展示键），不含 order_price/order_size。

> 展示就绪键（market_name/daily_reward）保留（SP1 已加，前端 eligible 表用）。order_price 缺失，前端已兜底显示「—」。

## 七、退役清单

- `engine/strategy.py`：删除 `determine_order_price` 及三个 `_strategy_*` 私有函数；**保留** `reward_price_range`。
- `engine/order_sizing.py` `compute_order_size`：退役（删除文件或清空），`manager.py` 去掉 import 与调用。
- `tests/test_strategy.py`：老 `determine_order_price` 用例随之删除/迁移。
- `TEMPLATE_DEFAULTS` 移除 `order_size_mode`/`order_size_custom_usd`/`max_buy_orders_per_wallet`。
- **遗留**：`config.html` 仍有 `order_size_mode`/`order_size_custom_usd`/`max_buy_orders_per_wallet` 表单字段，SP2 后这些字段不再被 `api_save_settings` 路由（不在新旧 DEFAULTS 中即被忽略），成为死字段——UI 收口留给 SP6；SP2 仅在 spec 标注，不动 config.html（避免与 SP6 冲突）。

## 八、测试

- **`tests/test_laddering.py`（纯函数，核心）**：build_ladder（买一合格/跳薄档/跳出区间/≤K/0.1¢ tick）；cumulative_thickness；resolve_tier_share 五种动作各一例 + `fixed_amount` 不足 min_size 上调；compute_market_ladders 的敞口跨档封顶（U 与 share 两条）、`wallet_total` 跨档递减、两边共享预算 + 遍历顺序（同档先 YES 后 NO）；apply_double_sided_floor（一边触发 <10¢ 但另一边空 → 整市场清空；两边都有 → 保留）。
- **`tests/test_scanner.py`**：`filter_for_template` 改为只 gate、按市场分组，不含 order_price 的断言更新。
- **`tests/test_manager.py` / `test_place_orders.py`**：多档下单、敞口、并发市场上限、<10¢、已挂幂等，用 mock API。
- 删除 `tests/test_strategy.py` 的 `determine_order_price` 用例（reward_price_range 若有用例保留）。

## 九、验收 checkpoint

1. 用默认模板（每档 min_size）跑：单个市场在奖励区间内对买一往下的多个有效档各挂 min_size，受 250U/500share 整市场敞口封顶；两边都挂（双边报价）。
2. 配一个含 `wallet_total`/`fixed_shares`/`skip` 的非默认 tier_rules，纯函数单测证明按累加厚度分段产出正确份额、敞口/预算正确递减。
3. 某市场存在 <10¢ 档且一边无档 → 整市场不挂；两边都有 → 正常挂。
4. 并发市场数达上限后不对新市场起单。
5. `pytest` 全绿；老 `determine_order_price`/`compute_order_size` 已退役且无残留引用。

## 十、范围之外（留后续）

SP3 三段式离场 · SP4 单份奖励阈值+取档 · SP5 三档节奏+观察名单+成交后单侧暂停 · SP6 模板编辑 UI（含退役死字段收口、逐档规则表编辑）。
