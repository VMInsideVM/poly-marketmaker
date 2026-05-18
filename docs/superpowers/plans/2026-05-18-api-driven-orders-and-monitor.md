# API-Driven Order Management + Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make order management and the monitor use Polymarket API real-time state as the single source of truth; DB keeps only `trades` history + `cooldown`.

**Architecture:** `OrderMonitor` runs three steps per wallet per tick — Step 1 detects fills via CLOB `get_trades` (id-dedup + timestamp watermark) and places take-profit sells + cancels the filled buy order's remainder; Step 2 stop-losses using Data API position `avgPrice`; Step 3 re-runs `determine_order_price` on resting buy orders and re-places/cancels on tick mismatch. Order pages read live from `get_open_orders` + `are_orders_scoring`. Core decisions are pure functions (testable without network) wrapped by a thin IO layer.

**Tech Stack:** Python 3.12, Flask, `py_clob_client_v2` (1.0.1), Polymarket Data API (`https://data-api.polymarket.com`), SQLite, pytest.

**Reference spec:** `docs/superpowers/specs/2026-05-18-api-driven-orders-and-monitor-design.md`

---

## File Structure

- `api/polymarket_api.py` — add `are_orders_scoring`, retype `get_trades` to accept `TradeParams`, add `get_user_positions` (Data API), add `extract_maker_order_id` helper.
- `engine/fills.py` (NEW) — pure functions: `select_new_trades`, `is_buy_side_trade`. No IO.
- `engine/strategy_check.py` (NEW) — pure functions: `needs_replace`, `recompute_price`. No IO.
- `engine/risk.py` (NEW) — pure function: `stop_loss_triggered`. No IO.
- `engine/monitor.py` — rewrite `OrderMonitor` to call the pure functions + IO; remove `_last_matched`, `positions`/`orders` table usage.
- `engine/manager.py` — rewrite `startup_recovery` (trade watermark init), `_cancel_buy_orders` (use `cancel_orders`), `worker.place_orders` (recompute price before placing), drop `check_existing_orders`.
- `web/routes.py` — `/api/orders`, `/api/orders/cancel-batch`, `/api/orders/cancel-all-buys` (new), `/api/positions`, `/api/dashboard`, `/api/engine/cancel-all`, `/api/orders/<id>/cancel` all API-driven.
- `web/templates/orders.html` — columns (scoring), filters (side/scoring), client-side sort, new buttons, 10s poll + refresh button, error banner.
- `config.py` — no new keys needed (confirmed; strict tick comparison has no tolerance knob).
- `tests/test_fills.py`, `tests/test_strategy_check.py`, `tests/test_risk.py` (NEW) — pure-logic unit tests.
- `scripts/spike_api_shapes.py` (NEW, throwaway) — prints real trade / Data-API-positions / scoring JSON shapes.
- `CLAUDE.md` — sync the "Critical behaviors" / "Architecture" wording.

---

## Phase 0 — Discover real API shapes (blocking, no guessing)

### Task 0: Spike script to capture real JSON shapes

The trade→order linkage field, Data API positions field names, and `are_orders_scoring` return shape are not documented in-repo. Capture them from the live API before coding against them.

**Files:**
- Create: `scripts/spike_api_shapes.py`

- [ ] **Step 1: Write the spike script**

```python
"""scripts/spike_api_shapes.py — throwaway: print real API JSON shapes. Not a pytest test."""
import json, hashlib, requests
from models.database import Database
from utils.crypto import decrypt, derive_key
from config import DB_PATH
from api.polymarket_api import PolymarketAPI
from py_clob_client_v2.clob_types import TradeParams, OrdersScoringParams


def main():
    db = Database(DB_PATH); db.init()
    pw_hash, salt = db.get_password()
    key = derive_key(input("访问密码: "), salt)
    assert hashlib.sha256(key).hexdigest() == pw_hash, "密码错误"
    w = db.list_wallets()[0]
    api = PolymarketAPI(decrypt(w["encrypted_key"], key), funder=w.get("funder") or None)
    addr = api.get_address()
    funder = w.get("funder") or addr

    print("\n=== get_open_orders[0] ===")
    oo = api.client.get_open_orders()
    print(json.dumps(oo[:1], indent=2, default=str))

    print("\n=== get_trades (maker_address) [0..1] ===")
    tr = api.client.get_trades(TradeParams(maker_address=funder))
    print(json.dumps(tr[:2], indent=2, default=str))

    print("\n=== are_orders_scoring ===")
    ids = [o["id"] for o in oo if o.get("side") == "BUY"][:5]
    if ids:
        print(json.dumps(api.client.are_orders_scoring(OrdersScoringParams(orderIds=ids)), indent=2, default=str))
    else:
        print("no open buy orders to test scoring")

    print("\n=== Data API positions ===")
    for u in {addr, funder}:
        r = requests.get("https://data-api.polymarket.com/positions",
                          params={"user": u}, timeout=15)
        print(f"user={u} status={r.status_code}")
        print(json.dumps(r.json()[:2] if r.ok else r.text, indent=2, default=str))
    db.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it and record findings**

Run: `python scripts/spike_api_shapes.py`
Record in the PR/commit message the exact keys for:
1. Trade object: the field holding the **maker buy order id** (expected `maker_orders` list with `order_id`, or `taker_order_id`), the size field (`size`/`matched_amount`), `price`, `asset_id`, `market`, the trade `id`, the timestamp field used for `after` (epoch seconds vs ms), and how `side` is expressed for a maker buy.
2. Data API positions: which `user` (wallet vs funder) returns data, and the exact keys for size / average cost / current price (expected `size`, `avgPrice`, `curPrice`, `asset` / `asset_id`).
3. `are_orders_scoring` return: dict keyed by order id → bool (confirm).

- [ ] **Step 3: Commit the spike + findings**

```bash
git add scripts/spike_api_shapes.py
git commit -m "chore: spike script capturing live API JSON shapes"
```

> **All later tasks that say `<TRADE_ORDER_ID_FIELD>`, `<TRADE_SIZE_FIELD>`, `<TRADE_TS_FIELD>`, `<POS_USER>`, `<POS_AVG_FIELD>`, `<POS_SIZE_FIELD>`, `<POS_CUR_FIELD>`, `<POS_ASSET_FIELD>` MUST be replaced with the exact strings recorded here before that task is implemented.** If the spike shows a shape materially different from the spec assumptions, stop and report before continuing.

---

## Phase 1 — Pure logic (TDD, no network)

### Task 1: `select_new_trades` — fill detection & dedup

**Files:**
- Create: `engine/fills.py`
- Test: `tests/test_fills.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fills.py
from engine.fills import select_new_trades


def _t(tid, side="BUY", size="10", ts=100):
    return {"id": tid, "side": side, "size": size, "trader_side": "MAKER",
            "price": "0.5", "asset_id": "A", "market": "M",
            "match_time": ts}


def test_returns_unseen_buy_trades_only():
    trades = [_t("t1"), _t("t2"), _t("t3", side="SELL")]
    seen = {"t1"}
    new = select_new_trades(trades, seen, ts_field="match_time")
    assert [t["id"] for t in new] == ["t2"]


def test_empty_when_all_seen():
    trades = [_t("t1"), _t("t2")]
    assert select_new_trades(trades, {"t1", "t2"}, ts_field="match_time") == []


def test_sorted_by_timestamp_ascending():
    trades = [_t("t2", ts=200), _t("t1", ts=100)]
    new = select_new_trades(trades, set(), ts_field="match_time")
    assert [t["id"] for t in new] == ["t1", "t2"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fills.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.fills'`

- [ ] **Step 3: Write minimal implementation**

```python
# engine/fills.py
"""Pure fill-detection logic (no network/IO)."""


def is_buy_side_trade(trade: dict) -> bool:
    """True if this trade represents a fill of one of our maker BUY orders.

    `side` reflects the trade's taker side per Polymarket; for our resting
    maker BUY the relevant fills are BUY-side. Field names verified in Task 0;
    keep this the single place that encodes the rule.
    """
    return str(trade.get("side", "")).upper() == "BUY"


def select_new_trades(trades: list[dict], seen_ids: set, ts_field: str) -> list[dict]:
    """Return unseen buy-side trades, sorted oldest-first by ts_field."""
    new = [
        t for t in trades
        if t.get("id") not in seen_ids and is_buy_side_trade(t)
    ]
    new.sort(key=lambda t: float(t.get(ts_field, 0) or 0))
    return new
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_fills.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add engine/fills.py tests/test_fills.py
git commit -m "feat: pure fill-detection (select_new_trades) with unit tests"
```

### Task 2: `needs_replace` — strict tick mismatch decision

**Files:**
- Create: `engine/strategy_check.py`
- Test: `tests/test_strategy_check.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_strategy_check.py
from engine.strategy_check import needs_replace


def test_none_want_means_cancel_no_replace():
    # want is None -> cancel, do not replace
    assert needs_replace(current_price=0.42, want_price=None, tick=0.01) == "cancel"


def test_same_tick_no_action():
    assert needs_replace(current_price=0.42, want_price=0.4203, tick=0.01) == "keep"


def test_different_tick_replace():
    assert needs_replace(current_price=0.42, want_price=0.44, tick=0.01) == "replace"


def test_rounding_at_tick_boundary():
    # 0.425 and 0.42 differ by less than a tick but land on different ticks
    assert needs_replace(current_price=0.42, want_price=0.43, tick=0.01) == "replace"
    assert needs_replace(current_price=0.420, want_price=0.4249, tick=0.01) == "keep"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_strategy_check.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.strategy_check'`

- [ ] **Step 3: Write minimal implementation**

```python
# engine/strategy_check.py
"""Pure strategy-compliance decision (no network/IO)."""


def _tick_index(price: float, tick: float) -> int:
    return round(float(price) / float(tick))


def needs_replace(current_price: float, want_price, tick: float) -> str:
    """Decide action for a resting buy order.

    Returns:
      "cancel"  -> want_price is None: no compliant price exists, cancel only
      "replace" -> recomputed price is on a different tick than current
      "keep"    -> same tick, leave the order alone
    """
    if want_price is None:
        return "cancel"
    if _tick_index(current_price, tick) == _tick_index(want_price, tick):
        return "keep"
    return "replace"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_strategy_check.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add engine/strategy_check.py tests/test_strategy_check.py
git commit -m "feat: pure strategy-compliance decision (needs_replace) with tests"
```

### Task 3: `stop_loss_triggered` — risk decision

**Files:**
- Create: `engine/risk.py`
- Test: `tests/test_risk.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_risk.py
from engine.risk import stop_loss_triggered


def test_triggers_below_threshold():
    # avg 0.50, 15% stop -> threshold 0.425
    assert stop_loss_triggered(cur_price=0.42, avg_price=0.50, stop_loss_pct=15.0) is True


def test_no_trigger_at_or_above_threshold():
    assert stop_loss_triggered(cur_price=0.425, avg_price=0.50, stop_loss_pct=15.0) is False
    assert stop_loss_triggered(cur_price=0.60, avg_price=0.50, stop_loss_pct=15.0) is False


def test_zero_or_missing_prices_never_trigger():
    assert stop_loss_triggered(cur_price=0.0, avg_price=0.50, stop_loss_pct=15.0) is False
    assert stop_loss_triggered(cur_price=0.42, avg_price=0.0, stop_loss_pct=15.0) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_risk.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.risk'`

- [ ] **Step 3: Write minimal implementation**

```python
# engine/risk.py
"""Pure stop-loss decision (no network/IO)."""


def stop_loss_triggered(cur_price: float, avg_price: float, stop_loss_pct: float) -> bool:
    """True when current price has fallen to/below the stop threshold.

    cur_price==0 (no quote) or avg_price==0 (no cost basis) never triggers —
    we do not market-sell on missing data.
    """
    cur = float(cur_price or 0)
    avg = float(avg_price or 0)
    if cur <= 0 or avg <= 0:
        return False
    threshold = avg * (1 - float(stop_loss_pct) / 100.0)
    return cur <= threshold
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_risk.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add engine/risk.py tests/test_risk.py
git commit -m "feat: pure stop-loss decision (stop_loss_triggered) with tests"
```

---

## Phase 2 — API wrapper layer

### Task 4: Add `are_orders_scoring`, `TradeParams` get_trades, `get_user_positions`, `extract_maker_order_id`

**Files:**
- Modify: `api/polymarket_api.py` (imports near line 7-15; methods near `get_trades` line ~211 and `get_open_orders`)

- [ ] **Step 1: Add SDK imports**

In `api/polymarket_api.py`, extend the existing import block:

```python
from py_clob_client_v2.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
    OrderPayload,
    MarketOrderArgsV2,
    PartialCreateOrderOptions,
    TradeParams,
    OrdersScoringParams,
)
```

- [ ] **Step 2: Add a Data API host constant**

Below `REWARDS_API = POLYMARKET_HOST` add:

```python
DATA_API_HOST = "https://data-api.polymarket.com"
```

- [ ] **Step 3: Replace `get_trades` and add new methods**

Replace the existing `get_trades` method:

```python
    def get_trades(self, params: TradeParams = None) -> list:
        """Get trade history for this wallet (auto-paginated)."""
        return self.client.get_trades(params)

    def are_orders_scoring(self, order_ids: list) -> dict:
        """Batch-query whether orders are scoring (in reward band).

        Returns dict keyed by order id -> bool. Empty dict for empty input.
        """
        if not order_ids:
            return {}
        return self.client.are_orders_scoring(
            OrdersScoringParams(orderIds=order_ids)
        )

    def get_user_positions(self, user_address: str) -> list:
        """Polymarket Data API: current positions for a user.

        Returns list of dicts. Field names confirmed in Task 0 spike;
        callers use the <POS_*> fields recorded there.
        """
        resp = requests.get(
            f"{DATA_API_HOST}/positions",
            params={"user": user_address},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    @staticmethod
    def extract_maker_order_id(trade: dict) -> str | None:
        """Resolve the resting maker BUY order id from a trade object.

        Field path confirmed in Task 0. Replace the body below with the
        exact structure recorded (example assumes maker_orders list):
        """
        makers = trade.get("<TRADE_ORDER_ID_FIELD>") or []
        if isinstance(makers, list) and makers:
            first = makers[0]
            return first.get("order_id") if isinstance(first, dict) else first
        if isinstance(makers, str):
            return makers
        return None
```

- [ ] **Step 4: Smoke-check import**

Run: `python -c "from api.polymarket_api import PolymarketAPI; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add api/polymarket_api.py
git commit -m "feat: API wrapper for scoring, TradeParams trades, Data-API positions"
```

---

## Phase 3 — Monitor rewrite

### Task 5: Rewrite `OrderMonitor` (Steps 1-3, API-driven)

**Files:**
- Modify: `engine/monitor.py` (full rewrite of the class)

- [ ] **Step 1: Replace `engine/monitor.py` entirely**

```python
"""engine/monitor.py — API-driven fill detection, stop-loss, strategy compliance."""

import logging
from py_clob_client_v2.clob_types import TradeParams
from engine.fills import select_new_trades
from engine.strategy_check import needs_replace
from engine.risk import stop_loss_triggered
from engine.strategy import determine_order_price

logger = logging.getLogger(__name__)

# Field names confirmed by Task 0 spike — set these to the recorded strings:
TRADE_TS_FIELD = "<TRADE_TS_FIELD>"
TRADE_SIZE_FIELD = "<TRADE_SIZE_FIELD>"
POS_USER_IS_FUNDER = True  # set per Task 0 (which `user` returned data)
POS_AVG_FIELD = "<POS_AVG_FIELD>"
POS_SIZE_FIELD = "<POS_SIZE_FIELD>"
POS_CUR_FIELD = "<POS_CUR_FIELD>"
POS_ASSET_FIELD = "<POS_ASSET_FIELD>"


class OrderMonitor:
    def __init__(self, api, db, wallet_address: str):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address
        self._seen_trade_ids: set[str] = set()
        # Watermark: only act on trades at/after this epoch ts.
        self._after_ts: float = 0.0

    def init_watermark(self):
        """Set watermark to latest recorded trade ts for this wallet (offline catch-up)."""
        rows = self.db.get_trade_history(self.wallet_address)
        self._after_ts = max((r.get("created_at", 0) or 0) for r in rows) if rows else 0.0

    def _funder(self) -> str:
        return self.api.client.funder if POS_USER_IS_FUNDER else self.api.get_address()

    # --- Step 1: fills via get_trades ---
    def check_buy_orders(self):
        try:
            params = TradeParams(maker_address=self._funder(),
                                  after=str(int(self._after_ts)) or None)
            trades = self.api.get_trades(params)
        except Exception as e:
            logger.error("get_trades failed for %s: %s", self.wallet_address, e)
            return
        new_trades = select_new_trades(trades, self._seen_trade_ids, TRADE_TS_FIELD)
        cancelled_orders: set[str] = set()
        for tr in new_trades:
            try:
                self._handle_fill(tr, cancelled_orders)
            except Exception as e:
                logger.error("Error handling trade %s: %s", tr.get("id"), e)
            finally:
                self._seen_trade_ids.add(tr.get("id"))
                ts = float(tr.get(TRADE_TS_FIELD, 0) or 0)
                self._after_ts = max(self._after_ts, ts)

    def _handle_fill(self, tr: dict, cancelled_orders: set):
        size = int(float(tr.get(TRADE_SIZE_FIELD, 0) or 0))
        price = float(tr.get("price", 0) or 0)
        asset_id = tr.get("asset_id", "")
        market_id = tr.get("market", "")
        if size <= 0:
            return
        # Take-profit sell at fill price (maker limit buy fills at its own price)
        self.api.place_limit_sell(asset_id, price, size)
        self.db.record_trade(
            wallet=self.wallet_address, market_id=market_id,
            market_name="", side="buy", price=price, size=size,
        )
        self.db.set_cooldown(
            self.wallet_address, market_id, self.db.get_settings()["cooldown_minutes"]
        )
        order_id = self.api.extract_maker_order_id(tr)
        if order_id and order_id not in cancelled_orders:
            try:
                self.api.cancel_orders([order_id])
                cancelled_orders.add(order_id)
            except Exception as e:
                logger.warning("Cancel remainder of %s failed: %s", order_id, e)
        elif not order_id:
            logger.warning("No maker order id on trade %s; skip remainder cancel",
                           tr.get("id"))

    # --- Step 2: stop-loss via Data API positions ---
    def check_stop_loss(self):
        settings = self.db.get_settings()
        try:
            positions = self.api.get_user_positions(self._funder())
        except Exception as e:
            logger.warning("Data API positions failed for %s (skip stop-loss): %s",
                           self.wallet_address, e)
            return
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed for %s: %s", self.wallet_address, e)
            open_orders = []
        for pos in positions:
            try:
                self._check_pos_sl(pos, open_orders, settings)
            except Exception as e:
                logger.error("Stop-loss error on %s: %s",
                             pos.get(POS_ASSET_FIELD), e)

    def _check_pos_sl(self, pos: dict, open_orders: list, settings: dict):
        asset_id = pos.get(POS_ASSET_FIELD, "")
        size = int(float(pos.get(POS_SIZE_FIELD, 0) or 0))
        avg = float(pos.get(POS_AVG_FIELD, 0) or 0)
        cur = float(pos.get(POS_CUR_FIELD, 0) or 0)
        if size <= 0:
            return
        if not stop_loss_triggered(cur, avg, settings["stop_loss_pct"]):
            return
        sell_ids = [o["id"] for o in open_orders
                    if o.get("asset_id") == asset_id and o.get("side") == "SELL"]
        if sell_ids:
            try:
                self.api.cancel_orders(sell_ids)
            except Exception as e:
                logger.warning("Cancel sell orders for %s failed: %s", asset_id, e)
        self.api.place_market_sell(asset_id, size)
        self.db.record_trade(
            wallet=self.wallet_address, market_id=pos.get("market", ""),
            market_name="", side="stop_loss", price=cur, size=size,
            pnl=(cur - avg) * size,
        )
        logger.warning("Stop-loss executed: asset=%s size=%d cur=%.4f avg=%.4f",
                       asset_id, size, cur, avg)

    # --- Step 3: strategy compliance on resting buy orders ---
    def check_sell_orders(self):
        """Reused tick name kept for the manager loop; runs strategy compliance."""
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed for %s: %s", self.wallet_address, e)
            return
        for o in open_orders:
            if o.get("side") != "BUY":
                continue
            if int(float(o.get("size_matched", 0) or 0)) != 0:
                continue
            try:
                self._check_compliance(o)
            except Exception as e:
                logger.error("Compliance error on %s: %s", o.get("id"), e)

    def _check_compliance(self, o: dict):
        token_id = o.get("asset_id", "")
        ob = self.api.get_orderbook(token_id)
        bids = sorted(ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
        if not bids or not asks:
            return
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        midpoint = (best_bid + best_ask) / 2
        tick = float(ob.get("tick_size", "0.01"))
        tick_str = ob.get("tick_size", "0.01")
        # rewards_max_spread is not on the order; recover from settings default
        max_spread = 2
        rmin = midpoint - max_spread * tick
        rmax = midpoint + max_spread * tick
        try:
            want = determine_order_price(bids=bids, max_spread=max_spread,
                                         tick_size=tick, reward_range_min=rmin,
                                         reward_range_max=rmax)
        except Exception as e:
            logger.warning("determine_order_price failed for %s: %s", o.get("id"), e)
            return
        action = needs_replace(float(o.get("price", 0)), want, tick)
        if action == "keep":
            return
        try:
            self.api.cancel_orders([o["id"]])
        except Exception as e:
            logger.warning("Cancel %s failed: %s", o.get("id"), e)
            return
        if action == "replace":
            size = int(float(o.get("original_size", 0) or 0))
            neg_risk = bool(o.get("neg_risk", False))
            self.api.place_limit_buy(token_id, want, size,
                                     tick_size=tick_str, neg_risk=neg_risk)
            logger.info("Replaced buy %s -> %.4f", o.get("id"), want)
        else:
            logger.info("Cancelled non-compliant buy %s (no valid price)", o.get("id"))
```

> **Note for implementer:** `max_spread = 2` mirrors the existing `manager.check_existing_orders` default (it used `int(market_data.get("rewards_max_spread", 2))`). The orderbook/order does not carry `rewards_max_spread`; matching the prior default keeps behavior consistent. Do not invent a new config key.

- [ ] **Step 2: Run the full pure-logic suite (unaffected) + import check**

Run: `pytest tests/ -q && python -c "from engine.monitor import OrderMonitor; print('ok')"`
Expected: existing tests pass; prints `ok`

- [ ] **Step 3: Commit**

```bash
git add engine/monitor.py
git commit -m "feat: API-driven OrderMonitor (trades fills, Data-API stop-loss, compliance)"
```

### Task 6: Manager — watermark init, batch cancel, place_orders recompute, drop dead code

**Files:**
- Modify: `engine/manager.py` — `_cancel_buy_orders` (line ~48-59), `WalletWorker.place_orders` (line ~70-128), `check_existing_orders` (line ~130-178, delete), `startup_recovery` (find & edit), `cancel_all_buy_orders` (line ~249-257).

- [ ] **Step 1: Replace `_cancel_buy_orders` with batch cancel**

```python
    def _cancel_buy_orders(self):
        """Cancel all open buy orders on the exchange in one batched request."""
        try:
            open_orders = self.api.get_open_orders()
            buy_ids = [o["id"] for o in open_orders if o.get("side") == "BUY"]
            if buy_ids:
                self.api.cancel_orders(buy_ids)
                logger.info("Cancelled %d buy orders for %s",
                            len(buy_ids), self.wallet_address)
        except Exception as e:
            logger.error("Error cancelling buy orders for %s: %s",
                         self.wallet_address, e)
```

- [ ] **Step 2: Make `place_orders` recompute price before placing**

Replace the order-placement body of `WalletWorker.place_orders`. Keep cooldown + dedupe + balance checks; before placing, re-pull orderbook and re-run `determine_order_price`:

```python
    def place_orders(self, eligible_markets: list[dict]):
        """Place orders on eligible markets; price recomputed at placement time."""
        from engine.strategy import determine_order_price

        for market in eligible_markets:
            if self.db.is_in_cooldown(self.wallet_address, market["market_id"]):
                continue
            try:
                open_orders = self.api.get_open_orders()
            except Exception as e:
                logger.error("get_open_orders failed for %s: %s",
                             self.wallet_address, e)
                continue
            if any(o.get("asset_id") == market["token_id"]
                   and o.get("side") == "BUY" for o in open_orders):
                continue

            try:
                ob = self.api.get_orderbook(market["token_id"])
            except Exception as e:
                logger.warning("Orderbook failed for %s: %s",
                               market["market_name"], e)
                continue
            bids = sorted(ob.get("bids", []),
                          key=lambda x: float(x["price"]), reverse=True)
            asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
            if not bids or not asks:
                continue
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            midpoint = (best_bid + best_ask) / 2
            tick = float(ob.get("tick_size", "0.01"))
            tick_str = ob.get("tick_size", "0.01")
            max_spread = int(market.get("rewards_max_spread", 2))
            rmin = midpoint - max_spread * tick
            rmax = midpoint + max_spread * tick
            try:
                order_price = determine_order_price(
                    bids=bids, max_spread=max_spread, tick_size=tick,
                    reward_range_min=rmin, reward_range_max=rmax)
            except Exception as e:
                logger.warning("Strategy failed for %s: %s",
                               market["market_name"], e)
                continue
            if order_price is None:
                continue

            balance = self.api.get_balance()
            required = market["order_size"] * order_price
            if required > balance:
                logger.info("Insufficient balance %.2f < %.2f for %s",
                            balance, required, market["market_name"])
                continue
            try:
                self.api.place_limit_buy(
                    market["token_id"], order_price, market["order_size"],
                    tick_size=tick_str,
                    neg_risk=market.get("neg_risk", False))
                logger.info("Placed buy %s [%s] @ %.4f x %d",
                            market["market_name"], market["outcome"],
                            order_price, market["order_size"])
            except Exception as e:
                logger.error("Error placing order for %s: %s",
                             market["market_name"], e)
```

> Note: order placement no longer writes `db.record_order` (orders table is not the source of truth anymore). Dedupe is now against live `get_open_orders` by `asset_id`.

- [ ] **Step 3: Delete `check_existing_orders`**

Remove the entire `check_existing_orders` method from `WalletWorker` (Step 3 of the monitor replaces it). Grep to ensure no caller remains:

Run: `grep -rn "check_existing_orders" --include=*.py .`
Expected: no matches (if any caller exists, delete that call site).

- [ ] **Step 4: Init watermark in `startup_recovery`**

Locate `startup_recovery` in `EngineManager`. Replace its body so it no longer cancels stale DB orders / detects DB fills; instead, for each started worker call `worker.monitor.init_watermark()`. Concretely, after workers are constructed:

```python
    def startup_recovery(self):
        """API-driven recovery: seed each monitor's trade watermark from DB history.

        Offline fills are caught next tick by get_trades(after=watermark) +
        id-dedup. Stale resting orders are reconciled by the monitor's
        compliance step. No DB orders/positions reconciliation.
        """
        for worker in self.engines.values():
            try:
                worker.monitor.init_watermark()
            except Exception as e:
                logger.error("Watermark init failed for %s: %s",
                             worker.wallet_address, e)
```

> If `startup_recovery` is currently called before workers are constructed in `start_all`/`start_*`, move the call to after the `start_wallet` loop so `self.engines` is populated. Verify by reading the surrounding method and adjusting the call order.

- [ ] **Step 5: `cancel_all_buy_orders` unchanged in behavior**

`EngineManager.cancel_all_buy_orders` calls `worker._cancel_buy_orders()` which is now batched — no further change needed. Confirm by reading lines ~249-257.

- [ ] **Step 6: Run tests + import**

Run: `pytest tests/ -q && python -c "from engine.manager import EngineManager; print('ok')"`
Expected: existing tests pass; `ok`

- [ ] **Step 7: Commit**

```bash
git add engine/manager.py
git commit -m "refactor: manager batch-cancel, recompute price on place, API-driven recovery"
```

---

## Phase 4 — Order management routes + frontend

### Task 7: `/api/orders` reads live from API + scoring

**Files:**
- Modify: `web/routes.py` — `api_get_orders` (line ~368-373), `api_cancel_order` (376-390), `api_cancel_batch` (393-406), add `api_cancel_all_buys`; `api_cancel_all_buy` (341-346).

- [ ] **Step 1: Add a helper to resolve a wallet's API**

Near the top of the orders section in `web/routes.py` add:

```python
def _wallet_apis(only: str = None) -> dict:
    """Return {address: PolymarketAPI} for enabled wallets (optionally one)."""
    out = {}
    wallets = db.list_wallets()
    for w in wallets:
        if not w.get("enabled"):
            continue
        addr = w["address"]
        if only and addr != only:
            continue
        if manager and manager.engines.get(addr) and manager.engines[addr].running:
            out[addr] = manager.engines[addr].api
        else:
            try:
                from api.polymarket_api import PolymarketAPI
                pk = decrypt(w["encrypted_key"], encryption_key)
                out[addr] = PolymarketAPI(pk, funder=w.get("funder") or None)
            except Exception as e:
                app.logger.error("API build failed for %s: %s", addr, e)
    return out
```

- [ ] **Step 2: Replace `api_get_orders`**

```python
@app.route("/api/orders", methods=["GET"])
@login_required
def api_get_orders():
    wallet = request.args.get("wallet")
    result, errors = [], []
    for addr, api in _wallet_apis(wallet).items():
        try:
            orders = api.get_open_orders()
        except Exception as e:
            errors.append({"wallet": addr, "msg": str(e)})
            continue
        buy_ids = [o["id"] for o in orders if o.get("side") == "BUY"]
        scoring = {}
        try:
            scoring = api.are_orders_scoring(buy_ids)
        except Exception as e:
            app.logger.warning("scoring failed for %s: %s", addr, e)
        for o in orders:
            result.append({
                "wallet": addr,
                "order_id": o.get("id"),
                "market": o.get("market"),
                "asset_id": o.get("asset_id"),
                "side": o.get("side"),
                "outcome": o.get("outcome"),
                "price": float(o.get("price", 0) or 0),
                "original_size": float(o.get("original_size", 0) or 0),
                "size_matched": float(o.get("size_matched", 0) or 0),
                "created_at": o.get("created_at"),
                "scoring": (scoring.get(o.get("id"))
                            if o.get("side") == "BUY" else None),
            })
    return jsonify({"orders": result, "errors": errors})
```

- [ ] **Step 3: Replace cancel routes (batched, no DB write)**

```python
@app.route("/api/orders/cancel-batch", methods=["POST"])
@login_required
def api_cancel_batch():
    data = request.get_json() or {}
    items = data.get("orders", [])  # [{order_id, wallet}, ...]
    by_wallet = {}
    for it in items:
        by_wallet.setdefault(it["wallet"], []).append(it["order_id"])
    apis = _wallet_apis()
    for addr, ids in by_wallet.items():
        api = apis.get(addr)
        if api and ids:
            try:
                api.cancel_orders(ids)
            except Exception as e:
                app.logger.error("cancel-batch failed for %s: %s", addr, e)
    return jsonify({"ok": True})


@app.route("/api/orders/<order_id>/cancel", methods=["POST"])
@login_required
def api_cancel_order(order_id):
    wallet = request.args.get("wallet")
    api = _wallet_apis(wallet).get(wallet) if wallet else None
    if not api:
        # fall back: try every enabled wallet
        for a in _wallet_apis().values():
            try:
                a.cancel_orders([order_id])
            except Exception:
                pass
        return jsonify({"ok": True})
    try:
        api.cancel_orders([order_id])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/orders/cancel-all-buys", methods=["POST"])
@login_required
def api_cancel_all_buys():
    data = request.get_json(silent=True) or {}
    wallet = data.get("wallet")
    for addr, api in _wallet_apis(wallet).items():
        try:
            orders = api.get_open_orders()
            buy_ids = [o["id"] for o in orders if o.get("side") == "BUY"]
            if buy_ids:
                api.cancel_orders(buy_ids)
        except Exception as e:
            app.logger.error("cancel-all-buys failed for %s: %s", addr, e)
    return jsonify({"ok": True})
```

- [ ] **Step 4: Point `/api/engine/cancel-all` at buy-only batch**

Replace `api_cancel_all_buy`:

```python
@app.route("/api/engine/cancel-all", methods=["POST"])
@login_required
def api_cancel_all_buy():
    if manager:
        manager.cancel_all_buy_orders()
    return jsonify({"ok": True})
```

(Unchanged signature; `cancel_all_buy_orders` now batches via Task 6.)

- [ ] **Step 5: Manual smoke test**

Run: `python -c "import web.routes; print('ok')"`
Expected: `ok` (import-level sanity; full route test is manual via the running app).

- [ ] **Step 6: Commit**

```bash
git add web/routes.py
git commit -m "feat: /api/orders live from API + scoring; batched cancel routes"
```

### Task 8: `/api/positions` + `/api/dashboard` off the dead tables

**Files:**
- Modify: `web/routes.py` — `api_get_positions` (~412-429), `api_dashboard` (~487+).

- [ ] **Step 1: Replace `api_get_positions` to use Data API**

```python
@app.route("/api/positions", methods=["GET"])
@login_required
def api_get_positions():
    wallet = request.args.get("wallet")
    sl = db.get_settings()["stop_loss_pct"] / 100.0
    out = []
    for addr, api in _wallet_apis(wallet).items():
        try:
            for p in api.get_user_positions(
                api.client.funder if True else addr
            ):
                avg = float(p.get("<POS_AVG_FIELD>", 0) or 0)
                cur = float(p.get("<POS_CUR_FIELD>", 0) or 0)
                size = float(p.get("<POS_SIZE_FIELD>", 0) or 0)
                out.append({
                    "wallet": addr,
                    "market_name": p.get("title", p.get("market", "")),
                    "buy_price": avg,
                    "current_price": cur,
                    "stop_price": avg * (1 - sl),
                    "pnl": (cur - avg) * size,
                })
        except Exception as e:
            app.logger.warning("positions failed for %s: %s", addr, e)
    return jsonify(out)
```

> Replace `<POS_*>` and the `funder` choice with Task 0 findings. `if True` mirrors `POS_USER_IS_FUNDER` from Task 5 — use the same decision.

- [ ] **Step 2: Replace DB counts in `api_dashboard`**

In `api_dashboard`, replace `total_orders`/`total_positions` and per-wallet `w_orders`/`w_positions` so they no longer call `db.get_open_orders`/`db.get_positions`. Read the full method first, then:
- `total_orders = 0` and per-wallet order/position counts derived from `_wallet_apis()` `get_open_orders()` where cheap, OR simply drop the counts that came from dead tables and keep `total_pnl` from `db.get_trade_history()`. Keep the change minimal: set order/position counts from live `get_open_orders()` length per wallet, positions count from `get_user_positions` length. If an API call fails, use `None` for that wallet's counts (frontend already tolerates missing fields).

Concretely replace the counting lines:

```python
    apis = _wallet_apis()
    total_orders = 0
    total_positions = 0
    trades = db.get_trade_history()
    total_pnl = sum(t.get("pnl", 0) for t in trades)
```

and in the per-wallet loop replace `w_orders`/`w_positions` with:

```python
        api = apis.get(w["address"])
        w_order_count = w_pos_count = None
        if api:
            try:
                oo = api.get_open_orders()
                w_order_count = len(oo)
                total_orders += w_order_count
            except Exception:
                pass
            try:
                w_pos_count = len(api.get_user_positions(api.client.funder))
                total_positions += w_pos_count
            except Exception:
                pass
```

> Read the rest of `api_dashboard` (lines ~500-481 region) and substitute any later use of `w_orders`/`w_positions` with `w_order_count`/`w_pos_count`. Keep response keys identical so the dashboard template keeps working.

- [ ] **Step 3: Import sanity**

Run: `python -c "import web.routes; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add web/routes.py
git commit -m "refactor: positions/dashboard off dead orders/positions tables"
```

### Task 9: `orders.html` — scoring column, filters, sort, buttons, error banner

**Files:**
- Modify: `web/templates/orders.html` (full `{% block %}` rewrite)

- [ ] **Step 1: Replace the template**

```html
{% extends "base.html" %}
{% block content %}
<h1>订单管理</h1>

<div class="filter-bar">
  <label>钱包筛选：</label>
  <select id="wallet-filter" onchange="refreshOrders()">
    <option value="">全部</option>
  </select>
  <label>方向：</label>
  <select id="side-filter" onchange="renderOrders()">
    <option value="">全部</option><option value="BUY">买</option><option value="SELL">卖</option>
  </select>
  <label>奖励区间：</label>
  <select id="scoring-filter" onchange="renderOrders()">
    <option value="">全部</option><option value="1">在区间</option><option value="0">不在区间</option>
  </select>
  <button class="btn btn-sm" onclick="refreshOrders()">立即刷新</button>
</div>

<div id="orders-error" class="loss" style="display:none;"></div>

<h2>当前挂单</h2>
<div class="batch-controls">
  <button class="btn btn-sm btn-danger" onclick="cancelSelected()">撤选中</button>
  <button class="btn btn-sm btn-danger" onclick="cancelAllBuys()">一键撤买单</button>
</div>
<table class="data-table">
  <thead><tr>
    <th><input type="checkbox" id="select-all" onchange="toggleAll(this)"></th>
    <th>钱包</th><th>市场</th><th>方向</th><th>Outcome</th>
    <th onclick="sortBy('price')">价格</th>
    <th onclick="sortBy('original_size')">原始数量</th>
    <th>已成交</th>
    <th onclick="sortBy('scoring')">奖励区间</th>
    <th onclick="sortBy('created_at')">时间</th><th>操作</th>
  </tr></thead>
  <tbody id="orders-body"></tbody>
</table>

<h2>当前持仓</h2>
<table class="data-table">
  <thead><tr><th>钱包</th><th>市场</th><th>买入价</th><th>当前价</th>
  <th>止损价</th><th>盈亏</th></tr></thead>
  <tbody id="positions-body"></tbody>
</table>
{% endblock %}

{% block scripts %}
<script>
let _orders = [], _sortKey = 'created_at', _sortDir = -1;
function getFilter(){return document.getElementById('wallet-filter').value;}

function refreshOrders(){
  const w = getFilter(); const qs = w ? `?wallet=${w}` : '';
  fetch(`/api/orders${qs}`).then(r=>r.json()).then(d=>{
    _orders = d.orders || [];
    const eb = document.getElementById('orders-error');
    if (d.errors && d.errors.length){
      eb.style.display='block';
      eb.textContent = '部分钱包拉取失败: ' + d.errors.map(e=>`${e.wallet.slice(0,8)}(${e.msg})`).join('; ');
    } else { eb.style.display='none'; }
    renderOrders();
  });
  fetch(`/api/positions${qs}`).then(r=>r.json()).then(ps=>{
    document.getElementById('positions-body').innerHTML = ps.map(p=>`
      <tr><td title="${p.wallet}">${p.wallet.slice(0,6)}...${p.wallet.slice(-4)}</td>
      <td>${p.market_name}</td><td>${(p.buy_price||0).toFixed(4)}</td>
      <td>${p.current_price!=null?p.current_price.toFixed(4):'-'}</td>
      <td>${p.stop_price!=null?p.stop_price.toFixed(4):'-'}</td>
      <td class="${(p.pnl||0)>=0?'profit':'loss'}">${(p.pnl||0).toFixed(2)}</td></tr>`).join('');
  });
}

function sortBy(k){ _sortDir = (_sortKey===k)? -_sortDir : 1; _sortKey=k; renderOrders(); }

function renderOrders(){
  const sf = document.getElementById('side-filter').value;
  const cf = document.getElementById('scoring-filter').value;
  let rows = _orders.filter(o=>{
    if (sf && o.side!==sf) return false;
    if (cf==='1' && o.scoring!==true) return false;
    if (cf==='0' && o.scoring!==false) return false;
    return true;
  });
  rows.sort((a,b)=>{
    const x=a[_sortKey], y=b[_sortKey];
    return ((x>y)-(x<y))*_sortDir;
  });
  document.getElementById('orders-body').innerHTML = rows.map(o=>`
    <tr><td><input type="checkbox" class="order-check"
        data-id="${o.order_id}" data-wallet="${o.wallet}"></td>
    <td title="${o.wallet}">${o.wallet.slice(0,6)}...${o.wallet.slice(-4)}</td>
    <td>${o.market||''}</td><td>${o.side}</td><td>${o.outcome||''}</td>
    <td>${(o.price||0).toFixed(4)}</td><td>${o.original_size}</td>
    <td>${o.size_matched}</td>
    <td>${o.side==='BUY' ? (o.scoring===true?'✓':(o.scoring===false?'✗':'?')) : '—'}</td>
    <td>${o.created_at? new Date(o.created_at*1000).toLocaleString('zh-CN'):'-'}</td>
    <td><button class="btn btn-sm btn-danger"
        onclick="cancelOne('${o.order_id}','${o.wallet}')">撤单</button></td></tr>`).join('');
}

function toggleAll(c){document.querySelectorAll('.order-check').forEach(x=>x.checked=c.checked);}

function _selected(){
  return [...document.querySelectorAll('.order-check:checked')]
    .map(c=>({order_id:c.dataset.id, wallet:c.dataset.wallet}));
}
function cancelSelected(){
  const items=_selected();
  if(!items.length){alert('请先勾选要撤销的订单');return;}
  if(!confirm(`确定撤销 ${items.length} 笔订单？`))return;
  fetch('/api/orders/cancel-batch',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({orders:items})}).then(()=>refreshOrders());
}
function cancelOne(id,wallet){
  if(!confirm('确定撤销该订单？'))return;
  fetch('/api/orders/cancel-batch',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({orders:[{order_id:id,wallet:wallet}]})}).then(()=>refreshOrders());
}
function cancelAllBuys(){
  const w=getFilter();
  if(!confirm('确定撤销'+(w?'该钱包':'全部钱包')+'所有买单？（保留止盈卖单）'))return;
  fetch('/api/orders/cancel-all-buys',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({wallet:w||null})}).then(()=>refreshOrders());
}

fetch('/api/wallets').then(r=>r.json()).then(ws=>{
  const s=document.getElementById('wallet-filter');
  ws.forEach(w=>{const o=document.createElement('option');
    o.value=w.address;o.textContent=`${w.address.slice(0,6)}...${w.address.slice(-4)}`;
    s.appendChild(o);});
});
refreshOrders();
setInterval(refreshOrders, 10000);
</script>
{% endblock %}
```

- [ ] **Step 2: Manual verification**

Run the app (`python app.py`), log in, open 订单管理. Confirm: orders list matches the exchange (cross-check with `python test_real_order.py` placing a test order — it should now appear), scoring column shows ✓/✗ for buys, side/scoring filters and column-sort work, "撤选中"/"一键撤买单"/行内撤单 succeed, error banner shows if a wallet API fails.

- [ ] **Step 3: Commit**

```bash
git add web/templates/orders.html
git commit -m "feat: orders page live API data, scoring, filters, sort, batch buttons"
```

---

## Phase 5 — Docs sync + cleanup

### Task 10: Sync CLAUDE.md and remove the spike script

**Files:**
- Modify: `CLAUDE.md`
- Delete: `scripts/spike_api_shapes.py`

- [ ] **Step 1: Update CLAUDE.md wording**

Edit these passages to match the new reality (read each line first, then replace):
- "Pipeline: scan → strategy → place → monitor" — note that monitor is now API-driven (trades for fills, Data API for positions/stop-loss, compliance re-checks resting buys).
- The `engine/monitor.py` bullet — replace "tracks `size_matched` deltas" with "detects fills via CLOB `get_trades` (id-dedup + timestamp watermark); stop-loss uses Data API position avgPrice".
- "Startup recovery" bullet — replace with: recovery seeds each monitor's trade watermark from DB trade history; offline fills caught next tick via `get_trades(after=watermark)`; no DB orders/positions reconciliation.
- "Balance is re-read before every order placement" — still true (kept in `place_orders`); leave as-is.
- Add a line under "Critical behaviors": orders/positions DB tables are no longer the source of truth — order management, positions, dashboard counts, monitor all read live API; DB keeps only `trades` + `cooldown`.

- [ ] **Step 2: Delete the throwaway spike**

```bash
git rm scripts/spike_api_shapes.py
```

- [ ] **Step 3: Full test run**

Run: `pytest -q`
Expected: all pure-logic tests pass (`tests/test_fills.py`, `tests/test_strategy_check.py`, `tests/test_risk.py`, plus existing `tests/test_strategy.py`).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: sync CLAUDE.md to API-driven orders/monitor; drop spike script"
```

---

## Self-Review (completed by plan author)

**Spec coverage:**
- Decision table rows → Task mapping: total architecture (T5/T6/T7/T8), strategy口径 determine_order_price (T2/T5 Step3), strict tick replace (T2), want=None cancel (T2/T5), A+B API (T5), stop-loss avgPrice via Data API (T3/T4/T5 Step2), 一键撤买单 buy-only (T7), multi-select cancel_orders (T7), fill detection via get_trades + id dedup + watermark (T0/T1/T5 Step1), 成交后撤该单剩余 (T5 `_handle_fill`), 分发挂单 recompute (T6), startup watermark from DB trades (T5 `init_watermark`/T6), `/api/positions`+dashboard off dead tables (T8), docs sync (T10). All covered.
- Error handling (spec §6): get_trades fail skip-no-advance (T5), Data positions fail skip stop-loss (T5), sell-fail still mark seen (T5 `finally`), per-item try/except (T5/T7). Covered.
- Testing (spec §7): pure-function suites T1-T3; IO via manual/`test_real_order.py` (T8 step2). Covered.

**Placeholder scan:** The `<TRADE_*>`/`<POS_*>` tokens are intentional, gated by Task 0 with an explicit "must replace before implementing" instruction and a stop-if-different rule — not silent placeholders. No "TBD/handle edge cases/similar to" patterns. All code steps contain full code.

**Type consistency:** `select_new_trades(trades, seen_ids, ts_field)`, `needs_replace(current_price, want_price, tick)→{"keep","replace","cancel"}`, `stop_loss_triggered(cur_price, avg_price, stop_loss_pct)→bool`, `are_orders_scoring(order_ids)→dict`, `get_user_positions(user)→list`, `extract_maker_order_id(trade)→str|None`, cancel routes consume `{orders:[{order_id,wallet}]}` — names consistent across T1-T9.

