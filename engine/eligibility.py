# engine/eligibility.py
"""Pure re-check of Market Scanner eligibility for a resting buy (no network/IO).

Mirrors the scanner's filters (engine/scanner.py) so a resting BUY that no
longer meets them can be cancelled. Any unknown input (None) skips that
dimension (conservative keep). A missing threshold in settings makes that
dimension a no-op (keep), so callers that pass a partial settings dict do not
trip gates they didn't configure.
"""

from typing import Optional, Tuple


def recheck_resting_buy(
    reward_total: Optional[float],
    days_left: Optional[float],
    best_bid_cents: Optional[float],
    spread_cents: Optional[float],
    settings: dict,
) -> Tuple[bool, Optional[str]]:
    """Re-check whether a resting BUY still meets scanner eligibility.

    Returns (cancel, reason):
      cancel=True  -> no longer eligible; reason is a zh string for the log.
      cancel=False -> keep; reason is None.

    Inputs (None = unknown -> skip that dimension):
      reward_total   : sum of rate_per_day for the market (USD/day)
      days_left      : days until settlement (negative = no end date / passed)
      best_bid_cents : current best bid * 100
      spread_cents   : (best_ask - best_bid) * 100

    Check order matches scanner: reward -> settlement -> price band -> spread.
    First failing dimension wins.
    """
    min_reward = float(settings.get("min_reward_usd", 0.0))
    min_days = float(settings.get("min_settlement_days", 0))
    min_price_cents = float(settings.get("min_price_cents", 0.0))
    max_price_cents = float(settings.get("max_price_cents", 100.0))
    max_spread_cents = float(settings.get("max_spread_cents", float("inf")))

    # 1. Reward amount (scanner.py:128)
    if reward_total is not None and reward_total < min_reward:
        return (
            True,
            f"市场日奖励 ${reward_total:.0f} 跌破阈值 ${min_reward:.0f}，撤买单",
        )

    # 2. Settlement days (scanner.py:92) — only the [0, min_days) window excludes;
    #    negative days_left passes, identical to the scanner.
    if days_left is not None and 0 <= days_left < min_days:
        return (
            True,
            f"距结算仅 {days_left:.1f} 天 < 阈值 {min_days:.0f} 天，撤买单避结算风险",
        )

    # 3. Price band on best_bid (scanner.py:194)
    if best_bid_cents is not None and not (
        min_price_cents <= best_bid_cents <= max_price_cents
    ):
        return True, (
            f"最优买价 {best_bid_cents:.1f}c 跑出区间 "
            f"[{min_price_cents:.0f}c, {max_price_cents:.0f}c]，撤买单"
        )

    # 4. Bid-ask spread (scanner.py:163)
    if spread_cents is not None and spread_cents >= max_spread_cents:
        return (
            True,
            f"买一卖一价差 {spread_cents:.1f}c ≥ 阈值 {max_spread_cents:.0f}c，撤买单",
        )

    return False, None
