# 买单按全额余额下单 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把每个合格市场的买单数量从写死的 `rewards_min_size` 改成「全部可用余额能买到的份数」，让闲置资金用起来。

**Architecture:** Polymarket maker 买单不锁仓、同一笔余额共用于所有挂着的买单，只有成交才扣款。`engine/manager.py` 的 `place_orders` 循环本就每次重读同一个余额、不递减，唯一改动是把下单量从 `min_size` 换成 `floor(balance / order_price)`，并加一条「全额都买不够 `rewards_min_size` 就跳过」的下限。铺几个市场仍由 `max_buy_orders_per_wallet` 控制，监控 Step 3 重挂用 `original_size` 自动沿用全额，均不需改。

**Tech Stack:** Python 3, pytest, unittest.mock（纯逻辑、不触网）。

参考 spec：`docs/superpowers/specs/2026-05-23-full-balance-order-sizing-design.md`

---

## File Structure

- Modify: `engine/manager.py`
  - `WalletWorker.place_orders`（约 `192-218` 行）：替换「`required > balance` 校验 + 用 `market["order_size"]` 下单」一段，改为全额反推份数 + 最小份额下限。
  - `WalletWorker._record_place_buy`（约 `247-265` 行）：新增 `order_size` 参数，动作日志记录真实下单份额而非 `market["order_size"]`。
- Test: `tests/test_place_orders.py`：新增 4 个测试，覆盖全额下单、余额不递减、低于最小份额跳过、动作日志记真实份额。

只有这一处生产代码改动，单个 Task 完成。

---

### Task 1: place_orders 按全额余额下单

**Files:**
- Modify: `engine/manager.py:192-218`（`place_orders` 下单段）、`engine/manager.py:247-265`（`_record_place_buy`）
- Test: `tests/test_place_orders.py`

说明（给实现者）：
- `tests/test_place_orders.py` 顶部已有 helper：`_worker(api, db, cap=5)`、`_market(i)`（含 `order_size: 10`，**不含** `rewards_min_size`）、`_ok_orderbook()`（bids `0.40` / asks `0.42` / tick `0.01`）。新测试复用它们。
- 测试里用 `patch("engine.strategy.determine_order_price", return_value=0.50)` 把落单价钉死为 `0.50`，因为 `0.5` 在浮点里精确可表示，`1000.0 / 0.50 == 2000.0`、`3.0 / 0.50 == 6.0`，断言不会被浮点误差影响。
- `place_limit_buy` 在 `place_orders` 里是按位置参数调用的 `(token_id, order_price, order_size, tick_size=..., neg_risk=...)`，所以下单份额是 `call_args.args[2]`。

- [ ] **Step 1: 写失败测试（4 个）**

把以下 4 个测试追加到 `tests/test_place_orders.py` 末尾：

```python
def test_order_size_uses_full_balance():
    # 每笔买单按全部可用余额下单：floor(balance/price),不是 rewards_min_size。
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    with patch("engine.strategy.determine_order_price", return_value=0.50):
        worker.place_orders([_market(0)])
    api.place_limit_buy.assert_called_once()
    assert api.place_limit_buy.call_args.args[2] == 2000  # floor(1000/0.50)


def test_balance_not_decremented_across_markets():
    # 同一笔余额垫付所有挂单,跨市场不递减 —— 每个市场都拿到全额份数。
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    markets = [_market(i) for i in range(3)]
    with patch("engine.strategy.determine_order_price", return_value=0.50):
        worker.place_orders(markets)
    sizes = [c.args[2] for c in api.place_limit_buy.call_args_list]
    assert sizes == [2000, 2000, 2000]


def test_skip_when_full_balance_below_min_reward_size():
    # 全额都买不够 rewards_min_size 份时跳过该市场(挂更少拿不到奖励)。
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 3.0  # floor(3.0/0.50)=6 份
    worker = _worker(api, db)
    m = _market(0)
    m["rewards_min_size"] = 10  # 需要 >=10,只买得起 6 -> 跳过
    with patch("engine.strategy.determine_order_price", return_value=0.50):
        worker.place_orders([m])
    api.place_limit_buy.assert_not_called()


def test_place_buy_action_records_full_size():
    # 动作记录里 size 是真实下单的全额份数,不是扫描时的最小份额。
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    with patch("engine.strategy.determine_order_price", return_value=0.50):
        worker.place_orders([_market(0)])
    pb = next(
        c
        for c in db.record_action.call_args_list
        if c.kwargs.get("action_type") == "place_buy"
    )
    assert pb.kwargs["size"] == 2000
```

- [ ] **Step 2: 运行新测试,确认失败**

Run: `pytest tests/test_place_orders.py -k "full_balance or not_decremented or below_min_reward or records_full_size" -v`
Expected: 4 个全部 FAIL（当前下单量是 `market["order_size"]==10`，`call_args.args[2]` 是 `10` 而非 `2000`；`test_skip_when_full_balance_below_min_reward_size` 现在会照常下单因此断言 `assert_not_called` 失败）。

- [ ] **Step 3: 改 `place_orders` 下单段**

在 `engine/manager.py` 中，把这一段（`192-218` 行）：

```python
            balance = self.api.get_balance()
            required = market["order_size"] * order_price
            if required > balance:
                logger.info(
                    "Insufficient balance %.2f < %.2f for %s",
                    balance,
                    required,
                    market["market_name"],
                )
                continue
            try:
                self.api.place_limit_buy(
                    market["token_id"],
                    order_price,
                    market["order_size"],
                    tick_size=tick_str,
                    neg_risk=market.get("neg_risk", False),
                )
                logger.info(
                    "Placed buy %s [%s] @ %.4f x %d",
                    market["market_name"],
                    market["outcome"],
                    order_price,
                    market["order_size"],
                )
                placed += 1
                self._record_place_buy(market, order_price, max_spread, rmin, rmax)
```

替换为：

```python
            balance = self.api.get_balance()
            # 每笔买单按全部可用余额下单。Polymarket maker 买单不锁仓,同一笔
            # 余额垫付所有挂着的买单,所以这里跨市场循环时刻意 *不* 递减余额。
            order_size = int(balance / order_price)
            # 全额都买不够拿奖励的最小合格份额,挂了也吃不到奖励 -> 跳过该市场。
            min_size = int(
                market.get("rewards_min_size", market.get("order_size", 0)) or 0
            )
            if order_size < min_size:
                logger.info(
                    "Balance %.2f only buys %d < min reward size %d for %s, skip",
                    balance,
                    order_size,
                    min_size,
                    market["market_name"],
                )
                continue
            try:
                self.api.place_limit_buy(
                    market["token_id"],
                    order_price,
                    order_size,
                    tick_size=tick_str,
                    neg_risk=market.get("neg_risk", False),
                )
                logger.info(
                    "Placed buy %s [%s] @ %.4f x %d",
                    market["market_name"],
                    market["outcome"],
                    order_price,
                    order_size,
                )
                placed += 1
                self._record_place_buy(
                    market, order_price, order_size, max_spread, rmin, rmax
                )
```

- [ ] **Step 4: 改 `_record_place_buy` 记录真实份额**

把 `engine/manager.py:247-256` 这一段：

```python
    def _record_place_buy(self, market, order_price, max_spread, rmin, rmax):
        """Log a successful buy placement to the actions table (never raises)."""
        try:
            self.db.record_action(
                wallet=self.wallet_address,
                market_id=market["market_id"],
                action_type="place_buy",
                side="买入",
                price=order_price,
                size=market["order_size"],
```

替换为（仅签名多了 `order_size`，`size=` 改用它）：

```python
    def _record_place_buy(
        self, market, order_price, order_size, max_spread, rmin, rmax
    ):
        """Log a successful buy placement to the actions table (never raises)."""
        try:
            self.db.record_action(
                wallet=self.wallet_address,
                market_id=market["market_id"],
                action_type="place_buy",
                side="买入",
                price=order_price,
                size=order_size,
```

（该方法剩余部分 `reason=...` / `price_basis=...` / `except` 不变。）

- [ ] **Step 5: 运行新测试,确认通过**

Run: `pytest tests/test_place_orders.py -k "full_balance or not_decremented or below_min_reward or records_full_size" -v`
Expected: 4 个全部 PASS。

- [ ] **Step 6: 跑整个文件,确认无回归**

Run: `pytest tests/test_place_orders.py -v`
Expected: 全部 PASS（原有的 cap / limit / min_cost / place_buy 记录等测试不受影响——它们不断言下单份额；`required > balance` 那段移除后由「最小份额下限」接管，余额充足场景行为不变）。

- [ ] **Step 7: 跑全套测试**

Run: `pytest`
Expected: 全部 PASS（本改动只动 `place_orders` 与 `_record_place_buy`，不影响 scanner/strategy/monitor 的单测）。

- [ ] **Step 8: 提交**

只 stage 本任务涉及的两个文件（按项目约定，别带进未提交的其它 WIP）：

```bash
git add engine/manager.py tests/test_place_orders.py
git commit -m "feat: 买单按全额余额下单(共用保证金),低于最小份额则跳过

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec 覆盖**：
  - 全额下单 `floor(balance/order_price)` → Step 3 + `test_order_size_uses_full_balance` ✓
  - 循环内余额不递减 → Step 3（不递减）+ `test_balance_not_decremented_across_markets` ✓
  - 最小份额下限跳过 → Step 3 + `test_skip_when_full_balance_below_min_reward_size` ✓
  - `min_cost` 门槛保留 → 未触碰 `manager.py:150-157`，回归由原有 `test_skips_market_when_balance_below_min_cost` 保护 ✓
  - `required > balance` 简化为空操作并移除 → Step 3 ✓
  - 动作日志记真实份额 → Step 4 + `test_place_buy_action_records_full_size` ✓
  - 市场数量上限 `max_buy_orders_per_wallet` 不变 → 未触碰，原有 cap 系列测试保护 ✓
  - 监控 Step 3 用 `original_size` 自动沿用全额 → 无需改动（spec 已说明）✓
  - scanner 仍记 `order_size`/`min_cost` → 未触碰 ✓
- **占位符扫描**：无 TBD/TODO，每个代码步骤都给了完整代码。✓
- **类型/签名一致**：`_record_place_buy` 新签名 `(market, order_price, order_size, max_spread, rmin, rmax)` 与 Step 3 的调用 `self._record_place_buy(market, order_price, order_size, max_spread, rmin, rmax)` 一致。✓
