"""tests/test_strategy.py"""

import pytest
from engine.strategy import determine_order_price


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

    def test_bid1_le_2000_place_at_bid1(self):
        bids = _make_bids([(0.30, 1500), (0.29, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.01,
            reward_range_min=0.28,
            reward_range_max=0.32,
        )
        assert result == 0.30

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
