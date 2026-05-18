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
