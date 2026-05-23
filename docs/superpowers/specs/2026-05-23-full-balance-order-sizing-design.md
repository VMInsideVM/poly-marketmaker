# 设计：买单按全额余额下单（共用保证金）

日期：2026-05-23

## 背景与问题

当前每个合格市场的买单数量写死为扫描记录的 `rewards_min_size`（拿奖励的最小合格份额），见 `engine/scanner.py:243` `"order_size": min_size` 与 `engine/manager.py` `place_orders` 里使用 `market["order_size"]`。结果是无论钱包里有多少钱，每个市场都只挂最小单，大量资金闲置。

用户要求：**按最大余额挂单**，把闲置资金用起来。

## 关键事实：Polymarket maker 买单不锁仓

Polymarket CLOB 的 maker（挂单方）买单在**挂单时不冻结保证金**，USDC 留在钱包里，只有**成交**才真正扣款。因此同一笔余额可以同时垫付任意多个挂着的买单——保证金是**共用**的，只要这些买单都还没成交。

代码现状已经符合这个模型：`place_orders` 的循环每次都重新读同一个 `balance`，并且**不**把已挂出去的金额从余额里减掉，只单独校验"当前这一笔 ≤ 余额"（`engine/manager.py:192-194`）。也就是说，框架早就是"共用保证金"，唯一没放开的就是把单量写死成了 `min_size`。CLAUDE.md 里"Balance is re-read before every order placement … so concurrent fills don't cause overspend"针对的也是**成交**导致的超支，而非挂单。

## 目标

- 每个合格市场的买单按**全部可用余额**能买到的份数下单。
- 同一笔余额共用于所有挂着的买单，挂单之间**不**递减余额。
- 铺多少个市场仍由现有设置 `max_buy_orders_per_wallet` 控制。

## 方案

### 核心改动：`engine/manager.py` `place_orders`

落单前已经会用实时订单簿重新算出 `order_price`（`manager.py:179`）。把下单数量从 `market["order_size"]`（即 `min_size`）改为用全额余额反推的份数：

```
balance    = self.api.get_balance()          # 每个市场循环都重新读，不递减
order_size = floor(balance / order_price)    # 全额能买到的整数份数
```

然后 `place_limit_buy(token_id, order_price, order_size, ...)`。

### 护栏（保留 / 调整）

1. **奖励最小份额下限**：若 `floor(balance / order_price) < rewards_min_size`，说明全额都凑不够该市场拿奖励的最小合格单，跳过该市场（`continue`）。
2. **`min_cost` 门槛不变**：循环开头仍先用 `balance < min_cost` 粗筛（`manager.py:150-157`），余额连最小合格单都挂不起的市场直接跳过，省去取订单簿/算策略的开销。
3. **`required > balance` 判断**：因为 `order_size` 是从 `balance` 反推的，`order_size × order_price ≤ balance` 必然成立，这段（`manager.py:192-201`）变成恒真的空操作，顺手简化/移除，逻辑上由上面的"最小份额下限"接管。

### 数量取整

`place_limit_buy` 的 `size` 参数本就是份数（内部 `float(size)`），全项目其他地方用 `int(float(...))` 取整。本方案对 `floor(balance / order_price)` 取整为 int，与现有约定一致。

### 不需要改动的地方

- **市场数量上限**：仍由 `max_buy_orders_per_wallet`（默认 5）控制，`place_orders` 现有的 `slots` / `effective_limit` 逻辑不变。即"最多 N 个市场，每个都按全额挂，共用同一笔钱"。
- **监控 Step 3 重挂**：重挂买单时用的是订单的 `original_size`（`engine/monitor.py:573,595`），会自动沿用最初挂出的全额份数，不会把单子缩回 `min_size`。
- **`engine/scanner.py`**：仍记录 `order_size`（= `min_size`）与 `min_cost`。`order_size` 不再被 `place_orders` 用作下单量（改由全额反推），但保留作为参考/最小份额来源（即 `rewards_min_size`）；`min_cost` 仍用于扫描门槛。

## 风险与取舍（用户已确认知悉）

N 个全额买单共用同一笔保证金时：

- 若 **2 笔以上几乎同时成交**，余额只够覆盖其中一笔，多出来的成交会在链上撮合时失败。
- 一旦某一笔成交，资金基本满仓压在那一个仓位上，其余"超额"买单进入保证金不足状态（再被撮合也会失败）。

机器人读取链上实时持仓与订单，账目不会错乱，下一轮扫描/监控会基于新的余额与持仓重新评估。集中成交风险是该打法自带的取舍，用户已确认接受。

## 测试

- 更新 `tests/test_place_orders.py`：
  - 余额充足时，下单数量 = `floor(balance / order_price)`，而非 `rewards_min_size`。
  - `floor(balance / order_price) < rewards_min_size` 时跳过该市场，不下单。
  - 循环内余额不递减：多个市场都按同一个 `balance` 反推份数（mock `get_balance` 返回固定值，验证每个市场的 size 都按全额算）。
  - `min_cost` 门槛与 `max_buy_orders_per_wallet` 上限行为不回归。
- 不触网：沿用现有测试对 `PolymarketAPI` / `db` 的 mock 方式。
