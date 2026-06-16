"""tests/test_strategy.py"""

import pytest
from engine.strategy import reward_price_range


class TestRewardPriceRange:
    """reward_price_range: the reward band is max_spread CENTS around the
    midpoint, independent of tick size.

    Regression: callers used to compute the band as ``max_spread * tick_size``,
    treating Polymarket's cents-valued max_spread as a tick count. That happens
    to be correct only for 1-cent markets (tick == 1 cent); on 0.1-cent markets
    (tick 0.001) it produced a band ~10x too narrow, so orders on those markets
    never earned rewards.
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

    def test_fine_tick_market_band_covers_expected_range(self):
        # 0.1-cent market: 4.5 cents max_spread → band is ±0.045 around midpoint.
        midpoint = 0.6375
        max_spread_cents = 4.5
        lo, hi = reward_price_range(midpoint, max_spread_cents)
        assert lo == pytest.approx(0.5925)
        assert hi == pytest.approx(0.6825)
        # A price 4 cents below midpoint (0.5975) is inside the correct band …
        assert lo <= 0.628 <= hi
        # … but the old buggy band int(4.5)*0.001 = ±0.004 excludes it.
        old_lo = midpoint - int(max_spread_cents) * 0.001
        old_hi = midpoint + int(max_spread_cents) * 0.001
        assert not (old_lo <= 0.628 <= old_hi)
