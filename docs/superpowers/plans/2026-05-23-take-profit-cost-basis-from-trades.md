# 止盈/止损成本价改用 get_trades 加权成本 + 穿价护栏 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 止盈/止损不再依赖 Data API `avgPrice`，改用 CLOB `get_trades` 真实买入成交算加权成本，并给止盈卖价加穿价护栏，杜绝刚建仓被市价清仓。

**Architecture:** 无状态：每个监控 tick 对每个持仓按 `asset_id` 查一次 `get_trades`、现算加权成本（数量仍信 Data API `size`）。新增纯函数 `extract_buy_fills`/`cost_basis_from_buy_fills`/`take_profit_price`，`monitor.py` 用一个 per-tick 缓存的 `_cost()` 把成本喂给止盈和止损。

**Tech Stack:** Python 3.12, pytest, `py-clob-client-v2`（`TradeParams`）。

参考 spec：`docs/superpowers/specs/2026-05-23-take-profit-cost-basis-from-trades-design.md`

---

## 文件结构

- `engine/fills.py` — 新增 `extract_buy_fills`（与 `select_new_buy_fills` 并列的纯函数）
- `engine/take_profit.py` — 新增 `cost_basis_from_buy_fills`、`take_profit_price`；改 `plan_take_profit` 签名（`avg`→`want_price`）
- `engine/monitor.py` — 新增 `_cost()` + per-tick 缓存、`_sell_tick`→`_sell_book`；改 `_reconcile_take_profit`、`_check_pos_sl`
- 测试：`tests/test_fills.py`、`tests/test_take_profit.py`、`tests/test_monitor.py`

---

### Task 1: `extract_buy_fills` 纯函数

**Files:**
- Modify: `engine/fills.py`
- Test: `tests/test_fills.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_fills.py` 末尾追加（文件顶部已有 `FUNDER`、`TRADE_SELL_MIX`、`TRADE_TWO_OURS_BUY`）：

```python
from engine.fills import extract_buy_fills


def test_extract_buy_fills_only_our_buys_on_asset():
    # TRADE_TWO_OURS_BUY 有我们两笔 BUY:ASSET_B1@0.4x20、ASSET_B2@0.41x30
    fills = extract_buy_fills([TRADE_TWO_OURS_BUY], FUNDER, "ASSET_B1")
    assert fills == [{"price": 0.4, "size": 20.0, "ts": 1779030000.0}]


def test_extract_buy_fills_skips_our_sell_and_others():
    # TRADE_SELL_MIX:我们的是 SELL(ASSET_YES),另一笔是别人的 BUY(ASSET_NO)
    assert extract_buy_fills([TRADE_SELL_MIX], FUNDER, "ASSET_YES") == []
    assert extract_buy_fills([TRADE_SELL_MIX], FUNDER, "ASSET_NO") == []


def test_extract_buy_fills_case_insensitive_funder():
    fills = extract_buy_fills([TRADE_TWO_OURS_BUY], FUNDER.lower(), "ASSET_B2")
    assert fills == [{"price": 0.41, "size": 30.0, "ts": 1779030000.0}]


def test_extract_buy_fills_aggregates_across_trades_same_asset():
    t2 = dict(TRADE_TWO_OURS_BUY)
    t2 = {**TRADE_TWO_OURS_BUY, "id": "trade-3", "match_time": "1779031111",
          "maker_orders": [{
              "order_id": "ord-b3", "maker_address": FUNDER, "side": "BUY",
              "matched_amount": "10", "price": "0.42", "asset_id": "ASSET_B1",
          }]}
    fills = extract_buy_fills([TRADE_TWO_OURS_BUY, t2], FUNDER, "ASSET_B1")
    assert {"price": 0.4, "size": 20.0, "ts": 1779030000.0} in fills
    assert {"price": 0.42, "size": 10.0, "ts": 1779031111.0} in fills
    assert len(fills) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_fills.py -v -k extract_buy_fills`
Expected: FAIL（`ImportError: cannot import name 'extract_buy_fills'`）

- [ ] **Step 3: 实现**

在 `engine/fills.py` 末尾追加：

```python
def extract_buy_fills(trades: list[dict], funder: str, asset_id: str) -> list[dict]:
    """挑出我们在某 token 上的全部 BUY 成交,用于算加权成本。

    返回 [{price, size, ts}, ...](不去重、不依赖 seen_keys)。只看 maker_orders
    里 maker_address==funder 且 side==BUY 且 asset_id 匹配的条目;price 取该 maker
    条目的 price,size 取 matched_amount,ts 取 trade 顶层 match_time。
    """
    f = (funder or "").lower()
    a = str(asset_id)
    out = []
    for tr in trades:
        ts = float(tr.get("match_time", 0) or 0)
        for mo in tr.get("maker_orders", []) or []:
            if str(mo.get("maker_address", "")).lower() != f:
                continue
            if str(mo.get("side", "")).upper() != "BUY":
                continue
            if str(mo.get("asset_id", "")) != a:
                continue
            out.append(
                {
                    "price": float(mo.get("price", 0) or 0),
                    "size": float(mo.get("matched_amount", 0) or 0),
                    "ts": ts,
                }
            )
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_fills.py -v`
Expected: PASS（含原有 `select_new_buy_fills` 用例）

- [ ] **Step 5: 提交**

```bash
git add engine/fills.py tests/test_fills.py
git commit -m "feat: extract_buy_fills 纯函数(挑某 token 我们的买入成交)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `cost_basis_from_buy_fills` 纯函数

**Files:**
- Modify: `engine/take_profit.py`
- Test: `tests/test_take_profit.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_take_profit.py` 顶部 import 改为：

```python
from engine.take_profit import (
    ceil_to_tick,
    plan_take_profit,
    cost_basis_from_buy_fills,
    take_profit_price,
)
```

并追加新测试类：

```python
def _bf(price, size, ts):
    return {"price": price, "size": size, "ts": ts}


class TestCostBasisFromBuyFills:
    def test_single_buy_equals_buy_price(self):
        # 事故那单:单独一笔 0.28 全持有 -> 成本就是 0.28(不会再读成 0.21)
        assert cost_basis_from_buy_fills([_bf(0.28, 361, 100)], 361) == 0.28

    def test_multi_buy_weighted_average(self):
        # 200@0.20 + 200@0.28,size=400 -> (40+56)/400 = 0.24
        fills = [_bf(0.20, 200, 1), _bf(0.28, 200, 2)]
        assert cost_basis_from_buy_fills(fills, 400) == pytest.approx(0.24)

    def test_takes_newest_fills_to_cover_size(self):
        # 最近一笔 161@0.28(ts2),更早 200@0.20(ts1);size=161 -> 只取最新 -> 0.28
        fills = [_bf(0.20, 200, 1), _bf(0.28, 161, 2)]
        assert cost_basis_from_buy_fills(fills, 161) == pytest.approx(0.28)

    def test_partial_coverage_uses_available(self):
        # 买入总量(100)不足 size(361) -> 按已有的算(优雅降级)
        assert cost_basis_from_buy_fills([_bf(0.28, 100, 1)], 361) == 0.28

    def test_empty_fills_none(self):
        assert cost_basis_from_buy_fills([], 361) is None

    def test_zero_size_none(self):
        assert cost_basis_from_buy_fills([_bf(0.28, 361, 1)], 0) is None
```

`pytest` 已在 import 列表（文件顶部若无 `import pytest` 请补上）。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_take_profit.py -v -k CostBasis`
Expected: FAIL（`ImportError: cannot import name 'cost_basis_from_buy_fills'`）

- [ ] **Step 3: 实现**

在 `engine/take_profit.py` 末尾追加：

```python
def cost_basis_from_buy_fills(buy_fills: list[dict], size: float) -> float | None:
    """当前持仓的加权成本,来自我们的真实买入成交。

    按 ts 从新到旧累计取份额直至覆盖 size,返回这些份额的加权均价。size<=0 或无成交
    -> None;买入总量不足 size(部分份额非 maker 买入/历史截断)-> 按已有份额加权。
    单笔买入全持有时精确等于买入价;多笔时为真实加权(不退化成单笔价)。
    """
    if size <= 0 or not buy_fills:
        return None
    fills = sorted(buy_fills, key=lambda f: f.get("ts", 0) or 0, reverse=True)
    remaining = size
    cost_sum = 0.0
    qty_sum = 0.0
    for f in fills:
        if remaining <= 0:
            break
        fsize = float(f.get("size", 0) or 0)
        if fsize <= 0:
            continue
        take = min(fsize, remaining)
        cost_sum += float(f.get("price", 0) or 0) * take
        qty_sum += take
        remaining -= take
    if qty_sum <= 0:
        return None
    return cost_sum / qty_sum
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_take_profit.py -v -k CostBasis`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add engine/take_profit.py tests/test_take_profit.py
git commit -m "feat: cost_basis_from_buy_fills(按真实买入成交算加权成本)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `take_profit_price` 穿价护栏纯函数

**Files:**
- Modify: `engine/take_profit.py`
- Test: `tests/test_take_profit.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_take_profit.py` 追加：

```python
class TestTakeProfitPrice:
    def test_cost_above_bid_sells_at_cost(self):
        # 成本 0.45 > 买一 0.40 -> 挂成本价 0.45
        assert take_profit_price(0.45, 0.40, 0.01) == 0.45

    def test_cost_below_bid_lifts_to_bid_plus_tick(self):
        # 事故场景:成本 0.21 < 买一 0.27 -> 上移到 0.28,绝不穿价
        assert take_profit_price(0.21, 0.27, 0.01) == pytest.approx(0.28)

    def test_no_bid_falls_back_to_cost(self):
        assert take_profit_price(0.30, None, 0.01) == 0.30

    def test_off_tick_cost_ceiled(self):
        assert take_profit_price(0.3023, 0.10, 0.01) == 0.31
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_take_profit.py -v -k TakeProfitPrice`
Expected: FAIL（`ImportError: cannot import name 'take_profit_price'`）

- [ ] **Step 3: 实现**

在 `engine/take_profit.py` 末尾追加（`ceil_to_tick` 已在本文件定义）：

```python
def take_profit_price(cost: float, best_bid: float | None, tick: float) -> float:
    """止盈卖价 = max(ceil_to_tick(cost), best_bid + tick) 的穿价护栏。

    保证卖价严格高于买一 -> 永远是挂得住的 maker 单,绝不穿价市价清仓。best_bid 为
    None(盘口某侧缺失)时退回 ceil_to_tick(cost)(盘口空时本就无买盘可穿)。
    """
    base = ceil_to_tick(cost, tick)
    if best_bid is not None:
        base = max(base, round(best_bid + tick, 10))
    return base
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_take_profit.py -v -k TakeProfitPrice`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add engine/take_profit.py tests/test_take_profit.py
git commit -m "feat: take_profit_price 穿价护栏(卖价不低于买一+1tick)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `monitor.py` 新增 `_cost()` + per-tick 缓存 + `_sell_book`

**Files:**
- Modify: `engine/monitor.py`（imports；`__init__`；`begin_status_tick`；新增 `_cost`；`_sell_tick`→`_sell_book`）
- Test: `tests/test_monitor.py`

本任务只新增/重构辅助方法，不改止盈/止损行为（`_reconcile_take_profit` 暂仍调用 `_sell_book` 的前两个返回值，等价于旧 `_sell_tick`）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_monitor.py` 追加（顶部已有 `_make_monitor`）：

```python
class TestCostHelper:
    def _buy_trade(self, asset, price, size, ts="100"):
        return {
            "id": f"t-{asset}-{ts}",
            "market": "mkt1",
            "match_time": ts,
            "maker_orders": [{
                "order_id": f"o-{asset}-{ts}", "maker_address": "0xFUNDER",
                "side": "BUY", "matched_amount": str(size), "price": str(price),
                "asset_id": asset,
            }],
        }

    def test_cost_from_get_trades_weighted(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = [
            self._buy_trade("tok1", 0.20, 200, ts="1"),
            self._buy_trade("tok1", 0.28, 200, ts="2"),
        ]
        monitor.begin_status_tick()
        assert monitor._cost("tok1", 400) == pytest.approx(0.24)

    def test_cost_cached_within_tick(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = [self._buy_trade("tok1", 0.28, 361)]
        monitor.begin_status_tick()
        monitor._cost("tok1", 361)
        monitor._cost("tok1", 361)
        assert api.get_trades.call_count == 1  # 同 tick 只取一次

    def test_cost_none_when_get_trades_fails(self):
        monitor, api, db = _make_monitor()
        api.get_trades.side_effect = Exception("boom")
        monitor.begin_status_tick()
        assert monitor._cost("tok1", 361) is None

    def test_cost_none_when_no_buy_fills(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        monitor.begin_status_tick()
        assert monitor._cost("tok1", 361) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_monitor.py -v -k TestCostHelper`
Expected: FAIL（`AttributeError: ... has no attribute '_cost'`）

- [ ] **Step 3: 实现**

`engine/monitor.py` 顶部 import（第 6-7 行）改为：

```python
from engine.fills import select_new_buy_fills, extract_buy_fills
from engine.take_profit import (
    plan_take_profit,
    cost_basis_from_buy_fills,
    take_profit_price,
)
```

`__init__` 里在 `self._tick_ts: float = 0.0` 之后加一行：

```python
        self._cost_cache: dict = {}  # asset_id -> 加权成本 or None(每 tick 重置)
```

`begin_status_tick` 改为：

```python
    def begin_status_tick(self) -> None:
        self._status_rows = []
        self._tick_ts = time.time()
        self._cost_cache = {}
```

新增 `_cost` 方法（放在 `_funder` 之后）：

```python
    def _cost(self, asset_id: str, size: float):
        """该持仓的加权成本(本 tick 缓存)。来自 CLOB get_trades 的真实买入成交,
        替代 Data API avgPrice。取不到 -> None(调用方据此跳过,不在不确定成本上动手)。"""
        if asset_id in self._cost_cache:
            return self._cost_cache[asset_id]
        funder = self._funder()
        try:
            trades = self.api.get_trades(
                TradeParams(maker_address=funder, asset_id=asset_id)
            )
        except Exception as e:
            logger.warning("get_trades(asset=%s) for cost failed: %s", asset_id, e)
            self._cost_cache[asset_id] = None
            return None
        fills = extract_buy_fills(trades, funder, asset_id)
        cost = cost_basis_from_buy_fills(fills, size)
        self._cost_cache[asset_id] = cost
        return cost
```

把现有 `_sell_tick`（约 188-198 行）整体替换为 `_sell_book`：

```python
    def _sell_book(self, asset_id: str):
        """(tick_float, tick_str, best_bid) for an asset;
        失败/盘口空时回 (0.01, "0.01", None)。"""
        try:
            ob = self.api.get_orderbook(asset_id)
            tick_str = ob.get("tick_size", "0.01")
            bids = sorted(
                ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True
            )
            best_bid = float(bids[0]["price"]) if bids else None
            return float(tick_str), tick_str, best_bid
        except Exception as e:
            logger.warning(
                "orderbook for %s failed (take-profit tick=0.01): %s", asset_id, e
            )
            return 0.01, "0.01", None
```

把 `_reconcile_take_profit` 里原来的：

```python
        tick, tick_str = self._sell_tick(asset_id)
```

临时改成（保持本任务行为不变，下个任务再改用 cost/护栏）：

```python
        tick, tick_str, _best_bid = self._sell_book(asset_id)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_monitor.py -v`
Expected: PASS（`TestCostHelper` 通过；`TestCheckTakeProfit`/`TestStopLoss` 仍按旧 avgPrice 行为通过——它们尚未调用 `_cost`，`_sell_book` 前两个返回值与旧 `_sell_tick` 等价）

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat: monitor._cost() 按 get_trades 算加权成本(per-tick缓存)+_sell_book

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 止盈改用加权成本 + 穿价护栏（含 `plan_take_profit` 改签名）

**Files:**
- Modify: `engine/take_profit.py`（`plan_take_profit` 签名 `avg`→`want_price`，去掉内部 ceil）
- Modify: `engine/monitor.py`（`_reconcile_take_profit`）
- Test: `tests/test_take_profit.py`（`TestPlanTakeProfit`）、`tests/test_monitor.py`（`TestCheckTakeProfit`）

- [ ] **Step 1: 改 `plan_take_profit` 测试为传 want_price**

把 `tests/test_take_profit.py` 的 `TestPlanTakeProfit` 里所有 `plan_take_profit(..., avg=X, ...)` 改成 `want_price=X`，并去掉"会被 ceil"的期望（调用方已传最终价）。完整替换该类为：

```python
class TestPlanTakeProfit:
    def test_no_position_is_noop(self):
        plan = plan_take_profit(size=0.0, want_price=0.30, tick=0.01, existing_sells=[])
        assert plan["action"] == "noop"

    def test_zero_price_is_noop(self):
        plan = plan_take_profit(size=222.0, want_price=0.0, tick=0.01, existing_sells=[])
        assert plan["action"] == "noop"

    def test_no_existing_sell_places_one(self):
        plan = plan_take_profit(size=222.08, want_price=0.30, tick=0.01, existing_sells=[])
        assert plan["action"] == "replace"
        assert plan["price"] == 0.30
        assert plan["size"] == 222.08
        assert plan["cancel_ids"] == []

    def test_correct_single_sell_is_kept(self):
        sells = [_sell("s1", 0.30, 222.08)]
        plan = plan_take_profit(size=222.08, want_price=0.30, tick=0.01, existing_sells=sells)
        assert plan["action"] == "keep"
        assert plan["cancel_ids"] == []

    def test_phantom_high_price_sell_is_replaced(self):
        sells = [_sell("phantom", 0.38, 200.0)]
        plan = plan_take_profit(size=222.08, want_price=0.30, tick=0.01, existing_sells=sells)
        assert plan["action"] == "replace"
        assert plan["price"] == 0.30
        assert plan["cancel_ids"] == ["phantom"]

    def test_split_orders_collapse_to_one(self):
        sells = [
            _sell("a", 0.38, 177.77), _sell("b", 0.38, 22.23),
            _sell("c", 0.30, 25.0), _sell("d", 0.30, 22.857),
            _sell("e", 0.30, 8.171), _sell("f", 0.30, 7.471),
            _sell("g", 0.30, 7.28), _sell("h", 0.30, 7.128),
        ]
        plan = plan_take_profit(size=222.08, want_price=0.30, tick=0.01, existing_sells=sells)
        assert plan["action"] == "replace"
        assert plan["price"] == 0.30
        assert set(plan["cancel_ids"]) == {"a", "b", "c", "d", "e", "f", "g", "h"}

    def test_single_sell_wrong_size_is_replaced(self):
        sells = [_sell("s1", 0.30, 100.0)]
        plan = plan_take_profit(size=222.08, want_price=0.30, tick=0.01, existing_sells=sells)
        assert plan["action"] == "replace"
        assert plan["cancel_ids"] == ["s1"]

    def test_single_sell_within_size_tolerance_is_kept(self):
        sells = [_sell("s1", 0.30, 222.08, matched=0.5)]
        plan = plan_take_profit(size=222.08, want_price=0.30, tick=0.01, existing_sells=sells)
        assert plan["action"] == "keep"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_take_profit.py -v -k PlanTakeProfit`
Expected: FAIL（`TypeError: plan_take_profit() got an unexpected keyword argument 'want_price'`）

- [ ] **Step 3: 改 `plan_take_profit` 实现**

`engine/take_profit.py` 把 `plan_take_profit` 整体替换为：

```python
def plan_take_profit(
    size: float, want_price: float, tick: float, existing_sells: list[dict]
) -> dict:
    """对一个持仓的 SELL 们做对账,使恰好一笔卖单挂在 want_price、覆盖整个 size。

    want_price 由调用方用 take_profit_price(cost, best_bid, tick) 预先算好(已对齐
    tick、已含穿价护栏),本函数不再加工价格。返回 {"action","price","size",
    "cancel_ids"}:noop / keep / replace。
    """
    if size <= 0 or want_price is None or want_price <= 0 or tick <= 0:
        return {"action": "noop", "price": None, "size": 0.0, "cancel_ids": []}
    want = want_price
    ids = [o.get("id") for o in existing_sells]
    remaining = sum(_remaining(o) for o in existing_sells)
    if (
        len(existing_sells) == 1
        and _price_matches(float(existing_sells[0].get("price", 0) or 0), want, tick)
        and _size_matches(remaining, size)
    ):
        return {"action": "keep", "price": want, "size": size, "cancel_ids": []}
    return {"action": "replace", "price": want, "size": size, "cancel_ids": ids}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_take_profit.py -v`
Expected: PASS

- [ ] **Step 5: 改 `TestCheckTakeProfit` 测试**

`tests/test_monitor.py` 的 `TestCheckTakeProfit`：每个用例补 `api.get_trades.return_value`（喂买入成交，使成本=0.30），把 `price_basis` 断言从 `"avgPrice"` 改为 `"get_trades"`，并新增浮盈上移、成本缺失两个用例。在类内加一个买入成交辅助方法并改造用例：

```python
    def _buy(self, price=0.30, size=222.08, asset="tok1", ts="100"):
        return {
            "id": f"t-{ts}", "market": "mkt1", "match_time": ts,
            "maker_orders": [{
                "order_id": f"o-{ts}", "maker_address": "0xFUNDER", "side": "BUY",
                "matched_amount": str(size), "price": str(price), "asset_id": asset,
            }],
        }
```

逐个用例改动：

- `test_places_one_sell_at_cost_when_none_exist`：在 `monitor.check_take_profit()` 前加 `api.get_trades.return_value = [self._buy()]`；末尾断言改为 `assert "get_trades" in tp.kwargs["price_basis"]`（删掉 `"avgPrice"` 断言）。其余（place 0.30、`take_profit_sell`）不变（orderbook 无 bids → best_bid None → want=0.30）。
- `test_keeps_correct_single_sell`：加 `api.get_trades.return_value = [self._buy()]`。其余不变。
- `test_replaces_phantom_and_split_sells_with_one`：加 `api.get_trades.return_value = [self._buy()]`。其余不变。
- `test_positions_api_failure_skips`、`test_open_orders_failure_skips`：不变（在取成本前就 return）。
- `test_zero_size_position_skipped`：不变（size<=0 先 return）。
- `test_orderbook_failure_falls_back_to_default_tick`：加 `api.get_trades.return_value = [self._buy()]`。其余不变（成本 0.30、盘口失败 → best_bid None → 0.30）。
- `test_ignores_other_assets_and_buy_orders`：加 `api.get_trades.return_value = [self._buy(asset="tok1")]`。其余不变。

新增两个用例：

```python
    def test_lifts_sell_above_bid_when_in_profit(self):
        # 事故场景:成本 0.21 < 买一 0.27 -> 卖价上移到 0.28,绝不穿价市价清仓
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(size=361.0)]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = [self._buy(price=0.21, size=361.0)]
        api.get_orderbook.return_value = {
            "tick_size": "0.01", "bids": [{"price": "0.27", "size": "9999"}],
            "asks": [{"price": "0.29", "size": "9999"}],
        }

        monitor.check_take_profit()

        api.place_limit_sell.assert_called_once_with(
            "tok1", pytest.approx(0.28), 361.0, tick_size="0.01"
        )

    def test_skips_when_cost_unavailable(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos()]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = []  # 无买入成交 -> 成本 None
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        api.place_limit_sell.assert_not_called()
        api.cancel_orders.assert_not_called()
```

- [ ] **Step 6: 跑测试确认失败**

Run: `pytest tests/test_monitor.py -v -k TestCheckTakeProfit`
Expected: FAIL（新用例失败 + 旧 price_basis 仍是 "avgPrice"；`_reconcile_take_profit` 尚未改用 cost/护栏）

- [ ] **Step 7: 改 `_reconcile_take_profit` 实现**

`engine/monitor.py` 把 `_reconcile_take_profit` 整体替换为：

```python
    def _reconcile_take_profit(self, pos: dict, open_orders: list):
        asset_id = pos.get("asset", "")
        size = float(pos.get("size", 0) or 0)
        cid = pos.get("conditionId", "")
        if size <= 0:
            return
        cost = self._cost(asset_id, size)
        if cost is None or cost <= 0:
            self._status_add(
                market=cid, side="卖出", price="-", size=str(size), matched="-",
                stage="止盈卖单", action="跳过(无成交数据)",
                detail="get_trades 无买入成交，保持现有卖单不动",
            )
            return
        tick, tick_str, best_bid = self._sell_book(asset_id)
        want = take_profit_price(cost, best_bid, tick)
        sells = [
            o
            for o in open_orders
            if o.get("asset_id") == asset_id and o.get("side") == "SELL"
        ]
        plan = plan_take_profit(size, want, tick, sells)
        if plan["action"] in ("noop", "keep"):
            self._status_add(
                market=cid, side="卖出", price=f"{want:.4f}", size=str(size),
                matched="-", stage="止盈卖单", action="保持(成本/护栏价)",
                detail=f"成本{cost:.4f} 持仓{size} 已挂一笔{want:.4f}",
            )
            return
        if plan["cancel_ids"]:
            try:
                self.api.cancel_orders(plan["cancel_ids"])
                self._record_action(
                    market_id=cid, action_type="take_profit_recancel", side="-",
                    price=-1, size=size,
                    reason="撤销与持仓不符的旧止盈卖单（价格/数量不符或被拆成多笔），改为按持仓挂单一笔",
                    price_basis=(
                        f"撤 {len(plan['cancel_ids'])} 笔 SELL；"
                        f"来源：CLOB get_open_orders（asset={asset_id} 的 SELL）"
                    ),
                )
            except Exception as e:
                logger.warning("Cancel stale sells for %s failed: %s", asset_id, e)
                return
        try:
            self.api.place_limit_sell(asset_id, want, size, tick_size=tick_str)
        except Exception as e:
            logger.warning("Place take-profit sell for %s failed: %s", asset_id, e)
            return
        self._record_action(
            market_id=cid, action_type="take_profit_sell", side="卖出",
            price=want, size=size,
            reason="按真实成交加权成本挂止盈卖单，并加穿价护栏（不亏本金、不穿价市价清仓、赚流动性奖励）",
            price_basis=(
                f"成本=get_trades加权 {cost:.4f}；卖价=max(成本,买一+1tick)={want:.4f}；"
                f"来源：CLOB get_trades + get_orderbook"
            ),
        )
        self._status_add(
            market=cid, side="卖出", price=f"{want:.4f}", size=str(size),
            matched="-", stage="止盈卖单", action="按成本挂单",
            detail=f"成本{cost:.4f} 持仓{size} 挂卖{want:.4f}",
        )
```

- [ ] **Step 8: 跑测试确认通过**

Run: `pytest tests/test_take_profit.py tests/test_monitor.py -v`
Expected: PASS

- [ ] **Step 9: 提交**

```bash
git add engine/take_profit.py engine/monitor.py tests/test_take_profit.py tests/test_monitor.py
git commit -m "feat: 止盈改用 get_trades 加权成本 + 穿价护栏(plan_take_profit 收 want_price)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 止损改用加权成本

**Files:**
- Modify: `engine/monitor.py`（`_check_pos_sl`）
- Test: `tests/test_monitor.py`（`TestStopLoss`）

- [ ] **Step 1: 改/加测试**

`tests/test_monitor.py` 的 `TestStopLoss`：给会进入触发判定的用例补 `api.get_trades.return_value`（喂买入成交使成本非 None），并新增"成本缺失则不平仓"用例。

- `test_triggers_stop_loss_when_price_drops`：在 `with patch(...)` 之前加
  `api.get_trades.return_value = [{"id":"t","market":"mkt1","match_time":"1","maker_orders":[{"order_id":"o","maker_address":"0xFUNDER","side":"BUY","matched_amount":"1000","price":"0.30","asset_id":"tok1"}]}]`。其余不变。
- `test_no_stop_loss_when_price_stable`：同样补上 `api.get_trades.return_value`（同上）。其余不变。
- `test_positions_api_failure_skips_stop_loss`、`test_zero_size_position_skipped`：不变。

新增：

```python
    def test_skips_stop_loss_when_cost_unavailable(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        api.get_user_positions.return_value = [
            {"asset": "tok1", "size": 1000.0, "avgPrice": 0.30,
             "curPrice": 0.10, "conditionId": "mkt1"}
        ]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = []  # 成本 None -> 不在不确定成本上市价平仓

        monitor.check_stop_loss()

        api.place_market_sell.assert_not_called()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_monitor.py -v -k TestStopLoss`
Expected: FAIL（`test_skips_stop_loss_when_cost_unavailable` 失败——当前止损读 `avgPrice`，会平仓）

- [ ] **Step 3: 改 `_check_pos_sl` 实现**

`engine/monitor.py` 把 `_check_pos_sl` 中下面这段：

```python
        asset_id = pos.get("asset", "")
        size = float(pos.get("size", 0) or 0)
        avg = float(pos.get("avgPrice", 0) or 0)
        cur = float(pos.get("curPrice", 0) or 0)
        if size <= 0:
            return
        if not stop_loss_triggered(cur, avg, settings["stop_loss_pct"]):
            return
```

替换为：

```python
        asset_id = pos.get("asset", "")
        size = float(pos.get("size", 0) or 0)
        cur = float(pos.get("curPrice", 0) or 0)
        if size <= 0:
            return
        avg = self._cost(asset_id, size)  # 真实成交加权成本,替代 Data API avgPrice
        if avg is None or avg <= 0:
            return
        if not stop_loss_triggered(cur, avg, settings["stop_loss_pct"]):
            return
```

（`_check_pos_sl` 后续 `avg` 的用法——pnl `(cur - avg) * size`、`price_basis` 里的成本——保持不变，现在 `avg` 即加权成本。）

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_monitor.py -v -k TestStopLoss`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat: 止损改用 get_trades 加权成本,成本取不到则不平仓

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: 全量回归 + 收尾

**Files:** 无（仅验证）

- [ ] **Step 1: 跑全套测试**

Run: `pytest`
Expected: 全绿。若有遗漏的旧用例因 `_cost` 现在调用 `api.get_trades`（MagicMock 默认返回不可迭代）而报错，按 Task 5/6 的做法给该用例补 `api.get_trades.return_value`。

- [ ] **Step 2: 确认无残留 `_sell_tick` / `pos.avgPrice` 引用**

Run: `grep -rn "_sell_tick\|avgPrice" engine/`
Expected: `engine/monitor.py` 中不再有 `_sell_tick`；`avgPrice` 仅可能出现在注释/Data API 字段读取处（止盈/止损的成本来源已是 `_cost`）。如有逻辑残留，修正并补测。

- [ ] **Step 3: 最终提交（如 Step 1/2 有改动）**

```bash
git add -A engine/ tests/
git commit -m "test: 全量回归,补齐 get_trades mock

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## 自查（写完计划后对照 spec）

- **spec 覆盖**：`extract_buy_fills`(T1)、`cost_basis_from_buy_fills`(T2)、`take_profit_price`(T3)、`_cost`+缓存+`_sell_book`(T4)、止盈接入+`plan_take_profit`改签名(T5)、止损接入(T6)、回归(T7)。错误兜底散落在 T4(`_cost` None)、T5(止盈 None 跳过)、T6(止损 None 跳过)。✓
- **占位符**：无 TBD/TODO，每个改码步骤均有完整代码。✓
- **类型/签名一致**：`plan_take_profit(size, want_price, tick, existing_sells)` 在 T5 实现与调用一致；`_sell_book` 返回三元组在 T4 引入、T5 使用一致；`_cost(asset_id, size)` 在 T4 定义、T5/T6 调用一致。✓
- **守住既有行为**：每持仓恰好一笔卖单、止盈在 Step1 后、Step3 跳过 SELL、停引擎只撤买单——均未改动。✓
