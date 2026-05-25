"""tests/test_strategy.py"""

import pytest
from engine.strategy import determine_order_price, reward_price_range


def _make_bids(price_size_pairs):
    """Helper: create bids list from [(price, size), ...]."""
    return [{"price": p, "size": s} for p, s in price_size_pairs]


class TestMaxSpread2_TickSize1Cent:
    """max_spread=2, tick_size=0.01 (1 cent increments)."""

    def test_bid1_gt_2000_place_at_bid2(self):
        bids = _make_bids([(0.30, 3000), (0.29, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.01,
            reward_range_min=0.28,
            reward_range_max=0.32,
        )
        assert result == 0.29

    def test_bid1_le_2000_skip(self):
        bids = _make_bids([(0.30, 1500), (0.29, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.01,
            reward_range_min=0.28,
            reward_range_max=0.32,
        )
        assert result is None

    def test_result_outside_reward_range_returns_none(self):
        bids = _make_bids([(0.30, 3000), (0.29, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.01,
            reward_range_min=0.30,
            reward_range_max=0.32,
        )
        # bid2 is 0.29, below reward_range_min 0.30
        assert result is None


class TestMaxSpread2_TickSize01Cent:
    """max_spread=2, tick_size=0.001 (0.1 cent increments)."""

    def test_cumulative_gt_6000_next_position(self):
        bids = _make_bids(
            [
                (0.300, 3000),
                (0.299, 2000),
                (0.298, 2000),
                (0.297, 500),
            ]
        )
        # cumsum: 3000, 5000, 7000 -> 7000 > 6000 at 0.298, next = 0.297
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.001,
            reward_range_min=0.290,
            reward_range_max=0.310,
        )
        assert result == 0.297

    def test_cumulative_never_exceeds_returns_none(self):
        bids = _make_bids([(0.300, 1000), (0.299, 1000)])
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.001,
            reward_range_min=0.290,
            reward_range_max=0.310,
        )
        assert result is None


class TestMaxSpreadGE3_TickSize1Cent:
    """max_spread>=3, tick_size=0.01."""

    def test_bid1_gt_2000_place_bid2(self):
        bids = _make_bids([(0.30, 3000), (0.29, 500), (0.28, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=3,
            tick_size=0.01,
            reward_range_min=0.27,
            reward_range_max=0.32,
        )
        assert result == 0.29

    def test_bid1_le_2000_bid2_gt_2000_place_bid3(self):
        bids = _make_bids([(0.30, 1000), (0.29, 3000), (0.28, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=3,
            tick_size=0.01,
            reward_range_min=0.27,
            reward_range_max=0.32,
        )
        assert result == 0.28

    def test_bid1_bid2_lt_2000_bid3_gt_2000_place_bid4(self):
        bids = _make_bids(
            [
                (0.30, 500),
                (0.29, 500),
                (0.28, 3000),
                (0.27, 100),
            ]
        )
        result = determine_order_price(
            bids=bids,
            max_spread=3,
            tick_size=0.01,
            reward_range_min=0.26,
            reward_range_max=0.32,
        )
        assert result == 0.27

    def test_fallback_keeps_searching(self):
        # All levels <= 2000, keep going until exceed max_spread
        bids = _make_bids(
            [
                (0.30, 500),
                (0.29, 500),
                (0.28, 500),
                (0.27, 500),
            ]
        )
        result = determine_order_price(
            bids=bids,
            max_spread=3,
            tick_size=0.01,
            reward_range_min=0.26,
            reward_range_max=0.32,
        )
        # No level > 2000, exhausted max_spread range
        assert result is None


class TestMaxSpreadGE3_TickSize01Cent:
    """max_spread>=3, tick_size=0.001."""

    def test_cumulative_gt_6000(self):
        bids = _make_bids(
            [
                (0.300, 2000),
                (0.299, 2000),
                (0.298, 3000),
                (0.297, 100),
            ]
        )
        # cumsum: 2000, 4000, 7000 -> next = 0.297
        result = determine_order_price(
            bids=bids,
            max_spread=3,
            tick_size=0.001,
            reward_range_min=0.290,
            reward_range_max=0.310,
        )
        assert result == 0.297


class TestRewardPriceRange:
    """reward_price_range: the reward band is max_spread CENTS around the
    midpoint, independent of tick size.

    Regression: callers used to compute the band as ``max_spread * tick_size``,
    treating Polymarket's cents-valued max_spread as a tick count. That happens
    to be correct only for 1-cent markets (tick == 1 cent); on 0.1-cent markets
    (tick 0.001) it produced a band ~10x too narrow, so determine_order_price
    almost always returned None and 0.1-cent markets never got an order.
    """

    def test_band_is_cents_not_ticks(self):
        lo, hi = reward_price_range(0.6375, 4.5)
        assert lo == pytest.approx(0.5925)
        assert hi == pytest.approx(0.6825)

    def test_fractional_cents_not_truncated(self):
        # 3.5 cents must NOT be truncated to 3.
        lo, hi = reward_price_range(0.50, 3.5)
        assert lo == pytest.approx(0.465)
        assert hi == pytest.approx(0.535)

    def test_band_independent_of_tick_size(self):
        # Same max_spread cents -> same band regardless of market tick size.
        assert reward_price_range(0.30, 3) == pytest.approx((0.27, 0.33))

    def test_fine_tick_market_prices_with_correct_band(self):
        # 0.1-cent book whose cumulative depth crosses 6000 several levels
        # below the midpoint. The old (buggy) band excluded that level; the
        # correct cents-based band includes it.
        bids = _make_bids(
            [
                (0.637, 3200),
                (0.636, 1500),
                (0.635, 800),
                (0.634, 900),
                (0.628, 400),
                (0.625, 300),
                (0.619, 500),
            ]
        )
        midpoint = 0.6375
        max_spread_cents = 4.5  # real Polymarket value for such a market

        # Old buggy band: int(4.5) * 0.001 = +-0.004 -> excludes 0.628 -> None.
        old_int = int(max_spread_cents)
        old_lo = midpoint - old_int * 0.001
        old_hi = midpoint + old_int * 0.001
        assert determine_order_price(bids, old_int, 0.001, old_lo, old_hi) is None

        # Correct band: 4.5 cents = +-0.045 -> includes 0.628.
        lo, hi = reward_price_range(midpoint, max_spread_cents)
        assert determine_order_price(bids, old_int, 0.001, lo, hi) == 0.628
