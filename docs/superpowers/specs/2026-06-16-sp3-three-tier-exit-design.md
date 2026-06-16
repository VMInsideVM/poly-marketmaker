# SP3：三段式离场（three-tier exit）设计 / spec

> 日期：2026-06-16
> 状态：待用户评审
> v4 做市策略接入的第三个子项目。父背景见记忆 [[v4-strategy-integration-roadmap]] 与 [[take-profit-position-driven]]。

## 零、背景与定位

SP1 解耦配置、SP2 重写挂单。本子项目按 v4 §7 重写**离场逻辑**：把当前「止盈（浮亏时绝不低于成本卖、挂在成本价等回升）+ 百分比止损」两套，合并成一个**三段式主动限损离场**——浮亏时主动扫单认亏（最多到 θ_loss），再深就 θ_stop 市价清仓。

**已确认的关键决策**：
1. 完整 v4 §7 三段式（用户确认逆转「浮亏绝不低于成本卖」的护栏）。
2. `θ_loss = 2¢`、`θ_stop = 5¢`（默认，模板参数，按美分全模板统一，θ_stop > θ_loss）。
3. Case A（成本 ≤ 买一，无损）默认 `挂卖一`（maker），可切 `市价`。
4. 沿用 SP2 打法：纯函数决策 + monitor 执行 + 复用既有成本重建（get_trades 加权 + 裸奔跳过）。

**不在 SP3**：SP4 单份奖励阈值+取档、SP5 三档节奏+观察名单+成交后单侧暂停+撤改收敛、SP6 模板 UI。

## 一、架构

- **新纯函数 `plan_exit(...)`**（加进 `engine/take_profit.py`，与既有 `take_profit_price`/`plan_take_profit` 同模块；不触网，全单测）：三段式决策，输出一个离场动作。
- **monitor 合并 `check_take_profit` + `check_stop_loss` 为一个 `check_exit`**：每持仓重建成本（复用 `_cost_lots` + 裸奔跳过）、取盘口（`_sell_book` + best_ask）、算 `plan_exit`、执行。
- **API 加 `place_marketable_limit_sell`**：FAK 限价卖（扫到价格下限的缺失原语）。
- **复用不动**：`_cost_lots` / `position_cost_with_lots` / `plan_take_profit`（维护一笔挂卖单对账）/ `ceil_to_tick` / `describe_cost_basis` / `_sell_book` / `record_trade` / 裸奔状态行。

## 二、`plan_exit` 决策（核心 IP）

**签名**：`plan_exit(cost, best_bid, best_ask, tick, theta_loss, theta_stop, case_a_mode, size) -> dict`
- `theta_loss` / `theta_stop` 为**价位单位**（= ¢ / 100；monitor 把模板的 `theta_*_cents` 除 100 后传入）。
- 返回 `{"tier", "action", "price", "size"}`，`action ∈ {"rest","market","sweep","noop"}`。

**决策表**（`亏损 = cost − best_bid`，`最低卖出价 = cost − theta_loss`）：

| 条件 | tier | action | price |
| --- | --- | --- | --- |
| `best_bid is None`（盘口无买盘）且 `best_ask` 有 | `A`/park | `rest` | `best_ask` |
| `best_bid is None` 且 `best_ask` 也无 | `none` | `noop` | — |
| `cost ≤ best_bid`（无损）且 `case_a_mode="ask"` | `A` | `rest` | `best_ask`（无则 `ceil_to_tick(cost,tick)`） |
| `cost ≤ best_bid` 且 `case_a_mode="market"` | `A` | `market` | — |
| `cost > best_bid` 且 `亏损 ≥ theta_stop` | `B0` | `market` | — |
| `cost > best_bid` 且 `亏损 < theta_stop` 且 `best_bid ≥ 最低卖出价` | `B_sweep` | `sweep` | `最低卖出价`（对齐 tick：`ceil_to_tick`） |
| `cost > best_bid` 且 `亏损 < theta_stop` 且 `best_bid < 最低卖出价` | `B_park` | `rest` | `best_ask`（无则 `ceil_to_tick(cost,tick)`） |

`size`：原样透传（整仓）。`cost is None`/`cost ≤ 0`/`size ≤ 0` 由调用方在调用前拦（裸奔跳过），`plan_exit` 不处理。

> **rest 价 = 卖一（best_ask），不再有 `max(成本, 买一+tick)` 护栏**——这是 v4 §7「挂卖一」原意（吃满价差）；B_park 时卖一可能低于成本，是认亏 park 等回升，刻意为之。`sweep` 价对齐 tick 用 `ceil_to_tick`（限价卖向上取整不会卖穿到更低）。

## 三、monitor 执行（`check_exit`）

`check_exit` 取 `positions`（Data API）+ `open_orders`（CLOB），逐持仓：

1. `size = pos.size`；`size ≤ 0` 跳过。
2. `cost, lots = self._cost_lots(asset, size, cid)`。`cost is None or ≤0` → **裸奔跳过 + ⚠️ 状态行**（统一一条，复用现文案），return。
3. `tick, tick_str, best_bid = self._sell_book(asset)`；`best_ask` 从同一订单簿取（`_sell_book` 扩展为也返回 best_ask，或新增取 ask）。
4. `plan = plan_exit(cost, best_bid, best_ask, tick, theta_loss, theta_stop, case_a_mode, size)`。
5. 执行：
   - **rest**（A-ask / B_park）：用 `plan_take_profit(size, plan["price"], tick, sells)` 维护**恰好一笔**挂卖单在 `plan["price"]`（卖一）。`keep/noop` 不动；`replace` 撤旧挂新（`place_limit_sell` GTC）。记 action + 状态行（注明 tier、成本构成 `describe_cost_basis`）。
   - **market**（A-market / B0）：先撤该 asset 全部挂卖单，再 `place_market_sell(asset, size)`（FAK）。B0 额外 `record_trade(side="stop_loss", price=cur, pnl=(cur−cost)*size)`（沿用现止损落库）。记 action + 状态行。
   - **sweep**（B_sweep）：先撤该 asset 全部挂卖单，再 `place_marketable_limit_sell(asset, plan["price"], size, tick_str)`（FAK 限价扫到下限）。**不**同 tick 重 park——FAK 异步、当轮拿不到准确剩余，下个 tick 按缩小后的 Data API 持仓重新评估（剩余届时若 `best_bid < 最低卖出价` 自然走 B_park 挂卖一）。记 action + 状态行。
   - **noop**：状态行标「盘口空·跳过」。

`_tick`（`engine/manager.py`）把 `check_take_profit()` + `check_stop_loss()` 两行换成一行 `check_exit()`（顺序：`check_buy_orders → check_exit → check_sell_orders → publish_status`）。

> **撤改收敛**：rest 动作沿用 `plan_take_profit` 的「价/量不变就保持」对账，不 churn。sweep/market 是 FAK 即发即灭，先撤后发避免与残留挂卖单双挂（与现 check_stop_loss 的「先撤 SELL 再市价」一致）。

## 四、新 API 原语

`api/polymarket_api.py` 新增：

```python
def place_marketable_limit_sell(self, token_id, price, size, tick_size="0.01", neg_risk=None) -> dict:
    """限价卖但 FAK(fill-and-kill):吃掉所有 >= price 的买单后 kill 剩余,永不挂簿。

    用于 v4 §7 B 段逐级扫单——以「最低卖出价」为限价的市价单,绝不成交在该价以下
    (盘口瞬时假跌也卖不穿,自带防错杀)。neg_risk=None 走自动解析(与其它卖单一致,
    防负风险仓卖不出)。
    """
    order_args = OrderArgs(token_id=token_id, price=price, size=float(size), side="SELL")
    options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
    res = self.client.create_and_post_order(order_args, options, OrderType.FAK)
    return _check_order_resp(res, "限价扫单(FAK)")
```

（与 `place_limit_sell` 同构，仅 `OrderType.GTC` → `OrderType.FAK`。）

## 五、退役清单

- `TEMPLATE_DEFAULTS` 删 `stop_loss_pct`，加 `theta_loss_cents`(2) / `theta_stop_cents`(5) / `case_a_mode`("ask")。
- `engine/take_profit.py` 删 `take_profit_price`（rest 价改卖一）；保留 `plan_take_profit`/`ceil_to_tick`/`position_cost_with_lots`/`describe_cost_basis`。
- `engine/risk.py` `stop_loss_triggered` 退役（删文件 + `tests/test_risk.py` 若有）。
- `engine/monitor.py` 删 `check_take_profit`/`_reconcile_take_profit`/`check_stop_loss`/`_check_pos_sl`，新增 `check_exit` + 每持仓执行;删 `from engine.risk import stop_loss_triggered`、`take_profit_price` 的 import。
- `web/routes.py` `api_get_positions` 止损价显示改用 θ_stop：`stop_price = avgPrice − theta_stop_cents/100`（展示近似；真实强平用 get_trades 成本 − θ_stop）。
- 删 `take_profit_price`/`stop_loss_triggered` 的旧用例。

## 六、测试

- **`tests/test_exit_plan.py`（纯函数，核心）**：A-ask / A-market / B0 / B_sweep / B_park / 盘口空(best_bid None → rest 卖一 / 全空 → noop) / 边界（亏损恰 θ_stop → B0；best_bid 恰 = 最低卖出价 → sweep；cost 恰 = best_bid → A）。
- **`tests/test_monitor.py`**：`check_exit` 各段触发正确 API 调用（rest→维护一笔 `place_limit_sell`；sweep→撤 SELL + `place_marketable_limit_sell`；B0/A-market→撤 SELL + `place_market_sell`）；rest 对账 keep/replace；裸奔 → 跳过 + ⚠️ 状态、不下任何卖单。
- **API 原语**：mock client 验 `place_marketable_limit_sell` 用 `OrderType.FAK` + 正确 OrderArgs。
- 删 `tests/test_strategy`/`test_risk` 中 `take_profit_price`/`stop_loss_triggered` 的用例。

## 七、验收 checkpoint

1. 成本 ≤ 买一：维护一笔挂卖单在卖一（case_a_mode=ask）；切 market 则撤+市价。
2. 浮亏、亏损 < θ_stop、best_bid ≥ 最低卖出价：撤旧卖单 + FAK 限价扫到最低卖出价。
3. 浮亏、亏损 ≥ θ_stop：撤旧卖单 + 市价清仓 + record_trade。
4. 浮亏、亏损 < θ_stop、best_bid < 最低卖出价：整仓挂卖一。
5. 成本取不到：裸奔跳过 + ⚠️ 告警，绝不下卖单。
6. `pytest` 全绿；`stop_loss_pct`/`stop_loss_triggered`/`take_profit_price` 已退役无残留引用。

## 八、范围之外

SP4 单份奖励阈值+取档 · SP5 三档节奏+观察名单+成交后单侧暂停+撤改收敛 · SP6 模板 UI（θ/case_a_mode 编辑、退役死字段收口）。
