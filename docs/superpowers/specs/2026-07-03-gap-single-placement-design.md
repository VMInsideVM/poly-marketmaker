# 网关式单档挂单 + 浮盈市价离场

日期：2026-07-03
状态：设计已确认，待写实现计划

## 背景

用户提出一套完整做市策略。筛选侧（奖励≥30 美金、结算日期≥1 天、奖励最低份数=20、买一 10–31 美分、品类白名单）现有配置项已能覆盖。缺口在两处，本 spec 解决：

1. **挂单选择**：按买单簿「相邻档位价差」把市场分三级，宽断层再加一道「高位系数和」市场门槛，最终每市场只挂**一单**，用「系数 > x」自上而下顺延选一档。现有多档做市引擎（laddering / tier_rules）表达不了这套形状。
2. **卖单**：浮盈（成本 < 买一）立即市价卖出兑现；保本/套牢（成本 ≥ 买一）挂成本价等回本；**完全关闭止损**。现有 `plan_exit` 浮盈是挂卖一做 maker，且总带 B0 强平。

用户 2026-07-03 逐条确认了以下口径：
- 断层看**奖励区间内**相邻档的**最大**价差。（⚠️ 2026-08-14 改为看**整个买单簿**，见文末「追加（2026-08-14）」。）
- 「高位买方加起来」= 断层上方各档**风险系数之和**，默认门槛 20、可配。
- 选档三级**统一**用「自上而下、第一个风险系数 > x 的档」，x 可配。
- 卖单 = 浮盈市价卖 + 套牢挂成本 + 无止损（用户已知晓关止损后套牢仓无兜底的风险）。

## 范围

**做**：新增可选「挂单模式」`gap_single` 及其纯函数、预算封顶包装、manager 接入；`plan_exit` 新增「浮盈市价」止盈模式与「关闭」止损；对应配置项与配置页；单元/契约/回归测试。

**不做**：筛选侧改动（已可配）；分品类结算天数（需多模板多钱包，属另一件事）；通用脚本引擎（守 YAGNI）；单档模式的挂单份数加仓（固定最低份数）。

## 架构决策

新增**模板级开关** `placement_mode ∈ {"laddering"(默认), "gap_single"}`。

- 默认 `laddering`，现有 `build_ladder` / `compute_market_ladders` / `apply_double_sided_floor` / tier_rules 可视化编辑器**原样保留、零回归**；其他钱包/模板不受影响。
- 选 `gap_single` 的模板走新纯函数链。两条链并存、按模板二选一。
- 不塞进 `tier_rules`：那套是「多档并列 + 每档份额规则」，与「单档 + 断层市场门槛 + 顺延」形状冲突，硬塞会破坏既有模型且难测。独立纯函数最干净、可单测。

接入点：`engine/manager.py` 现有 `_place_round` 里 `compute_market_ladders(...)` + `apply_double_sided_floor(...)`（约 293–302 行）按 `placement_mode` 分支。`side` 字典（`token_id/outcome/min_size/bids/reward_range_min/reward_range_max/tick_size_str`）两条链共用，无需改造上游。撤改收敛 `reconcile_buy_orders`、成交后单侧暂停、预算/敞口扣减全部沿用——挂单随盘口移动、买一撤单自动跟随都不用改。

## A. 挂单选择 — `plan_gap_single_order`（`engine/laddering.py`，纯函数）

对**单个 token** 的买单簿返回 `(price, shares)` 或 `None`（不挂）。

签名：
```
plan_gap_single_order(
    bids,                      # [{"price","size"}...]
    reward_range_min, reward_range_max,
    min_size,                  # 该市场 rewards_min_size（本策略恒=20）
    amount_value_table,        # 复用 laddering.amount_value 的表
    gap_wide_cents, gap_mid_cents,
    gap_high_coeff_sum_min,    # 规则1 高位系数和门槛
    rule1_min_coeff,           # 规则1（宽断层）选档系数门槛
    rule2_min_coeff,           # 规则2（中断层）选档系数门槛
    rule3_min_coeff,           # 规则3（密盘）选档系数门槛
) -> (price, shares) | None
```

算法：
1. `min_size ≤ 0` 或无 bids → `None`。
2. **取奖励区间内买档**：`reward_range_min ≤ price ≤ reward_range_max`，按价降序。空 → `None`。
3. **每档风险系数** `coeff = size / (min_size × amount_value(price, table))`；`amount_value` 为空/≤0（价超表）时 `coeff = 0`（不入求和、也选不中）。
4. **最大相邻价差 + 劈分点**：对相邻在区间档算价差（美分）`gap_i = (price_i − price_{i+1}) × 100`；取最大 `max_gap` 及其位置 `split_idx`（高位 = `in_range[0 .. split_idx]`，即价差上方那一侧）。不足 2 档 → `max_gap = 0`。
5. **分级（整个市场归一级，选用该级门槛）**：
   - `max_gap > gap_wide_cents`（规则1，宽断层）：`high_sum = Σ coeff(高位各档)`；`high_sum < gap_high_coeff_sum_min` → `None`（整市场不挂）；否则用 `rule1_min_coeff` 进选档。
   - `gap_mid_cents ≤ max_gap ≤ gap_wide_cents`（规则2）：用 `rule2_min_coeff` 进选档。
   - `max_gap < gap_mid_cents`（规则3，正常）：用 `rule3_min_coeff` 进选档。
6. **选档（顺延）**：自最高在区间档往下，第一个 `coeff > 该级门槛` 的档 → 返回 `(该档价, int(min_size))`；都不满足 → `None`。

**下单份数固定 = `min_size`**（一单）。

边界口径（明确写死，供复核）：
- 价差落点：`10` 归规则2、`5` 归规则2（规则1 严格 `>gap_wide`，规则3 严格 `<gap_mid`）。
- 高位系数和：`< 门槛` 才拦，`== 门槛` 放行。
- 选档：严格 `> 该级门槛`（门槛应 ≥ 0；`coeff=0` 的档在门槛 `≥0` 时永不入选）。三级各用自己的门槛，互不影响。
- 「高位」= 最大价差上方（含上沿档）全部在区间档；多处并列最大价差时取**最靠上**那处（第一次出现）。
- 选档扫描**全部在区间档**（不限高位）：规则1 过闸后，若高位无档 >x 而低位有，仍挂低位——与「统一顺延」一致。高位系数和只作**市场级闸门**，不限制落档位置。

### 例（min_size=20；金额数值 ≤0.20→1 / ≤0.25→1.5 / ≤0.31→2；x=0；区间[0.12,0.30]）

买档降序 `0.28×50 / 0.27×800 / 0.15×400 / 0.10×100`：
- `0.10` 出区间剔除。在区间系数：`0.28→50/(20×2)=1.25`、`0.27→800/40=20`、`0.15→400/20=20`。
- 相邻价差 0.28→0.27=1¢、0.27→0.15=**12¢** → `max_gap=12 > 10` → 规则1，高位=｛0.28,0.27｝，`high_sum=21.25 ≥ 20` → 通过。
- 顺延：`0.28` 系数 1.25 > 0 → **挂 20 股 @ 0.28**。
- 反例：`0.27×800`→`0.27×30` 则 `high_sum=1.25+0.75=2 < 20` → 整市场不挂。

## B. 预算/敞口封顶 — `compute_market_single_orders`（`engine/laddering.py`）

与 `compute_market_ladders` 同风格、同输出形状 `{"a":[(price,shares)]|[], "b":[...]}`（每边至多一单）。

签名：
```
compute_market_single_orders(
    side_a, side_b,            # 与 compute_market_ladders 同结构或 None
    market_budget_usd, max_exposure_shares,
    amount_value_table,
    gap_wide_cents, gap_mid_cents,
    gap_high_coeff_sum_min,
    rule1_min_coeff, rule2_min_coeff, rule3_min_coeff,
) -> {"a":[...], "b":[...]}
```

逻辑：对每边调 `plan_gap_single_order`；得 `(price, shares)` 后按剩余预算/敞口封顶 `shares = min(shares, ⌊remaining_usd/price⌋, max_shares−spent)`；封顶后 `< side.min_size` → 放弃该边（沿用现有「不挂残档」）。a 先于 b 扣预算。**不套用** `apply_double_sided_floor`（那是 laddering §8 的双边地板；单档策略无此要求，且 10–31¢ 区间下阈值恒为 no-op）。

## C. 卖单 — `plan_exit` 新增 `take_profit_mode` + 止损可关（`engine/take_profit.py`）

- `take_profit_mode ∈ {"maker"(默认), "market"}`；`stop_loss_mode` 增加 `"off"`。
- `effective_theta_stop`：`stop_loss_mode == "off"` 时返回 `None`；`plan_exit` 收到 `theta_stop is None` 即**不触发 B0**。`check_exit`（monitor）从模板读 `take_profit_mode` 与 `stop_loss_*`，算出 `theta_stop` 一并传入 `plan_exit`。
- `plan_exit` 在 `take_profit_mode == "market"` 下：
  - 成本 **< 买一**（浮盈）→ `{"tier":"A_market","action":"market",...}`：走现有 `_exit_position` 的 `place_market_sell` + `market_fill_price` 记真实成交价（≈买一），仅 tier 标签换成止盈语义。市价卖仅在买一>成本时触发，**成交必 ≥ 成本**，「绝不低于成本」不变量成立。
  - 成本 **≥ 买一**（保本/套牢）→ 挂成本价 `ceil_to_tick(cost)`（`B_park`）。
  - `theta_stop = None` → 跳过 B0；无盘口 → `noop`（同现有）。
- `maker` 模式行为与现状完全一致（回归保护）。
- monitor 侧：`_exit_position` 已能处理 `action=="market"`；仅需按 tier（止盈 vs 止损）区分记录的 `action_type`/reason/状态行文案。现有「下单前钳价 ≥ 成本」「每次离场打 INFO 日志」保护保留。

## D. 配置项 + 配置页

`config.py TEMPLATE_DEFAULTS` 新增（均在 `/api/settings`、`/api/templates/<id>` 的 `TEMPLATE_DEFAULTS` 白名单内，自动存取，无需改路由契约）：

| key | 默认 | 含义 |
|---|---|---|
| `placement_mode` | `"laddering"` | 挂单模式 |
| `gap_wide_cents` | `10` | 宽断层阈值（美分） |
| `gap_mid_cents` | `5` | 中断层阈值（美分） |
| `gap_high_coeff_sum_min` | `20` | 规则1 高位风险系数和门槛 |
| `rule1_min_coeff` | `0` | 规则1（宽断层）选档系数门槛 |
| `rule2_min_coeff` | `0` | 规则2（中断层）选档系数门槛 |
| `rule3_min_coeff` | `0` | 规则3（密盘）选档系数门槛 |
| `take_profit_mode` | `"maker"` | 止盈方式 |
| `stop_loss_mode` | 增 `"off"` | 止损方式加「关闭」 |

`amount_value_table` 默认最高档上界 `0.30 → 0.31`（值仍 2），使 30–31¢ 档有金额数值可算（否则该档 `coeff=0` 选不中，与 31¢ 买一区间矛盾）。此改动对 laddering 风险系数模式仅极微扩宽，无害。

配置页 `web/templates/config.html`：
- 加 `placement_mode`、`take_profit_mode` 两个 `<select>`；`stop_loss_mode` 加 `<option value="off">关闭止损</option>`。
- 加断层参数输入（`gap_wide_cents/gap_mid_cents/gap_high_coeff_sum_min` + 三级选档门槛 `rule1_min_coeff/rule2_min_coeff/rule3_min_coeff`）。
- JS 按模式 show/hide：`gap_single` 显示断层参数、隐藏 tier_rules 编辑器（金额表仍显示，风险系数要用）；`stop_loss_mode=="off"` 隐藏比例/固定阈值输入；`take_profit_mode` 独立。
- **保存 JS 显式收两个新 select**（既有坑：select 不在默认序列化路径，参照 `stop_loss_mode` 的收法）。
- 断层参数与止损「关闭」旁加 `⚠️` 风险提示文案。

## E. 测试

- `tests/` 新增 `plan_gap_single_order` 纯函数用例：三级分级；规则1 求和门槛（`<20` 跳过 / `≥20` 挂 / `==20` 放行）；顺延取首个 `>x`；全不满足跳过；区间过滤；金额表 None→coeff 0；`<2` 档当正常；边界（gap=10/5、x 严格 >）；并列最大价差取最上。
- `compute_market_single_orders`：预算/敞口封顶、封顶后不足 min_size 跳过、a 先扣 b 后、双边/单边。
- `plan_exit` market 模式：浮盈→market、成本==买一→挂成本、套牢→挂成本、`theta_stop=None`→无 B0、`maker` 模式回归不变、止损 on 时 B0 照常。
- 契约测试：`/api/settings`、`/api/templates/<id>` 往返新键不丢。
- 回归：现有 laddering 全套测试保持全绿（默认模式不变）。

## 预演页（次要，可作最后增量）

`/api/markets/<id>/ladder`（市场发现「梯队预演」）当前恒走 `preview_market_ladders`。`gap_single` 模板打开会展示误导性的多档预演。取舍：
- 首选：加 `preview_gap_single_market`（返回逐档 price/size/coeff/是否高位/命中或跳过原因、max_gap、规则级、门槛结果），路由按 `placement_mode` 分支；前端加相应渲染。
- 若压缩范围：路由在 `gap_single` 时返回带 `mode` 标记的简版（选中档 + 规则级 + 门槛结果），前端顶部加「当前为网关式单档模式」提示。
排在实现计划最后；核心（A/B/C/D + 测试）不依赖它。

## 风险与留档

**关闭止损**：套牢仓（成本 > 买一且不回本）将一直挂成本价、无 B0 兜底，极端行情可扛到归零。与系统历史上多次救过用户的强平保护相反，属用户明确选择。缓解：配置页醒目 `⚠️` 提示；发布说明写清；保留「下单前钳价 ≥ 成本」与离场 INFO 日志取证。浮盈市价卖本身安全（仅买一>成本时触发，成交必 ≥ 成本）。

## 验收标准

1. 某模板设 `placement_mode=gap_single`，其钱包按 A/B 逻辑每市场至多挂一单、断层分级与顺延正确；默认模板行为不变。
2. 该模板设 `take_profit_mode=market` + `stop_loss_mode=off`：浮盈市价清仓、套牢挂成本、无强平。
3. 配置页可编辑并持久化全部新键；`maker`/`laddering` 组合行为与现状一致。
4. 新增测试通过，既有测试全绿。

## 追加（2026-07-04）：断层单档处理透明化

用户要求历史/前端能看到「每个市场按断层单档规则怎么判、判的原因、价格依据来源」。原实现只有挂成功的市场记一条 `place_buy`，且原因是写死的 laddering 文案（「多档…累计厚度」），跳过的市场无任何记录。

核心：抽出**一个纯函数产出完整判断**，记账/预演都从它格式化，避免断层逻辑在三处各写一遍。

- **`explain_gap_single_order`**（`engine/laddering.py`）：`plan_gap_single_order` 的底座，返回完整决策 dict（`action/rule/max_gap/min_coeff/high_sum/gate_passed/levels（逐档 price/size/coeff/high_side/chosen）/chosen_index/price/skip_reason`）。`plan_gap_single_order` 改成它的薄壳（行为不变，既有测试全绿）。
- **`gap_single_reason` / `gap_single_price_basis`**：把决策格式化成中文原因/价格依据。
- **① place_buy 正确记账**：`manager.place_orders` gap_single 分支对每边算 `explain`，`_record_place_buy_tier` 加可选 `reason/price_basis`，挂单时传断层真实原因（规则级·最大断层·高位系数和·顺延第几档·系数>门槛）；laddering 文案不变。
- **② 跳过留痕**：判成不挂的边记 `gap_skip` 动作，**按 token 去重**（`WalletWorker._last_gap_skip`，判断变化才记），避免每轮下单往历史刷同一条。history.html `ACTION_LABELS` 加 `gap_skip`。
- **③ gap_single 预演**：`preview_gap_single_market`（`engine/laddering.py`）+ `/api/markets/<id>/ladder` 按 `placement_mode` 分支；markets.html `renderGapSingle` 渲染逐市场判断（逐档系数表 + 高位/选中标记 + 规则/闸门/选中或跳过原因）。替掉了原「仅供参考盘口」占位。

测试：`explain`/formatter/`preview` 纯函数用例 + manager 记账（挂单原因、跳过去重、laddering 回归）。

---

## 追加（2026-08-14）：断层口径改为整个买单簿

用户报实盘市场 `0xe457df06…`（美国 6.0+ 地震）挂进了 21¢，而按他的预期规则1 的厚度闸门应该拦住它。

查下来不是实现走偏，是原口径盖不住这个形状。当时的盘口是买 21¢/236.24、19¢/120、8¢/9.14、7¢/16.35，奖励区间 `[19.5¢, 27.5¢]` 里只剩 21¢ 一档。原口径只在区间内算相邻价差，一档就无价差可算，`max_gap = 0` 直接落到最松的规则3；19¢ 往下那个 11¢ 的深坑整个不参与判定。悬崖闸也没拦住：`cliff_probe_cents = 2` 只往下探到 17.5¢，19¢ 有档就算「有支撑」。

**改动**：`max_gap` / `split_idx` / 高位系数和一律在**全部买档**上算（含奖励区间外的档），选档仍只在奖励区间内顺延。`levels` 随之改成全簿逐档并新增 `in_range` 标记，`chosen_index` 是 `levels` 的下标。

用户在知道下面两个副作用后仍选了这个口径（备选方案是「区间内不足 2 档时按规则1 处理」，只动这一个形状）：

- **规则2/3 实质废弃**：真实买单簿低价区（1 到 8¢）几乎总有零散挂单，全簿一算必然冒出 >10¢ 的断层，绝大多数市场都会归到规则1。区间内 21/20/19 连续厚档的密盘也不例外。
- **闸门会被区间外的挂量撑松**：「高位」= 断层上方全部买档，断层越靠下，高位纳入的区间外档越多、`high_sum` 越大越容易过闸。两个区间内形状完全相同（都只剩 21¢ 一档）的市场，可能因区间外挂量不同而一个拦一个放。

实测四个形状（金额数值=1、闸门 20、最低份数 20）：

| 买单簿 | 旧口径 | 新口径 |
|---|---|---|
| 21/236 · 19/120 · 8/9 · 7/16（用户这个） | 规则3 → 挂 | 规则1 gap=11¢ 高位和17.8 → **不挂** |
| 21/236 · 19/120 · 18/50 · 5/300 · 4/500 | 规则3 → 挂 | 规则1 gap=13¢ 高位和20.3 → 挂 |
| 21/200 · 20/180 · 19/160 · 2/5 | 规则3 → 挂 | 规则1 gap=17¢ 高位和27.0 → 挂 |
| 21/300 · 6/400 · 5/500 | 规则3 → 挂 | 规则1 gap=15¢ 高位和15.0 → **不挂** |

**波及展示层**：`gap_single_price_basis` 的逐档展开改标题为「买单簿(价降序)」并给区间外的档标 `[区间外]`（原文案写「区间内买档」，全簿口径下会骗人）；markets.html 预演表加「奖励区间」列、区间外的行压暗；config.html / help.html 的分级说明同步改口。

**上线注意**：这是行为改变，挂单量大概率下降，发版公告要写。已配的 `gap_wide_cents` / `gap_mid_cents` / 三级选档门槛都没动，但它们的实际生效面变了：规则1 的两个参数从此承担几乎全部市场。
