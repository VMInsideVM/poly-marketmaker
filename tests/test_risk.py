# tests/test_risk.py
from engine.risk import stop_loss_triggered


def test_triggers_below_threshold():
    # avg 0.50, 15% stop -> threshold 0.425
    assert (
        stop_loss_triggered(cur_price=0.42, avg_price=0.50, stop_loss_pct=15.0) is True
    )


def test_no_trigger_at_or_above_threshold():
    assert (
        stop_loss_triggered(cur_price=0.425, avg_price=0.50, stop_loss_pct=15.0)
        is False
    )
    assert (
        stop_loss_triggered(cur_price=0.60, avg_price=0.50, stop_loss_pct=15.0) is False
    )


def test_zero_or_missing_prices_never_trigger():
    assert (
        stop_loss_triggered(cur_price=0.0, avg_price=0.50, stop_loss_pct=15.0) is False
    )
    assert (
        stop_loss_triggered(cur_price=0.42, avg_price=0.0, stop_loss_pct=15.0) is False
    )
