"""tests/test_eligibility.py — pure bid-ask-spread re-check for resting buys."""

from engine.eligibility import recheck_resting_buy

S = {"max_spread_cents": 3.0}


def test_keeps_when_spread_below_threshold():
    cancel, reason = recheck_resting_buy(2.0, S)
    assert cancel is False
    assert reason is None


def test_cancels_when_spread_at_threshold():
    # boundary: spread == threshold cancels (condition is >=)
    cancel, reason = recheck_resting_buy(3.0, S)
    assert cancel is True
    assert "价差" in reason


def test_cancels_when_spread_above_threshold():
    cancel, reason = recheck_resting_buy(8.5, S)
    assert cancel is True
    assert "价差" in reason


def test_keeps_when_spread_unknown():
    # None = couldn't determine spread -> conservative keep
    cancel, reason = recheck_resting_buy(None, S)
    assert cancel is False
    assert reason is None


def test_missing_threshold_is_noop():
    cancel, reason = recheck_resting_buy(99.0, {})
    assert cancel is False
    assert reason is None
