# engine/eligibility.py
"""Pure re-check of a resting buy against the bid-ask spread (no network/IO).

The order monitor's Step 3 cancels a resting BUY whose market's bid-ask spread
has widened past the configured threshold (max_spread_cents) — mirroring the
scanner's spread filter. spread_cents None (unknown) keeps the order; a missing
threshold makes the check a no-op (keep).
"""

from typing import Optional, Tuple


def recheck_resting_buy(
    spread_cents: Optional[float],
    settings: dict,
) -> Tuple[bool, Optional[str]]:
    """Re-check a resting BUY against the bid-ask spread only.

    Returns (cancel, reason):
      cancel=True  -> spread at/over the threshold; reason is a zh string.
      cancel=False -> keep; reason is None.

    spread_cents is (best_ask - best_bid) * 100. None = unknown -> keep
    (conservative). A missing max_spread_cents threshold is a no-op (keep).
    """
    max_spread_cents = float(settings.get("max_spread_cents", float("inf")))
    if spread_cents is not None and spread_cents >= max_spread_cents:
        return (
            True,
            f"买一卖一价差 {spread_cents:.1f}c ≥ 阈值 {max_spread_cents:.0f}c，撤买单",
        )
    return False, None
