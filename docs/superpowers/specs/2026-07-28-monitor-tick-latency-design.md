# 监控 tick 提速：把「奖励下跌 / 档位变化 → 撤单」的延迟压下来

2026-07-28

## 问题

用户实测：已挂单市场的奖励下跌之后，大约一分钟多才撤单。希望更快。

## 现状

监控线程一个 tick 严格串行，跑完才 `wait(5s)`（`engine/manager.py:118-127`），所以**真实周期 = tick 耗时 + 5 秒**，不是 5 秒。tick 的步骤顺序在 `manager.py:130-142`：

| 步骤 | 网络往返 |
|---|---|
| 1 `check_buy_orders` | `get_trades` ×1（有成交再 +1 `get_open_orders`） |
| 2 `check_resolution` | `get_open_orders` ×1 + Gamma ×1 |
| 3 `check_low_balance` | 余额 ×1（触发才继续） |
| 4 `check_exit` | positions + `get_open_orders` + Gamma 各 1，**外加每个持仓 2 次串行** |
| 5 `check_sell_orders`（Step 3，奖励/价差复查在此） | `get_open_orders` ×1 + 奖励 6 路并发 + 盘口 6 路并发 |

只有第 4 步是 O(持仓数) 且完全串行：`monitor.py:493` 逐仓调 `_exit_position`，每仓内部一次 `_cost_lots`（`monitor.py:166`，`get_trades(market=cid)`）加一次 `_sell_book`（`monitor.py:348`，`get_orderbook`）。15 个持仓就是 30 次串行往返。

奖励复查排在这一步后面。

### 为什么「把 Step 3 挪到前面」不管用

奖励什么时候变是随机的，而检测每个周期只发生一次。期望延迟是周期的一半，最坏是一整个周期。在 tick 内挪动相位不改变周期，所以这条路走不通。要压延迟只有两条：压周期，或者给 Step 3 自己的周期。

### 已经确认的事实

- `rewards_cache_ttl_sec` 默认 0（`config.py:38`），奖励复查没有客户端缓存造成的陈旧。
- `/rewards/markets/{cid}` 的响应项顶层带 `rewards_min_size`，和 `rewards_max_spread` 并列（2026-07-28 实测：单条 item，`rewards_min_size = 200`、`rewards_max_spread = 4.5`）。
- `monitor_status` 按 key 存快照、读时跨 key 合并，行内自带 `wallet` 字段（`engine/monitor_status.py:15-24`）。
- 那一分钟里有多少是 Polymarket 自己的传播延迟，客户端量不到也控制不了。如果阶段 1 之后周期已经降到十几秒而观测延迟仍是一分钟，说明大头在对面，后续阶段就不必做了。

## 不做的事

- 不改 `plan_exit` / `plan_take_profit` / `recheck_resting_buy` 的任何判定逻辑。本设计只动取数的并发结构和调用节奏。
- 不缩短 `fill_check_interval_sec`。瓶颈是 tick 时长，不是那 5 秒的等待。
- 不引入通用的请求调度器或缓存层。按需就地解决。

## 阶段 1：逐仓取数并发化（含缩小范围后的快照复用）

### 1a 预取

在 `check_exit` 的循环之前加预取阶段，照 Step 3 已经验证过的模式（`monitor.py:994` `_prefetch`）：

- **第一拨成交、第二拨盘口，两拨串行，顺序不可调。** 盘口驱动挂价和 B0 止损判定，必须是更新鲜的那份；成本只在我们自己成交时才变。这与 Step 3 里「奖励先取、盘口后取」是同构的理由。
- 成交那拨按 `condition_id` 去重：同一市场的 YES/NO 两个持仓共用一次 `get_trades(market=cid)`。
- 盘口那拨按 `asset_id` 去重。
- 并发度沿用 `_STEP3_MAX_WORKERS = 6`。

worker 线程**只发网络请求，绝不碰 `db`**。`models/database.py` 每线程开一条 sqlite 连接且从不回收，worker 里读一次 db 就是每轮泄漏一批连接。`extract_fills`、`position_cost_with_lots`、离场判定、下单、`_record_action`、状态行，全部留在主线程串行，顺序一字不动。

`_cost_lots` 和 `_sell_book` 改成优先读本 tick 的预取表、缺失才回退自取，这样 `_resolution_dump` 等其它调用点和现有测试都不受影响。`_sell_book` 顺带获得 per-tick 缓存：低余额清仓触发时，它和 `check_exit` 现在会各拉一遍同一个盘口。

失败语义逐字保留：

- 取成交失败 → `(None, [])` → 跳过并留 ⚠️裸奔状态行。
- 取盘口失败 → `(0.01, "0.01", None, None)`。
- 「取数失败」和「取到了但为空」在日志里仍要分得开。

### 1b 共用一份 open_orders 快照

tick 开头取一次 `get_open_orders`，步骤 1 到 4 共用。

**Step 3 保留自己的新鲜取数。** 它会重挂买单，吃陈旧快照就可能看到一张 Step 1 刚因成交撤掉的单，然后在刚成交过的市场里重新挂一张。冷却机制正是要拦这个。

步骤 1 到 4 吃 tick 开头的快照是安全的：`check_exit` 只看 SELL，而被 `check_low_balance` 清掉的仓已经被 `_just_dumped` 挡在循环外。

这一项收益不大（一轮省 1 到 3 个往返，不到 tick 时长的 5%），但顺手且风险可控。

### 1c 分段耗时日志

一条 INFO：各步耗时、持仓数、挂单数、本轮总时长。「周期真降下来了」只能靠它验证，后面两个阶段还要不要做也看它。

### 验收

- 现有测试全绿，离场/止损相关测试一条不改（判定逻辑没动）。
- 新增测试覆盖：预取表命中与回退、`condition_id` 去重、取数失败仍走跳过加裸奔告警、两拨的先后顺序。
- 日志里 `check_exit` 的耗时随持仓数增长明显放缓。

## 阶段 2：Step 3 独立节奏

先看阶段 1 之后的实测周期再决定要不要做。

每个钱包多一条 daemon 线程，只跑 `check_sell_orders`，周期走新增的引擎级配置 `compliance_interval_sec`（默认 10 秒）。

状态行的冲突有干净解法：快车道用 `f"{wallet}#step3"` 当 `set_snapshot` 的 key。`get_snapshot` 跨 key 合并、行内自带 `wallet` 字段，所以读侧和前端零改动。

其余：`_rewards_cache` 跨线程共用没问题（值是不可变元组）；两条线程可能撤到同一张单，撤已撤单返回 already-canceled，现有代码是 WARNING 加 return，不会刷屏。

### 验收

- 撤单延迟上限与持仓数脱钩。
- 状态页同时显示两条车道的行，不互相覆盖。

## 阶段 3：补 `rewards_min_size` 复查

现在 Step 3 只复查买卖价差和每日奖励额，**从不复查 `rewards_min_size`**。这个字段只在发现阶段（默认 4 小时一轮）和下单时用池快照 `tier_for` 判。

后果：如果 Polymarket 只改份额要求、每日奖励仍在门槛之上，那张买单会在不匹配的档位上最长挂 4 小时。用户观察到的一分钟撤单，大概率是奖励额那道闸门触发的，因为改奖励参数时 rate 和 min_size 通常一起动。

修法：`_market_rewards` 从已经取回的同一份响应里多解析一项 `rewards_min_size`，Step 3 加一道「档位不再匹配就撤」，cancel-only、不重挂。**零新增请求。**

判据与既有的档位匹配保持一致：`tier_for(size_tiers, min_size)` 返回 None 就撤。取不到该字段时跳过本轮，不撤（与奖励额那道闸门的 `None` 语义一致，绝不 fail-close）。

### 验收

- 新增测试：档位不再匹配则撤且不重挂；字段取不到则不动；档位仍匹配则走原有价格合规路径。
- 撤单动作在 `actions` 表里有独立的 `action_type`，理由写清新旧份额要求。

## 顺序

阶段 1 → 实测周期 → 阶段 2（视数据决定）→ 阶段 3。

阶段 3 与前两个阶段互不依赖，可以随时插队。

## 不变量

- 「卖单永不低于成本」不受影响：本设计不碰 `plan_exit` 和 `_exit_position` 的钳位。
- 成本只认 `get_trades` 逐笔重建，绝不用 Data API `avgPrice`；重建不出就跳过并告警。
- 预取 worker 绝不碰 `db`。
- 奖励复查的 `0.0` 与 `None` 语义不可合并成一个 falsy 判断。
- Gamma 结算状态取数失败一律 fail-open。
