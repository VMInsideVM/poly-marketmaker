# 低余额清仓（low-balance liquidation）设计 / spec

> 日期：2026-07-17　　状态：已批准，待写实现计划

## 一、背景与目标

现金余额过低时做市引擎基本停摆（`place_orders` 跳过 `min_cost > 余额` 的市场，挂不出奖励单）。
本功能：当某钱包现金余额 `< low_balance_threshold`（默认 4u）时，**按优先级逐笔市价卖持仓腾现金**，
恢复到「停手目标」为止。**这是主动清仓，覆盖「卖单永不低于成本」铁律**（与结算清仓同类——为了继续
farming 而认亏，用户明确选择：第三档就是「卖损失最小的」）。纯资金管理行为，逐笔卖用现有市价卖机器。

## 二、触发与优先级

每个 WalletWorker 每 tick 先查余额；`余额 ≥ low_balance_threshold` → 秒退（仅一次 `get_balance`）。
`余额 < 阈值`（阈值 `0` = 关闭本功能）→ 进清仓，按下列**优先级顺序**逐笔卖，直到「停手目标」达成或候选卖光：

1. **档1**：所在市场 `daily_reward < low_reward_threshold`（默认 30u）的持仓（低奖励市场，最不值得留）。
2. **档2**（不在档1）：`份额 < small_position_shares`（默认 20）的持仓。
3. **档3**（其余）：按亏损从小到大。

- **档内 + 档3 统一按「亏损从小到大」排**（先卖最不心疼的，含盈利仓 = 负亏损排最前）。
- `亏损 = (成本 − 买一) × 份额`；**成本只走 `get_trades` 逐笔重建**（`_cost_lots`，禁 avgPrice/curPrice，见 [[take-profit-position-driven]]）。
- **成本取不到**的持仓（`_cost_lots` 返回 None，新成交未进 get_trades）：**跳过、推迟**到下 tick 成本重建
  （与 check_exit 的裸奔跳过一致；低余额无结算 deadline，不必像结算清仓那样成本未知也照卖——**2026-07-17
  评审后由「仍照卖」改为「跳过」**，避免刚买入的新仓被低于未知成本甩卖）。`plan_liquidation` 仍保留
  loss=None→档末的处理（供 best_bid 空但成本已知的情形）。
- **无买盘**（`best_bid` 空/≤0）的持仓：市价卖不出 → **跳过该笔**（不计入已腾现金），下 tick 有买盘再清。

## 三、停手目标（配置页选，两模式）

- `liquidate_target_mode = "balance"`（默认）：卖到 `估算余额 ≥ liquidate_target_usd`（默认 4u）就停。
- `= "next_order"`：卖到 `估算余额 ≥ 下一单 min_cost` 就停。`下一单 min_cost` 取当前
  `eligible_markets` 表里 **`MIN(min_cost)`**（能挂得起最便宜的一单）；表空则回退 `low_balance_threshold`。

**用「估算余额」而非每笔重查真实余额**：市价卖后链上余额有秒级滞后，逐笔重查会因还没到账而**过卖**
（多认亏）。故取初始 `get_balance()` 为基，每卖一笔加 `估算到手 = best_bid × 份额`，`估算 ≥ 目标` 即停。
best_bid 偏保守（实际成交可能略低）→ 倾向少卖 → 不够则下 tick 再触发，自愈；绝不过卖。

## 四、卖出机制

逐笔：先撤该 asset 的挂卖单（否则份额被占，市价卖没份额）→ `place_market_sell(asset, size)`（FAK，全仓）→
`market_fill_price(resp, best_bid, cur)` 记成交价（**绝不用 Data API 现价**）→ 记 `record_trade(side="stop_loss",
pnl=(fill−cost)×size)`（成本已知时）+ `record_action(action_type="exit_market", reason="低余额清仓…")` + 状态行。
**覆盖「永不低于成本」**（主动清仓）。发生在 `check_exit` **之前**，卖掉的仓自然不再被离场逻辑维护卖单。

## 五、配置（模板级，`TEMPLATE_DEFAULTS`）

与止损等离场参数同级、每钱包/每模板可不同：

- `low_balance_threshold_usd`（默认 4；`0` = 关闭）
- `low_reward_threshold_usd`（默认 30）
- `small_position_shares`（默认 20）
- `liquidate_target_mode`（`"balance"` | `"next_order"`，默认 `"balance"`）
- `liquidate_target_usd`（默认 4；仅 `mode="balance"` 用）

config.html 新增「低余额清仓」区（阈值/奖励线/份额 数字输入 + 目标模式下拉 + 目标 U）。`/api/templates/<id>`
PUT 已按 `TEMPLATE_DEFAULTS` 白名单存取；select 需前端单独收（见 [[take-profit-position-driven]] 配置链路坑）。

## 六、数据来源

- **市场奖励 `daily_reward`**：查 `eligible_markets` 表（`db.get_market_daily_reward(condition_id) →
  float|None`，`SELECT daily_reward FROM eligible_markets WHERE market_id=? LIMIT 1`）。持仓市场多半近期扫过、
  在表里；**不在表里 → `None` → 不进档1**（保守，当高奖励对待，不优先卖）。**不为持仓现拉 rewards API**
  （避免复刻 scanner 的 reward 提取逻辑;YAGNI + 更稳）。
- **成本 / 买一**：`_cost_lots`（成本）+ `_sell_book`（best_bid/cur/tick）——均 monitor 现成。
- **eligible MIN(min_cost)**：新增 `db.get_min_order_cost() → float|None`（`SELECT MIN(min_cost) FROM eligible_markets`）。

## 七、落点

monitor 新增步骤 `check_low_balance()`，插入 `_tick`：`check_buy_orders → check_resolution → check_low_balance
→ check_exit → check_sell_orders`（清仓在离场之前）。整体 try/except、失败 WARNING 不阻断其余步骤。
`余额 ≥ 阈值` 时秒退（不拉持仓/成本）。

## 八、纯函数 `plan_liquidation`（`engine/liquidation.py`，无 IO，全单测）

```
plan_liquidation(candidates, low_reward_threshold, small_shares_threshold) -> list[str]
```

- `candidates`: `[{asset_id, size, daily_reward(或 None), loss(或 None)}]`。
- 分档：档1 `daily_reward is not None and daily_reward < low_reward_threshold`；档2 `size < small_shares`（不在档1）；档3 其余。
- 每档内按 `loss` 升序（`None` 视为 `+∞` 排末尾）；返回 `档1+档2+档3` 的 asset_id 顺序列表。
- 「停手目标」不在纯函数内——编排器按估算余额逐笔停（纯函数只定顺序）。

## 九、测试

- `plan_liquidation` 全单测：仅档1、跨档、盈利仓（负 loss 排前）、cost-unknown（排档末）、档2 仅按份额、空输入。
- `check_low_balance` 编排单测（mock api/db）：余额≥阈值不动；余额<阈值按顺序逐笔市价卖、估算到手到目标即停、
  先撤挂卖单、无买盘跳过、成本已知记 pnl/未知不记、两种 target 模式、失败不抛。
- `db.get_min_order_cost` 单测。
- 配置链路（`TEMPLATE_DEFAULTS` 白名单往返）+ 前端表单渲染走查。

## 十、不做（YAGNI）

- 不撤挂买单腾现金（用户只要求卖持仓；买单侧另说）。
- 不做逐笔真实余额重查（用估算，防过卖）。
- 不为 cost-unknown 仓做重型链上成本追溯（同结算清仓：照卖、不记 pnl）。
- 只市价卖（不提供 maker 清仓选项）。
- 不改 `check_exit`/`place_orders` 既有逻辑（新增独立步骤,清仓在离场前）。
