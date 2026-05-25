"""tests/test_take_profit.py — pure position-driven take-profit planning."""

import pytest
from engine.take_profit import (
    ceil_to_tick,
    plan_take_profit,
    cost_basis_from_buy_fills,
    cost_basis_with_lots,
    take_profit_price,
)


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
        plan = plan_take_profit(size=0.0, want_price=0.30, tick=0.01, existing_sells=[])
        assert plan["action"] == "noop"

    def test_zero_price_is_noop(self):
        plan = plan_take_profit(
            size=222.0, want_price=0.0, tick=0.01, existing_sells=[]
        )
        assert plan["action"] == "noop"

    def test_no_existing_sell_places_one(self):
        plan = plan_take_profit(
            size=222.08, want_price=0.30, tick=0.01, existing_sells=[]
        )
        assert plan["action"] == "replace"
        assert plan["price"] == 0.30
        assert plan["size"] == 222.08
        assert plan["cancel_ids"] == []

    def test_correct_single_sell_is_kept(self):
        sells = [_sell("s1", 0.30, 222.08)]
        plan = plan_take_profit(
            size=222.08, want_price=0.30, tick=0.01, existing_sells=sells
        )
        assert plan["action"] == "keep"
        assert plan["cancel_ids"] == []

    def test_phantom_high_price_sell_is_replaced(self):
        sells = [_sell("phantom", 0.38, 200.0)]
        plan = plan_take_profit(
            size=222.08, want_price=0.30, tick=0.01, existing_sells=sells
        )
        assert plan["action"] == "replace"
        assert plan["price"] == 0.30
        assert plan["cancel_ids"] == ["phantom"]

    def test_split_orders_collapse_to_one(self):
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
        plan = plan_take_profit(
            size=222.08, want_price=0.30, tick=0.01, existing_sells=sells
        )
        assert plan["action"] == "replace"
        assert plan["price"] == 0.30
        assert set(plan["cancel_ids"]) == {"a", "b", "c", "d", "e", "f", "g", "h"}

    def test_single_sell_wrong_size_is_replaced(self):
        sells = [_sell("s1", 0.30, 100.0)]
        plan = plan_take_profit(
            size=222.08, want_price=0.30, tick=0.01, existing_sells=sells
        )
        assert plan["action"] == "replace"
        assert plan["cancel_ids"] == ["s1"]

    def test_single_sell_within_size_tolerance_is_kept(self):
        sells = [_sell("s1", 0.30, 222.08, matched=0.5)]
        plan = plan_take_profit(
            size=222.08, want_price=0.30, tick=0.01, existing_sells=sells
        )
        assert plan["action"] == "keep"


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


def _bft(price, size, ts, tid):
    return {"price": price, "size": size, "ts": ts, "trade_id": tid}


class TestCostBasisWithLots:
    def test_single_buy_one_lot(self):
        cost, lots = cost_basis_with_lots([_bft(0.28, 361, 100, "T1")], 361)
        assert cost == pytest.approx(0.28)
        assert lots == [{"price": 0.28, "take": 361.0, "ts": 100.0, "trade_id": "T1"}]

    def test_multi_buy_weighted_and_lots(self):
        fills = [_bft(0.20, 200, 1, "A"), _bft(0.28, 200, 2, "B")]
        cost, lots = cost_basis_with_lots(fills, 400)
        assert cost == pytest.approx(0.24)
        assert len(lots) == 2
        assert sum(l["take"] for l in lots) == pytest.approx(400.0)

    def test_partial_lot_take_is_consumed_amount(self):
        # 最新 161@0.28(ts2) 全取,更早 200@0.20(ts1) 只取 39 凑满 200
        fills = [_bft(0.20, 200, 1, "OLD"), _bft(0.28, 161, 2, "NEW")]
        cost, lots = cost_basis_with_lots(fills, 200)
        takes = {l["trade_id"]: l["take"] for l in lots}
        assert takes["NEW"] == pytest.approx(161.0)
        assert takes["OLD"] == pytest.approx(39.0)
        assert cost == pytest.approx((161 * 0.28 + 39 * 0.20) / 200)

    def test_insufficient_fills_graceful(self):
        cost, lots = cost_basis_with_lots([_bft(0.28, 100, 1, "X")], 361)
        assert cost == pytest.approx(0.28)
        assert lots[0]["take"] == pytest.approx(100.0)

    def test_empty_fills(self):
        assert cost_basis_with_lots([], 361) == (None, [])

    def test_zero_size(self):
        assert cost_basis_with_lots([_bft(0.28, 361, 1, "X")], 0) == (None, [])


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
