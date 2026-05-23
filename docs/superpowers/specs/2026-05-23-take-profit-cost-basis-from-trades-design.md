# 止盈/止损成本价改用 get_trades 加权成本 + 穿价护栏 — 设计

日期：2026-05-23

## 背景

线上一次真实事故（链上已确认）：

- 钱包 `0x98d6…ae75` 在 "Will the U.S. invade Iran before 2027?" 市场，BUY 361 份 @ **0.28**；
- **14 秒后** SELL 361 份 @ **0.27**，整仓在买入后立刻被市价吃掉。

链上完整交易史只有这两笔，卖出数量 = 买入数量，说明这就是单独一笔 0.28 买出来的仓位、随即被全部清掉。复盘根因：

1. **触发（间歇性、外部）**：止盈用的成本价来自 Polymarket Data API `/positions` 的 `avgPrice`，程序原样取用（`monitor.py:203`）。仓位刚建立的几秒内，Data API 持仓聚合服务尚未结算好成本，返回了一个错值（约 0.21）。监控每 5 秒一个 tick，成交后**下一个 tick 就去查 `/positions`**，正好踩在最不稳定的时刻。
2. **缺失防线 A（本可避免）**：同一 tick 上一步 `check_buy_orders` 已通过 CLOB `get_trades` 拿到这笔成交的真实价 0.28（`fills.py:38`），但止盈逻辑丢弃了它，转而信 Data API 的 avgPrice。两个数据源谁都没拿来互相校验。
3. **缺失防线 B（放大器，本次直接元凶）**：止盈"按成本价挂卖单"，`take_profit.py:58` 只有 `ceil_to_tick(avg)`，**不检查卖价是否已低于当前买一**。当成本(0.21) < 买一(0.27)，这个"按成本挂的卖单"变成可立即成交的限价单，扫穿买盘 → 整仓市价清出。即便成本价是真实的 0.28，只要买一涨过 0.28，同样会被砸出去。

止损（`_check_pos_sl`）同样读 Data API `avgPrice` 判断止损线（默认 `avg×(1-15%)`），同一脏读也会污染它。

> 注：`take_profit.py` 当初**正是**从"按每笔 get_trades 成交价"改成"按 Data API avgPrice"的（注释记录："曾观测到 200 股挂在 0.38、真实 avgPrice 0.30"）。所以两个源都曾各自不可靠 —— 本设计的关键不是简单换源，而是用**真实成交的加权成本**消灭单笔价的不可靠，并叠加穿价护栏作为第二层防护。

## 目标

- 止盈、止损的成本价改为从 CLOB `get_trades` 的真实买入成交计算的**加权成本**，不再依赖 Data API 的 `avgPrice`。
- 止盈卖价加**穿价护栏**：永远是挂得住的 maker 单，绝不穿价市价清仓。
- 持仓**数量**仍信 Data API `/positions` 的 `size`（事故中它是准的）。

## 非目标（本次不做）

- 不区分"机器人挂的卖单"与"用户手动挂的卖单"，也不做市场/仓位的整体豁免（此前已讨论并由用户暂缓）。
- 不改"持仓/已挂卖单不拦截新买单"这一既有行为（独立问题，另议）。
- 不改止损里 `curPrice` 的来源（仍用 Data API，它不是本次元凶）。
- 不引入"刚建仓 N 秒冷静期"（用 get_trades 算成本已天然规避时序脏读，冷静期为多余复杂度）。

## 架构方案（已选定：无状态重算）

无状态、不改 DB：每个 tick 对每个持仓按 `asset_id` 查一次 `get_trades`，现算成本。契合本项目"不以本地表为准、一切读实时 API"的既有架构（CLAUDE.md），逻辑纯粹可单测，且天然规避时序脏读（算成本用的 get_trades 就是检测成交的同一个源）。

### 数据流

```
每个监控 tick：
  Data API /positions ──► 每个持仓的 size（信它）
  CLOB get_trades(maker_address=funder, asset_id) ──► 我们在该 token 的 BUY 成交
        │
        ▼
  cost_basis_from_buy_fills(buy_fills, size)  ── 纯函数，加权成本
        │
        ├─► 止盈：want = take_profit_price(cost, best_bid, tick) ──► 整仓挂一笔卖单
        └─► 止损：stop_loss_triggered(curPrice, cost, pct)        ──► 市价平仓
```

数量用 Data API 的 `size`、价格用 get_trades 的真实买入价 —— 既绕开 avgPrice 脏读，又**不必处理"净掉卖出"的 taker/maker 账务**（我们的买单全是 maker，可靠出现在 `maker_orders`；卖出（尤其穿价的 taker 卖）在按 maker 过滤的 get_trades 里不一定可见，故不依赖它）。

## 详细设计

### 1. 纯函数（无 IO，全单测）

**`engine/fills.py` 新增**（与 `select_new_buy_fills` 并列，但不做 dedup）：

```python
def extract_buy_fills(trades: list[dict], funder: str, asset_id: str) -> list[dict]:
    """挑出我们在某 token 上的全部 BUY 成交。返回 [{price, size, ts}, ...]。
    遍历每个 trade 的 maker_orders，取 maker_address==funder 且 side==BUY
    且 asset_id 匹配的条目；price=mo.price，size=mo.matched_amount，
    ts=trade.match_time。不去重、不依赖 seen_keys。"""
```

**`engine/take_profit.py` 新增**：

```python
def cost_basis_from_buy_fills(buy_fills: list[dict], size: float) -> float | None:
    """从买入成交算当前持仓的加权成本。
    按 ts 从新到旧排序，累计取份额直至覆盖 size；返回这些份额的加权均价。
    - size <= 0 或 buy_fills 为空 → None
    - 买入总量不足 size（部分份额非 maker 买入/历史截断）→ 按已有份额加权（优雅降级）
    单笔买入全持有时精确等于买入价；多笔买入时为真实加权（不退化成单笔价）。"""

def take_profit_price(cost: float, best_bid: float | None, tick: float) -> float:
    """穿价护栏：返回 max(ceil_to_tick(cost, tick), best_bid + tick)。
    best_bid 为 None（盘口某侧缺失）→ 退回 ceil_to_tick(cost, tick)。
    保证卖价严格高于买一 → 永远是挂得住的 maker 单，绝不穿价。"""
```

`ceil_to_tick` 复用现有实现。

**`plan_take_profit` 签名调整**：`(size, avg, tick, existing_sells)` → `(size, want_price, tick, existing_sells)`。把"算价"移到上面的纯函数，`plan_take_profit` 只负责"保持/撤换、维持恰好一笔卖单"的对账（其余语义不变）。

### 2. 止盈接入（`monitor.py` `_reconcile_take_profit`）

1. `avg = pos.avgPrice` → `cost = self._cost(asset_id, size)`（见 §4）。
2. `cost is None` → 记状态行"无成交数据·跳过"，**不动任何卖单**，return。
3. 盘口同时取 `tick` 与 `best_bid`（`_sell_tick` 扩成 `_sell_book` 返回 `(tick, tick_str, best_bid)`）。
4. `want = take_profit_price(cost, best_bid, tick)`。
5. `plan = plan_take_profit(size, want, tick, sells)`；keep/replace 逻辑不变（仍维持恰好一笔）。
6. `record_action` 的 `price_basis` 改述新来源，例：`成本=get_trades 加权 {cost:.4f}；卖价=max(成本, 买一+1tick)={want:.4f}；来源：CLOB get_trades + get_orderbook`。

行为：成本 ≥ 买一时挂成本价（保本回收）；浮盈时卖单自动上移到买一上方一档（挂得住、被吃也赚、继续吃奖励）。事故的市价清仓不再发生。

### 3. 止损接入（`monitor.py` `_check_pos_sl`）

1. `avg = pos.avgPrice` → `cost = self._cost(asset_id, size)`（与止盈共用同 tick 缓存值）。
2. `cost is None` → 跳过该持仓止损判定（不在不确定成本上市价平仓）。
3. `stop_loss_triggered(cur, cost, stop_loss_pct)` 及 pnl/记录里的成本全部用 `cost`。
4. 止损不加穿价护栏（市价平仓为设计意图，保留 `place_market_sell`）。
5. `curPrice` 仍用 Data API。

### 4. 取数与缓存（`monitor.py`）

- 新增 `self._cost(asset_id, size)`：先查**本 tick 缓存**（按 asset_id），未命中则
  `get_trades(TradeParams(maker_address=funder, asset_id=asset_id))`
  → `extract_buy_fills(trades, funder, asset_id)`
  → `cost_basis_from_buy_fills(fills, size)`，写缓存返回。
- 缓存（如 `self._cost_cache: dict`）在 `begin_status_tick()` 清空：每 tick 重算一次，止盈/止损共用，避免重复取数。
- 按 `asset_id` 过滤 → 单 token 成交历史很小，自动翻页代价可忽略；同时持仓数量也少。

## 错误处理（容错哲学：不确定就不动手）

- `get_trades` 抛异常/返回空 → `cost=None` → 止盈、止损该 tick 都**跳过该持仓、保持现有卖单不动**，绝不在错误数据上挂单或平仓。
- 双层防护：即便成本万一仍偏低，§1 的穿价护栏也保证不会市价清仓。
- 盘口空（`best_bid` 为 None）→ `take_profit_price` 退回 `ceil_to_tick(cost)`；盘口空时本就无买盘可穿，安全。

## 测试计划（TDD，先红后绿）

- `tests/test_fills.py`：`extract_buy_fills` —— 只挑本 funder、只挑 BUY、只挑该 asset；忽略 SELL/他人/别的 token；多笔聚合。
- `tests/test_take_profit.py`：
  - `cost_basis_from_buy_fills`：单笔=买入价；多笔=加权；最近买入凑 size；买入不足 size；空→None；size≤0→None。
  - `take_profit_price`：成本>买一→成本；成本<买一→买一+tick；无买一→成本；tick 对齐。
  - 更新现有 `plan_take_profit` 用例（改传 want_price）。
- `tests/test_monitor.py`：
  - 止盈浮盈时挂 `max(cost, 买一+1tick)`、而非 Data API avgPrice；`cost=None` 时不动卖单。
  - 止损用 `cost` 触发；`cost=None` 时不平仓。
  - mock `api.get_trades` 喂买入成交；更新原断言"按 avgPrice 挂卖"的用例。

## 守住的既有关键行为（不改）

- 每个持仓**恰好一笔**止盈卖单。
- 止盈跑在 Step1（成交检测）之后。
- Step3 仍跳过 `side!="BUY"` 的卖单与部分成交单。
- 停引擎仍只撤买单。
- 监控水位线、黑名单等其余逻辑不变。
