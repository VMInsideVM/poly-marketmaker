# 在挂单市场的每日奖励实时复查

日期：2026-07-26

## 问题

市场的每日奖励金额会中途下调，有时是断崖式的。程序目前发现不了这件事。

监控 Step3（`engine/monitor.py:956` `_check_compliance`）每 5 秒对每个在挂买单跑一遍，但只复查盘口价差、单价区间和奖励价差区间，奖励金额不在其中。奖励金额只在发现阶段重新联网取（`_discover` → `fetch_candidates`），间隔 `discovery_interval_sec` 默认 14400 秒，也就是 4 小时。中间每 30 秒的下单轮虽然会逐项判 `min_reward_usd`，但读的是候选池里最多 4 小时前的快照值。

结果就是某个市场奖励从 300 掉到 5，程序最长 4 小时都在拿资金给它白挂单。

## 目标

在挂单市场的每日奖励跟着现有的每 5 秒挂单检查一起实时复查，低于该钱包模板的 `min_reward_usd` 立即撤单，并且撤掉之后不会被下一个下单轮又挂回去。

不做的事：不改发现阶段的节奏（`discovery_interval_sec` 保持 4 小时）；不改扫描和下单的其它筛选门槛；不给未挂单的市场加实时复查（那是发现阶段的事）；不引入新的配置项。

## 前提：口径同源

发现阶段算 `market_reward` 用的是 `/rewards/markets/{condition_id}` 加上这个求和公式（`engine/scanner.py:260-273` `_precise_reward`）：

```python
sum(rc.get("rate_per_day", 0) for rd in raw for rc in rd.get("rewards_config", []))
```

监控 Step3 现在也已经在调同一个接口（`_market_max_spread` → `get_rewards_for_market`），只是只解析了 `rewards_max_spread`，把 `rewards_config` 丢了。

实时复查用同一个接口、同一个公式，算出来的值与候选池里的值同源同口径，可以直接写回，不会出现两套标准。也因此这个功能不需要新的接口调用形态，只是不再缓存、多解析一个字段。

**求和不看时间段这件事已实测过**（2026-07-26 线上普查，报告：`.superpowers/sdd/2026-07-26-realtime-reward-recheck/probe-reward-config-periods.md`）。评审提过一种失效可能：`rewards_config` 的每项都带 `start_date`/`end_date`，如果 Polymarket 是「把旧项截止 + 追加一条新项」来降奖励，那么不分时间段地求和就会把过期项的额度也算进去，降奖励永远检测不出来。实测结论是这种情形在线上不存在：

- 全量 500 个在跑奖励的市场，**每个市场的 `rewards_config` 都恰好只有 1 项**（0 个例外），该项的 `end_date` 一律是 `2500-12-31` 这种远期哨兵值，即「无截止」。
- `/rewards/markets/{condition_id}` 返回的是**每个市场一项**，不是每个 token 一项（91 个市场抽查，无例外）。`api/polymarket_api.py` 原来的注释写的是 per-token，已改正。
- 也就是说 Polymarket 是**原地改**那一条奖励配置，不是追加历史。所以不看时间段的求和与「只算当前生效项」的求和在实测中恒等，降奖励能被检测到。

局限：这是某一时点的普查，没有守到一次真实的降奖励事件发生。

## 方案

### 1. 纯函数：解析每日奖励

`engine/rewards.py` 新增 `extract_daily_rate`，与现有 `extract_max_spread` 并列：

```python
def extract_daily_rate(rewards_items: list) -> Optional[float]:
    """Sum rate_per_day across all reward configs of /rewards/markets/{cid}.

    Returns the market's total daily reward in USD, or None when the payload
    carries no parsable rewards_config at all. 0.0 and None are different:
    0.0 means the reward really is zero (cancel), None means we could not tell
    (skip safely). Callers must not collapse them into one falsy check.
    """
```

实现要点：遍历 `rewards_items` 的每一项、每项的 `rewards_config`，累加可解析的 `rate_per_day`。一个都没解析到就返回 `None`；解析到了就返回累加值，可能是 `0.0`。

### 2. 监控：一次取数，两个用途

`OrderMonitor._market_max_spread` 改为 `_market_rewards(condition_id) -> tuple[Optional[float], Optional[float]]`，返回 `(max_spread_cents, daily_rate_usd)`，同一次 HTTP 响应解析出两个值。TTL 缓存的代码结构保留（缓存值变成两元组），只是默认 TTL 变成 0。

`_check_compliance` 的判定顺序只插入一处，其余判定的相对顺序全部不动：

```
黑名单撤单
  → get_orderbook（bids/asks/买一/卖一/价差）
  → recheck_resting_buy（盘口价差）
  → _market_rewards（一次取数）
  → 【新增】daily_rate 低于 min_reward_usd → 撤单 + 写回 + return
  → 空盘口跳过
  → max_spread 取不到 → 本轮跳过
  → 单价区间护栏
  → 奖励价差区间判定
```

新分支排在「空盘口跳过」和「max_spread 取不到就跳过」两道**之前**，两个理由：

- 奖励判定根本用不到盘口。薄奖励市场经常缺卖单一侧，而没有卖单的在挂买单照样会被别人的市价卖单吃掉。闸门要是排在空盘口跳过之后，这类市场奖励归零时买单会一直挂着，还会连带不写回候选池、下单轮继续拿旧快照重挂——这个功能要堵的 4 小时窗口对这批市场原封不动地开着。
- `max_spread` 与奖励来自同一次响应，万一响应里 `rewards_max_spread` 缺失而 `rewards_config` 正常，奖励判定不该被一起跳过。

`midpoint` 必须留在空盘口守卫之后（它要解 `best_bid`/`best_ask`）。

新增分支的行为：

```python
min_reward = float(settings.get("min_reward_usd", 0))
if daily_rate is not None and daily_rate < min_reward:
    # 撤单（撤不掉就 WARNING + return，5 秒后下一 tick 自动重试）
    # record_action: reward_drop_cancel
    # status_add:    Step3 / 撤单(奖励下降)
    # 写回候选池（见下）
    return
```

`daily_rate is None`，即接口失败或响应里没有可解析的奖励配置，什么都不做，继续走原有流程。门槛取不到时兜底为 0，即不撤任何单（`get_template_for` 会合并 `TEMPLATE_DEFAULTS`，这个键实际恒有值，兜底只是防御）。

记录的动作理由要写明数值：`实时每日奖励 $5.00 < 门槛 $100.00，撤买单不重挂`。`price_basis` 注明来源是 `CLOB /rewards/markets/{cid}` 实时取数。

### 3. 写回候选池

撤单之后必须让下单轮也知道这件事，否则 30 秒后它会拿 4 小时前的旧快照把单挂回来，5 秒后监控再撤，来回打架。

连接方式：`OrderMonitor.__init__` 增加可选参数 `on_reward_update=None`，`WalletWorker.__init__` 增加同名可选参数并传给 monitor。`EngineManager.start_wallet`（`engine/manager.py:842`）构造 worker 时把 `self.update_market_reward` 传进去。`engine/manager.py:797` 那个临时下单 worker 不跑监控线程，不传，保持默认 `None`。这样不产生 monitor 到 manager 的反向引用，测试里也可以完全不传。

manager 侧新方法：

```python
def update_market_reward(self, condition_id: str, reward: float):
    """实时复查到的每日奖励写回候选池(内存+DB),下单轮 prefilter 立刻用新值。"""
    pool = self.eligible_markets      # 先取本地引用:扫描会整体重绑该属性
    for m in pool:
        if m.get("condition_id") == condition_id:
            m["market_reward"] = reward
            m["daily_reward"] = reward
    self.db.update_eligible_reward(condition_id, reward)
```

只写 `market_reward` 就足以让市场跌出 eligible。`prefilter_for_template`（`engine/scanner.py:469`）判的是 `if total_rate < min_reward or market_reward < min_reward`，或条件，任一不达标即踢。`daily_reward` 是展示键，一起更新保持一致。

DB 侧新方法：

```python
def update_eligible_reward(self, condition_id: str, reward: float):
    """UPDATE eligible_markets SET daily_reward = ? WHERE market_id = ?"""
```

它同时修好两处既有的不一致：`/api/eligible` 页面上显示的旧奖励值，以及低余额清仓 tier1 判「低奖励市场」所依据的 `db.get_market_daily_reward`。

双保险：市场跌出 eligible 后，下一个下单轮的 `cancel_dropouts`（`engine/manager.py:236`）会撤掉该市场剩余的全部买单。Step3 撤得快（5 秒），写回让它不再回来，dropout 兜底扫尾。

写回只改内存池和 DB 表，4 小时后 `_discover` 整体重建池，奖励若恢复则市场自然回到 eligible。代价是奖励降了又很快升回来的情况下，最长要等 4 小时才会重新挂单。这一点比「撤单时设冷却」的方案差，用户已确认接受。

### 4. 配置

不新增配置项，门槛复用模板的 `min_reward_usd`，默认 100。

`config.py` 的 `ENGINE_DEFAULTS["rewards_cache_ttl_sec"]` 由 `600` 改为 `0`。0 表示每次实时取，现有代码 `(now - fetched_at) < ttl` 在 ttl=0 时恒为 False，天然支持。保留这个配置项而不是删掉，是为了代理吃紧时能一键调回缓存；删它要动 `web/templates/config.html`、`README.md`、`docs/系统逻辑与参数说明.md` 三处文案。

这三处文案要同步改成「奖励参数复查缓存 TTL（秒），0=每次实时取」。

## 错误处理与不变量

绝不 fail-close。取奖励失败、响应为空、解析不出奖励配置，一律本轮跳过，不撤不重挂，记 WARNING。接口抖一下就把正在赚奖励的单撤光是最坏结果。

为此 `get_rewards_for_market` 分页中途出错必须返回 `[]`，**不能返回已取到的半份数据**（它原来吞掉异常后 `return all_data`）：半份数据求出来的奖励额偏小，是个「看着合理但是错的」数字，足以让正在赚奖励的单被判成跌破门槛而撤掉——正是这条不变量禁止的 fail-close。返回空列表则调用方看到的是「取不到」→ `(None, None)` → 跳过。`engine/scanner.py` 那个调用方同样受益：它判的是 `if raw:`，空列表会退回批量值。

0 与 None 必须分开。`0.0` 是奖励真的归零，要撤单；`None` 是取不到，要跳过。禁止用 `if not daily_rate` 这类假值判断把两者合并。

撤单失败不写回。`cancel_orders` 抛错时 WARNING + return，与现有黑名单、价差分支的处理一致；因为不再缓存，5 秒后下一 tick 会重新判定重试。

写回不许拖累交易。回调整体包 try/except，只记 WARNING。与 `wallets.last_active_at` 同规矩：显示和记账用的写入绝不能中断撤单流程。

持仓不受影响。这条路径只撤买单，持仓照旧由 `check_exit` 管，与 `dropout_cancel` 的既有语义一致。

## 请求量

Step3 每个在挂买单每 tick 从 1 个请求（orderbook）变成 2 个（orderbook + rewards）。按默认 `max_concurrent_markets=10`、`fill_check_interval_sec=5` 估算，单钱包约 120 涨到 240 请求/分钟，全部走该钱包自己的代理。

奖励请求是**按市场**而不是按单去重的：`check_sell_orders` 开头重置一个 `condition_id → (max_spread, daily_rate)` 的本轮备忘（`_round_market_rewards`），同一市场 YES/NO 两侧都有在挂买单时一轮只发一次请求。TTL 默认已是 0（每次实时取），挡不住这种同轮重复。失败结果 `(None, None)` 也进备忘，保证同一轮里两侧得到同一个判定（否则可能一侧撤、一侧不撤）；下个 tick 只隔 5 秒，重试不吃亏。新鲜度不变：每市场每 tick 仍是一次实时取数。

备忘放在遍历这一层、不放进 `_market_rewards`：后者的职责是「按 TTL 取数」，一次遍历内的去重是遍历自己的事；放进去还会把 `TestMarketRewards` 那几个直接连调两次、断言请求次数的缓存用例悄悄弄成永真。

这件事对止损延迟有意义：Step3 排在 `_tick` 最后，每笔单 2 个串行 HTTP（各 10 秒超时），10 笔单的最坏耗时翻倍会推迟**下一** tick 的成交检测和止损。这个项目有代理间歇性抽风拖垮整轮的前史，能省的请求就省。

## 测试

`tests/test_rewards.py` 增 `extract_daily_rate` 用例：多档 `rewards_config` 求和、多个 data 项累加、`rate_per_day` 为 0 时返回 `0.0`、空列表返回 `None`、字段缺失或不可解析返回 `None`。

`tests/test_monitor.py` 增 `TestStep3RewardDrop`，照 `TestStep3PriceBand`（`tests/test_monitor.py:872`）的写法：奖励低于门槛则撤单且不重挂、奖励等于门槛不撤（门槛是低于才撤）、奖励达标走原有流程、取数失败不撤、撤单失败不写回、写回回调抛错不影响撤单流程。

`tests/test_manager.py` 增 `update_market_reward` 用例：内存池条目被改写、DB 方法被调用、改写后该市场从 `prefilter_for_template` 的结果里消失。

跑全量 `pytest` 确认现有 735 个测试不回归，重点看 `tests/test_settings_routes.py` 是否断言了 `rewards_cache_ttl_sec` 的具体默认值。

## 兼容性与发版

在挂单市场的奖励一旦跌破门槛会被立即撤单，且 4 小时内不会重挂。这是行为改变，发版公告要写明。

`rewards_cache_ttl_sec` 默认值变更只对没在配置页动过该键的用户生效。动过的用户 settings 表里存着自己的值，需要手动改成 0 才能启用实时复查，公告里要单独说这一句。

按 `docs/版本号规范.md`，行为改变走主版本号。
