"""tests/test_take_profit.py — pure position-driven take-profit planning."""

from engine.take_profit import ceil_to_tick, plan_take_profit


# ---------------------------------------------------------------------------
# ceil_to_tick: round a (possibly off-tick) avgPrice UP to a valid tick so a
# take-profit sell never goes below cost ("原价卖出不亏本金").
# ---------------------------------------------------------------------------


class TestCeilToTick:
    def test_on_tick_price_unchanged(self):
        # avgPrice exactly on a 0.01 tick must not bump up to the next tick.
        assert ceil_to_tick(0.30, 0.01) == 0.30

    def test_on_tick_price_with_float_dirt_unchanged(self):
        # Data API avg often carries float dirt (e.g. 0.30000002); stays 0.30.
        assert ceil_to_tick(0.3000000244755322, 0.01) == 0.30

    def test_off_tick_rounds_up(self):
        # 0.3023 -> 0.31 (smallest 0.01-multiple >= cost, so we never lose).
        assert ceil_to_tick(0.3023, 0.01) == 0.31

    def test_finer_tick_keeps_precision(self):
        assert ceil_to_tick(0.305, 0.001) == 0.305

    def test_zero_tick_returns_input(self):
        assert ceil_to_tick(0.30, 0) == 0.30


# ---------------------------------------------------------------------------
# plan_take_profit: decide how to reconcile resting SELLs for ONE position so
# that exactly one sell rests at the cost price for the full position size.
# ---------------------------------------------------------------------------


def _sell(oid, price, original, matched=0):
    return {
        "id": oid,
        "side": "SELL",
        "price": str(price),
        "original_size": str(original),
        "size_matched": str(matched),
    }


class TestPlanTakeProfit:
    def test_no_position_is_noop(self):
        plan = plan_take_profit(size=0.0, avg=0.30, tick=0.01, existing_sells=[])
        assert plan["action"] == "noop"

    def test_zero_avg_is_noop(self):
        plan = plan_take_profit(size=222.0, avg=0.0, tick=0.01, existing_sells=[])
        assert plan["action"] == "noop"

    def test_no_existing_sell_places_one_at_cost(self):
        plan = plan_take_profit(size=222.08, avg=0.30, tick=0.01, existing_sells=[])
        assert plan["action"] == "replace"
        assert plan["price"] == 0.30
        assert plan["size"] == 222.08
        assert plan["cancel_ids"] == []

    def test_correct_single_sell_is_kept(self):
        sells = [_sell("s1", 0.30, 222.08)]
        plan = plan_take_profit(size=222.08, avg=0.30, tick=0.01, existing_sells=sells)
        assert plan["action"] == "keep"
        assert plan["cancel_ids"] == []

    def test_phantom_high_price_sell_is_replaced(self):
        # The reported bug: 200 shares resting at 0.38 while real cost is 0.30.
        sells = [_sell("phantom", 0.38, 200.0)]
        plan = plan_take_profit(size=222.08, avg=0.30, tick=0.01, existing_sells=sells)
        assert plan["action"] == "replace"
        assert plan["price"] == 0.30
        assert plan["size"] == 222.08
        assert plan["cancel_ids"] == ["phantom"]

    def test_split_orders_collapse_to_one(self):
        # The reported bug: one position split into 8 resting sells.
        sells = [
            _sell("a", 0.38, 177.77),
            _sell("b", 0.38, 22.23),
            _sell("c", 0.30, 25.0),
            _sell("d", 0.30, 22.857),
            _sell("e", 0.30, 8.171),
            _sell("f", 0.30, 7.471),
            _sell("g", 0.30, 7.28),
            _sell("h", 0.30, 7.128),
        ]
        plan = plan_take_profit(size=222.08, avg=0.30, tick=0.01, existing_sells=sells)
        assert plan["action"] == "replace"
        assert plan["price"] == 0.30
        assert set(plan["cancel_ids"]) == {"a", "b", "c", "d", "e", "f", "g", "h"}

    def test_single_sell_wrong_size_is_replaced(self):
        sells = [_sell("s1", 0.30, 100.0)]
        plan = plan_take_profit(size=222.08, avg=0.30, tick=0.01, existing_sells=sells)
        assert plan["action"] == "replace"
        assert plan["cancel_ids"] == ["s1"]

    def test_single_sell_within_size_tolerance_is_kept(self):
        # Tiny drift (partial fill / float) must not churn cancel+replace.
        sells = [_sell("s1", 0.30, 222.08, matched=0.5)]  # remaining 221.58
        plan = plan_take_profit(size=222.08, avg=0.30, tick=0.01, existing_sells=sells)
        assert plan["action"] == "keep"
