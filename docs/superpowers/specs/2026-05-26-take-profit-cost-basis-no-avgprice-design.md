# 止盈/止损成本价彻底弃用 avgPrice：严格 get_trades 逐笔重建并写入理由 — 设计

日期：2026-05-26

承接并**部分回滚**：`docs/superpowers/specs/2026-05-24-take-profit-cost-fallback-design.md`。该设计分两层：第一层"补抓 taker 成交"（get_trades 增强，正确，保留），第二层"get_trades 取不到时回落 Data API avgPrice"（本设计撤销）。

## 背景

止盈/止损的卖价公式 `卖价 = max(ceil_to_tick(成本), 买一 + 1 tick)`（穿价护栏）保持不变。问题只在**成本从哪来**。

2026-05-24 引入了 avgPrice 兜底：`get_trades` 取不到买入成交（0 笔）且 `avgPrice > 0` 时，用 Data API 的 `avgPrice` 当成本。`engine/monitor.py` 的 `_cost_with_source` 实现了这个回落，止盈（`_reconcile_take_profit`）与止损（`_check_pos_sl`）两处都用它。

实践判定：**Data API 的 `avgPrice` 是一个经常出错、甚至抽风的数据源**（这也是 2026-05-23 最初弃用它的原因——刚建仓的 0.28 买入曾被读成 ~0.21，差点市价砸盘）。把它留作兜底，等于在最不确定的场景（取不到成交）下，反而信任一个已知不可靠的源。即便止盈有穿价护栏，止损是市价平仓、无护栏，avgPrice 读高会误触发砸盘。

结论：**任何时候都不再用 `avgPrice`（`get_average_price`）作为成本来源。** 成本必须严格从 `get_trades` 的真实买入成交逐笔重建。取不到成交时——既然无法确认成本——就不动手（不挂止盈、不止损），但要醒目告警，让用户知道该持仓在裸奔。

附带需求：把"即将挂出的卖单"的成本构成讲清楚——成本是从哪几笔之前的买入交易记录算出来的——写进卖单理由（`price_basis`），便于事后复盘。

## 目标

- 止盈与止损的成本**只来自** `get_trades` 真实买入成交的加权（maker 成交 ∪ taker 成交，沿用 2026-05-24 第一层）。
- 彻底移除 avgPrice 兜底：`pos["avgPrice"]` 不再参与任何成本计算。
- `get_trades` 取不到买入成交 → 止盈、止损都跳过该持仓，并在实时监控状态表里**醒目告警**（⚠️ + 文案说明该持仓无成交记录、未受保护），同时 `log.warning`。
- 卖单理由（`price_basis`）列出参与加权的每一笔买入成交：**时间 + 价格×份额 + trade_id**，让成本构成可追溯到具体成交记录。

## 非目标（本次不做）

- 不改卖价公式 `max(ceil_to_tick(成本), 买一+1tick)` 与穿价护栏。
- 不改 `cost_basis_from_buy_fills` 的取数算法（按 ts 从新到旧累计取份额直至覆盖 size）——用户明确"现在的公式没问题，继续用"。
- 不撤销 2026-05-24 第一层"补抓 taker 成交"（`extract_buy_fills` taker 分支、`_cost` 查询去掉 `maker_address`）——那是 get_trades 增强，正确。
- 不改 Step1 成交检测/冷却/撤余单（`select_new_buy_fills`），不改 Step3 调单合规。
- 不引入持久化告警（不往 `actions` 表每 tick 写裸奔记录，避免刷屏）——告警走实时监控状态表 + 日志。

## 架构方案

无状态、不改 DB，沿用既有"一切读实时 API、纯函数可单测"的架构。

```
每个监控 tick，对每个 size>0 的持仓（Data API /positions 提供 size、止损的 curPrice）：

  CLOB get_trades(asset_id=asset)            ← 不传 maker_address（保留 taker 增强）
      │
      ▼
  extract_buy_fills(trades, funder, asset)   ← maker BUY ∪ taker BUY，每条带 trade_id
      │
      ▼
  cost_basis_with_lots(fills, size) ──► (cost, lots)
      │                                  lots = 被消耗的每笔买入 {price, take, ts, trade_id}
      │
      ├── cost is None ──► 跳过该持仓（不挂止盈/不止损）+ ⚠️醒目状态行 + log.warning
      │                    （绝不回落 avgPrice）
      │
      └── cost 有值 ──►
            止盈：want = take_profit_price(cost, best_bid, tick)；维持恰好一笔卖单
                  price_basis = describe_cost_basis(cost, lots) + 卖价说明
            止损：stop_loss_triggered(curPrice, cost, pct) → 市价平仓
                  price_basis = describe_cost_basis(cost, lots) + 现价说明
```

## 详细设计

### 1. `engine/fills.py`：`extract_buy_fills` 增带 `trade_id`

每条 fill 字典从 `{price, size, ts}` 扩为 `{price, size, ts, trade_id}`。maker 分支与 taker 分支都取 `trade_id = tr.get("id")`。纯增字段，不影响既有按 price/size/ts 的消费。

### 2. `engine/take_profit.py`：新增两个纯函数

**`cost_basis_with_lots(buy_fills, size) -> tuple[float | None, list[dict]]`**

复用 `cost_basis_from_buy_fills` 既有算法（按 ts 从新到旧累计取份额直至覆盖 size），**算法零改动**，额外把每笔被消耗的份额收集进 `lots`：

```python
def cost_basis_with_lots(buy_fills, size):
    if size <= 0 or not buy_fills:
        return None, []
    fills = sorted(buy_fills, key=lambda f: f.get("ts", 0) or 0, reverse=True)
    remaining = size
    cost_sum = 0.0
    qty_sum = 0.0
    lots = []
    for f in fills:
        if remaining <= 0:
            break
        fsize = float(f.get("size", 0) or 0)
        if fsize <= 0:
            continue
        take = min(fsize, remaining)
        price = float(f.get("price", 0) or 0)
        cost_sum += price * take
        qty_sum += take
        remaining -= take
        lots.append({
            "price": price,
            "take": take,
            "ts": float(f.get("ts", 0) or 0),
            "trade_id": f.get("trade_id", ""),
        })
    if qty_sum <= 0:
        return None, []
    return cost_sum / qty_sum, lots
```

`cost_basis_from_buy_fills` 改为薄包装：`return cost_basis_with_lots(buy_fills, size)[0]`。既有签名、行为、单测不变。

**`describe_cost_basis(cost, lots, max_lots=6) -> str`**

生成成本构成片段（纯函数）。`lots` 按 ts 正序（最早→最新）展示，便于读"仓位是怎么一步步建起来的"。超过 `max_lots` 笔时列前 `max_lots` 笔 + "…等共 M 笔"。trade_id 缩写为 `0xab..3f`（首 6 + 末 4；够短则原样）。时间用本地时区 `time.localtime` 格式化为 `MM-DD HH:MM`。

示例输出：
```
成本=0.2800（加权自2笔买入成交：①05-23 14:30 0.2700×200股 [trade 0xab..3f] ②05-24 09:10 0.2900×161股 [trade 0x9c..d1] 共取361股）
```

### 3. `engine/monitor.py`：删 avgPrice 兜底，接成本构成

**删 `_cost_with_source`**；把 `_cost` 重构为 `_cost_lots(asset_id, size) -> tuple[cost_or_None, lots]`：

```python
def _cost_lots(self, asset_id, size):
    """该持仓的加权成本 + 逐笔构成（本 tick 缓存）。只来自 CLOB get_trades 的真实买入
    成交，绝不回落 avgPrice。取不到 -> (None, [])。"""
    if asset_id in self._cost_cache:
        return self._cost_cache[asset_id]
    funder = self._funder()
    try:
        trades = self.api.get_trades(TradeParams(asset_id=asset_id))
    except Exception as e:
        logger.warning("get_trades(asset=%s) for cost failed: %s", asset_id, e)
        self._cost_cache[asset_id] = (None, [])
        return None, []
    fills = extract_buy_fills(trades, funder, asset_id)
    result = cost_basis_with_lots(fills, size)
    self._cost_cache[asset_id] = result
    return result
```

（`_cost_cache` 现缓存 `(cost, lots)` 元组。）

**`_reconcile_take_profit`（止盈）：**
- `cost, lots = self._cost_lots(asset_id, size)`。
- `cost is None or cost <= 0` → 不挂卖单，写 ⚠️ 状态行：`stage="止盈卖单"`、`action="⚠️跳过·裸奔"`、`detail="get_trades 无买入成交、无法算成本，未挂止盈（绝不用 avgPrice 兜底），该持仓未受保护"`，`log.warning`，return。
- 否则照常 `want = take_profit_price(cost, best_bid, tick)`，`plan_take_profit` 维持一笔卖单。`take_profit_sell` 的 `price_basis`：
  ```
  describe_cost_basis(cost, lots) + "；卖价=max(成本,买一+1tick)={want:.4f}；来源：CLOB get_trades + get_orderbook"
  ```

**`_check_pos_sl`（止损）：**
- `cost, lots = self._cost_lots(asset_id, size)`（替代原 `avg`）。
- `cost is None or cost <= 0` → 不止损，写 ⚠️ 状态行：`stage="Step2"`、`action="⚠️跳过·无成本"`、`detail="get_trades 无买入成交、无法算成本，未做止损保护"`，`log.warning`，return。
- 否则用 `cost` 判 `stop_loss_triggered(cur, cost, pct)`，市价平仓。`stoploss_market_sell` 的 `price_basis`：
  ```
  describe_cost_basis(cost, lots) + "、现价 curPrice={cur:.4f}；来源：CLOB get_trades + Data API /positions"
  ```
- `record_trade` 的 pnl 用 `(cur - cost) * size`。

**`pos["avgPrice"]` 不再被读取**用于成本。Data API 仅供 `size`（止盈/止损）与 `curPrice`（止损）。

### 4. 文档与记忆

- `CLAUDE.md`：那段已写"成本用 get_trades，NOT avgPrice"，本就与 2026-05-24 兜底相左；改完重新准确。补一句：取不到成交则跳过 + 醒目告警、卖单理由含逐笔成本构成（时间+价格×份额+trade_id）。
- memory `take-profit-position-driven.md`：把"受控 avgPrice 最后兜底（avgPrice 不再全禁）"更新为"avgPrice 全面禁用，成本只认 get_trades 逐笔"。

## 错误处理（容错哲学：不确定就不动手）

- `get_trades` 抛异常 → `_cost_lots` catch 返回 `(None, [])` → 跳过 + ⚠️告警，保持现有卖单不动（下个 tick 自愈）。
- get_trades 0 笔买入成交 → `(None, [])` → 跳过 + ⚠️告警。**不再有第二数据源**。
- 卖价穿价护栏仍在：即便将来成本计算有偏差，止盈也永远挂在买一之上当 maker，不市价砸盘。

## 测试计划（TDD，先红后绿）

**`tests/test_take_profit.py`：**
- `cost_basis_with_lots`：单笔全持有 → `(price, 1 笔 lot take==size)`；多笔加权 → 正确均价 + 多笔 lots；买入总量不足 size → 按已有份额加权 + lots 求和等于实取；部分消耗的边界笔 take 为实取量（非整笔）；无成交/size<=0 → `(None, [])`。
- `describe_cost_basis`：笔数文案、每笔 `价格×份额`、trade_id 缩写格式、超 6 笔出现"…等共 M 笔"汇总；时间串只验结构（含 `-`、`:`），不验时区相关具体数值。
- `cost_basis_from_buy_fills` 既有用例全绿（薄包装回归）。

**`tests/test_monitor.py`：**
- **反转** `test_falls_back_to_avgprice_when_no_trades`：get_trades 空（不管 avgPrice 多少）→ 不挂卖单 + 写 `⚠️跳过·裸奔` 状态行。
- **反转** `test_stop_loss_falls_back_to_avgprice`：get_trades 空 → 不撤单/不市价平仓 + 写 `⚠️跳过·无成本` 状态行。
- `test_no_fallback_when_get_trades_has_data`：断言从 `"get_trades加权"` 改为校验 `price_basis` 含逐笔构成（`加权自`、`×`、`trade`、`共取`）。
- 保留并按需调整：taker 成交挂卖、两源皆空跳过、穿价护栏（成本<买一时卖价上移）、`maker_address` 省略等用例（去掉 avgPrice 维度断言）。
- 止盈/止损 `price_basis` 不再出现 `avgPrice兜底`（可加反向断言）。

## 守住的既有关键行为（不改）

- 每个持仓**恰好一笔**止盈卖单（`plan_take_profit`）。
- 卖价穿价护栏 `max(ceil_to_tick(成本), 买一+1tick)`。
- 止盈跑在 Step1 之后；Step3 跳过 `side!="BUY"` 与部分成交单。
- 停引擎仍只撤买单。
- 监控水位线、黑名单、Step3 调单等其余逻辑不变。
- 补抓 taker 成交（2026-05-24 第一层）保留。
