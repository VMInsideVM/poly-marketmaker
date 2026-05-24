# 止盈/止损成本价兜底：补抓 taker 成交 + get_trades 取不到时回落 avgPrice — 设计

日期：2026-05-24

承接：`docs/superpowers/specs/2026-05-23-take-profit-cost-basis-from-trades-design.md`（成本价改用 get_trades 加权 + 穿价护栏）。本设计修补那套方案的一个覆盖盲区。

## 背景

现行止盈/止损的成本价来自 CLOB `get_trades` 的真实买入成交加权（`engine/take_profit.py` `cost_basis_from_buy_fills`，经 `engine/fills.py` `extract_buy_fills` 提取）。容错哲学是"不确定就不动手"：取不到成交 → `cost=None` → 止盈、止损**都跳过该持仓**（`engine/monitor.py:240-251` 止盈、`monitor.py:350-351` 止损）。

问题：**当 `get_trades` 取不到某持仓的买入成交时，这个 `size>0` 的仓位永远拿不到止盈卖单，也得不到止损保护——完全裸奔。** Data API `/positions` 按链上余额聚合，能看到这个仓位，却没有任何逻辑给它挂卖单。

复盘 `get_trades` 取不到买入成交的几类成因：

| 失败模式 | 能否靠 get_trades 优化解决 |
|---|---|
| **buy 以 taker 成交**（盘口在"算价→下单落地"间移动） | ✅ 能修——补抓 taker 成交 |
| `get_trades` 偶发网络异常 | ⚠️ 轻微，下个 tick 自愈 |
| 分页截断 | ✅ 不存在（`get_trades` 已循环到 `END_CURSOR`，自动翻页到底，见 `py_clob_client_v2/client.py:587`） |
| **仓位是 app 之外建的**（手动建仓 / 别的程序） | ❌ 修不了——这钱包的成交史里本来就没有 |

两个根因，需要两层各自的修复：

1. **taker 成交不可见（app 自己的仓位）**：`place_limit_buy` 用 `OrderType.GTC`（`api/polymarket_api.py:163`）。GTC 只意味着"没成交就挂着"，**不保证是 maker**——下单瞬间限价 ≥ 卖一时，这笔 GTC 买单会立即以 **taker** 成交。但 `_cost` 的取数写死了 `maker_address=funder`（服务端只返回我们当 maker 的成交，`client.py:599`），且 `extract_buy_fills` 只遍历 `maker_orders` 里 `side==BUY` 的条目（`fills.py:60-66`，记的是对手方而非我们）。两道关卡都把 taker 成交挡在外面 → 取到 0 笔。

2. **外部仓位无成交史**：手动建仓或别的程序建的仓位，其买入成交根本不在本钱包的 `get_trades` 里。无论怎么优化 `get_trades` 都变不出来——这是硬边界，只能靠兜底数据源。

## 目标

- 任何 `size>0` 的持仓都拿到止盈卖单与止损保护，不再因取不到买入成交而裸奔。
- **第一层**：让成本计算把"我们当 taker 的买入成交"也算进去，覆盖 app 自己买出来的仓位。
- **第二层**：`get_trades` 真·取不到成交（0 笔）时，回落到 Data API `/positions` 的 `avgPrice` 作为成本兜底，覆盖外部仓位。
- 保持现有"主路径优先 get_trades 加权成本"不变；兜底严格门控，绝不在 get_trades 可用时覆盖它。

## 非目标（本次不做）

- 不改主路径"优先用 get_trades 加权成本"的既有行为；avgPrice 只作最后兜底。
- 不改穿价护栏 `take_profit_price = max(ceil_to_tick(cost), best_bid+tick)` 的算法。
- 不引入"刚建仓 N 秒冷静期"。
- 不改 `select_new_buy_fills`（Step1 成交检测/冷却/撤余单）的行为——taker 买入是瞬间全成交、无剩余可撤，冷却缺失影响轻微，属独立问题，本次不动。
- 不区分机器人挂的卖单与用户手动挂的卖单（沿用既有暂缓决定）。

## 架构方案

无状态、不改 DB，沿用既有"一切读实时 API、纯函数可单测"的架构。两层独立但同一目标：

```
每个监控 tick，对每个持仓（Data API /positions 提供 size 与 avgPrice）：

  第一层（主路径）
    CLOB get_trades(asset_id=asset)        ← 去掉 maker_address，拿两种角色的成交
        │
        ▼
    extract_buy_fills(trades, funder, asset)  ← maker BUY ∪ taker BUY
        │
        ▼
    cost_basis_from_buy_fills(fills, size) ──► cost (加权成本) 或 None

  第二层（兜底，仅当上面 cost is None）
    cost = pos["avgPrice"]  if avgPrice > 0  else None

  ── cost 仍为 None ──► 跳过该持仓（保持现状：不动卖单/不止损）
  ── cost 有值 ──►
      止盈：want = take_profit_price(cost, best_bid, tick) ──► 维持恰好一笔卖单
      止损：stop_loss_triggered(curPrice, cost, pct)        ──► 市价平仓
```

## 详细设计

### 第一层：`extract_buy_fills` 补抓 taker 成交

#### 0. 前置验证任务（写代码前必做）

taker 成交在 `/data/trades` 里的确切结构需要用真实数据确认，不靠猜（碰真钱）。用现成的手动脚本（`test_live.py` 那套）对一个真实钱包打一份原始 `get_trades` 返回，落盘成 JSON，确认：

1. **去掉 `maker_address` 后，`/data/trades`（带 L2 鉴权）是否只返回本钱包两种角色（maker+taker）的成交**，而不是返回全市场所有人的成交。这是整个第一层成立的前提——若它返回全市场，则方案需改为"额外按 taker 维度单独查一次"。
2. **taker 成交里哪个字段标识"我们是 taker"**（候选：顶层 `trader_side == "TAKER"` / `taker_order_id` / owner 字段）、顶层 `side`/`size`/`price`/`asset_id`/`match_time` 的实际键名。
3. 顺带核对 maker 成交结构与现有 `extract_buy_fills` 假设一致（`maker_orders[].maker_address/side/asset_id/price/matched_amount`）。

把确认到的字段名记进实现计划，作为 `extract_buy_fills` taker 分支的依据。

#### 1. `_cost` 取数去掉 `maker_address`（`engine/monitor.py`）

```python
# 现状
trades = self.api.get_trades(TradeParams(maker_address=funder, asset_id=asset_id))
# 改为
trades = self.api.get_trades(TradeParams(asset_id=asset_id))
```

去掉服务端 maker 过滤后返回更多 trade（含我们当 taker 的），maker 分支仍由 `extract_buy_fills` 内部按 `funder` 过滤 `maker_orders`，行为不变；只是多了 taker 分支可处理的数据。按 asset 过滤后单 token 成交量很小，多翻几页代价可忽略。

> 依赖前置验证任务结论 1：若去掉 `maker_address` 会返回全市场成交，则改为保留 maker 查询 + 额外查一次 taker（按验证到的 taker 过滤维度），在 `extract_buy_fills` 内合并去重。

#### 2. `extract_buy_fills` 增加 taker 分支（`engine/fills.py`，纯函数）

现有逻辑（maker 分支）保留不动。新增：遍历每个 trade，若该 trade 是**我们当 taker**（按验证到的字段判定）、顶层 `side == "BUY"`、顶层 `asset_id == asset`，则追加一条 `{price: 顶层 price, size: 顶层 size, ts: 顶层 match_time}`。

去重要点：一笔 trade 对我们而言要么是 maker、要么是 taker，不会两边都算 → 不会重复计入同一份额。`cost_basis_from_buy_fills` 仍按 ts 从新到旧累计取 size，taker 买入只是让这个池子更完整。

### 第二层：avgPrice 兜底（`engine/monitor.py`）

#### 1. 取成本带兜底

在 `_reconcile_take_profit` 与 `_check_pos_sl` 两个调用点，`pos` 字典里已有 `avgPrice`。`_cost` 仍只负责 get_trades 加权 + 本 tick 缓存（返回加权成本或 None），兜底在调用点显式发生：

```python
cost = self._cost(asset_id, size)        # get_trades 加权，或 None
source = "get_trades加权"
if cost is None:
    avg = float(pos.get("avgPrice", 0) or 0)
    if avg > 0:
        cost = avg
        source = "avgPrice兜底"
```

门控严格：仅当 get_trades 返回 0 笔成交（`cost is None`）且 `avgPrice > 0` 才启用。get_trades 有数据时 `cost` 非 None，永远不碰 avgPrice——避开当年"两源并存信错"的事故场景。

#### 2. 止盈接入（`_reconcile_take_profit`）

- `cost` 仍为 None（两源皆空）→ 记状态行"无成交数据·跳过"，不动任何卖单，return（现状不变）。
- `cost` 有值（含兜底）→ `want = take_profit_price(cost, best_bid, tick)`，穿价护栏照旧。最坏 avgPrice 抽风读低，卖单也永远挂在买一之上当 maker，**不会市价砸盘**。
- `record_action` 的 `price_basis` 标明来源，例：`成本=avgPrice兜底 {cost:.4f}（get_trades无成交，回落Data API）；卖价=max(成本,买一+1tick)={want:.4f}`。

#### 3. 止损接入（`_check_pos_sl`）

- 用同一带兜底的 `cost` 判定 `stop_loss_triggered(cur, cost, pct)` 及 pnl/记录里的成本。
- `cost` 仍为 None → 跳过该持仓止损（现状不变）。
- `curPrice` 仍用 Data API。

> **已知风险（已接受，用户决策）**：止损是 `place_market_sell` 市价平仓，**无穿价护栏**（市价平仓是设计意图）。当兜底用的 avgPrice 读高时，止损线 `avg×(1-pct)` 偏高，可能在不该止损时误触发市价砸出。已通过最紧门控（仅 get_trades 0 笔 + avgPrice>0）把踩中概率压到最低；外部仓位通常非"刚建仓"，avgPrice 多已结算稳定。`record_action`/状态行标明成本来源，便于事后复盘。

## 错误处理（容错哲学：不确定就不动手）

- `get_trades` 抛异常 → `_cost` 已 catch 返回 None（`monitor.py:107-110`）→ 进入兜底；若 `avgPrice` 也无 → 该 tick 跳过该持仓、保持现有卖单不动。
- 两源皆空（get_trades 0 笔 + avgPrice≤0）→ 止盈、止损都跳过该持仓，绝不在无成本数据上挂单或平仓。
- 双层防护：即便兜底成本偏低，止盈的穿价护栏仍保证不市价清仓。

## 测试计划（TDD，先红后绿）

- `tests/test_fills.py`：`extract_buy_fills`
  - 新增 taker 买入用例：我们当 taker、顶层 side==BUY、asset 匹配 → 计入（用顶层 price/size/match_time）。
  - taker 但 side==SELL / asset 不匹配 / 是别人的 taker → 忽略。
  - maker + taker 混合 → 两者都计入、同一 trade 不重复。
  - 既有纯 maker 用例保持绿。
- `tests/test_monitor.py`：
  - 止盈：get_trades 空 + `avgPrice>0` → 按 avgPrice 经穿价护栏挂卖；状态/`price_basis` 标"avgPrice兜底"。
  - 止损：get_trades 空 + `avgPrice>0` → 用 avgPrice 判定 `stop_loss_triggered`。
  - 两源皆空（get_trades 空 + `avgPrice<=0`）→ 止盈不动卖单、止损不平仓（现状）。
  - get_trades 有成交 → 仍用加权成本，**不**回落 avgPrice（门控验证）。

## 守住的既有关键行为（不改）

- 每个持仓**恰好一笔**止盈卖单。
- 止盈卖价穿价护栏 `max(ceil_to_tick(cost), best_bid+tick)`。
- 止盈跑在 Step1（成交检测）之后；Step3 仍跳过 `side!="BUY"` 的卖单与部分成交单。
- 停引擎仍只撤买单（不动止损线程外的持仓）。
- 监控水位线、黑名单等其余逻辑不变。
- 主路径永远优先 get_trades 加权成本，avgPrice 仅最后兜底。
