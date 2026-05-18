# engine/risk.py
"""Pure stop-loss decision (no network/IO)."""


def stop_loss_triggered(
    cur_price: float, avg_price: float, stop_loss_pct: float
) -> bool:
    """True when current price has fallen to/below the stop threshold.

    cur_price==0 (no quote) or avg_price==0 (no cost basis) never triggers —
    we do not market-sell on missing data.
    """
    cur = float(cur_price or 0)
    avg = float(avg_price or 0)
    if cur <= 0 or avg <= 0:
        return False
    threshold = avg * (1 - float(stop_loss_pct) / 100.0)
    return cur < threshold
