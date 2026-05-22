"""tests/test_eligibility.py — pure scanner-eligibility re-check for resting buys."""

from engine.eligibility import recheck_resting_buy

# Full thresholds present (mirrors config.DEFAULTS for the relevant keys).
S = {
    "min_reward_usd": 100.0,
    "min_settlement_days": 4,
    "min_price_cents": 10.0,
    "max_price_cents": 50.0,
    "max_spread_cents": 3.0,
}


def test_keeps_when_all_pass():
    cancel, reason = recheck_resting_buy(150.0, 10.0, 30.0, 2.0, S)
    assert cancel is False
    assert reason is None


def test_cancels_when_reward_below_threshold():
    cancel, reason = recheck_resting_buy(50.0, 10.0, 30.0, 2.0, S)
    assert cancel is True
    assert "奖励" in reason


def test_keeps_when_reward_unknown():
    cancel, reason = recheck_resting_buy(None, 10.0, 30.0, 2.0, S)
    assert cancel is False


def test_cancels_when_near_settlement():
    cancel, reason = recheck_resting_buy(150.0, 2.0, 30.0, 2.0, S)
    assert cancel is True
    assert "结算" in reason


def test_keeps_when_days_left_negative():
    # negative days_left = no end date / already passed -> scanner does NOT exclude
    cancel, reason = recheck_resting_buy(150.0, -1.0, 30.0, 2.0, S)
    assert cancel is False


def test_keeps_when_days_left_equals_threshold():
    # boundary: days_left == min_settlement_days -> NOT excluded (condition is < min_days)
    cancel, reason = recheck_resting_buy(150.0, 4.0, 30.0, 2.0, S)
    assert cancel is False


def test_keeps_when_days_left_unknown():
    cancel, reason = recheck_resting_buy(150.0, None, 30.0, 2.0, S)
    assert cancel is False


def test_cancels_when_bid_below_band():
    cancel, reason = recheck_resting_buy(150.0, 10.0, 5.0, 2.0, S)
    assert cancel is True
    assert "区间" in reason


def test_cancels_when_bid_above_band():
    cancel, reason = recheck_resting_buy(150.0, 10.0, 60.0, 2.0, S)
    assert cancel is True
    assert "区间" in reason


def test_keeps_when_bid_at_band_edges():
    assert recheck_resting_buy(150.0, 10.0, 10.0, 2.0, S)[0] is False
    assert recheck_resting_buy(150.0, 10.0, 50.0, 2.0, S)[0] is False


def test_keeps_when_bid_unknown():
    cancel, reason = recheck_resting_buy(150.0, 10.0, None, 2.0, S)
    assert cancel is False


def test_cancels_when_spread_at_or_over_threshold():
    cancel, reason = recheck_resting_buy(150.0, 10.0, 30.0, 3.0, S)  # 3.0 >= 3.0
    assert cancel is True
    assert "价差" in reason


def test_keeps_when_spread_unknown():
    cancel, reason = recheck_resting_buy(150.0, 10.0, 30.0, None, S)
    assert cancel is False


def test_reward_reason_wins_when_multiple_fail():
    # reward checked first
    cancel, reason = recheck_resting_buy(50.0, 2.0, 5.0, 9.0, S)
    assert cancel is True
    assert "奖励" in reason


def test_missing_thresholds_are_noops():
    # No thresholds in settings -> every dimension is a no-op (keep).
    cancel, reason = recheck_resting_buy(0.0, 0.0, 99.0, 99.0, {})
    assert cancel is False
    assert reason is None
