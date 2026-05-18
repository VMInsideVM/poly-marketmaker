# engine/strategy_check.py
"""Pure strategy-compliance decision (no network/IO)."""

from typing import Optional


def _tick_index(price: float, tick: float) -> int:
    return round(float(price) / float(tick))


def needs_replace(
    current_price: float, want_price: Optional[float], tick: float
) -> str:
    """Decide action for a resting buy order.

    Returns:
      "cancel"  -> want_price is None: no compliant price exists, cancel only
      "replace" -> recomputed price is on a different tick than current
      "keep"    -> same tick, leave the order alone

    Contract: want_price is either None or a valid float price; falsy-but-not-None values are NOT treated as "cancel".
    """
    if want_price is None:
        return "cancel"
    if _tick_index(current_price, tick) == _tick_index(want_price, tick):
        return "keep"
    return "replace"
