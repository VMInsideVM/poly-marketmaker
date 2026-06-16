"""engine/strategy.py — Reward band helper (order pricing moved to laddering engine)."""


def reward_price_range(midpoint: float, max_spread_cents: float) -> tuple[float, float]:
    """Reward-eligible price band ``[midpoint - d, midpoint + d]``.

    Polymarket's rewards ``max_spread`` is expressed in CENTS (e.g. 3, 4.5):
    the maximum distance from the midpoint, in cents, for an order to earn
    rewards. It is NOT a count of ticks. ``d = max_spread_cents * 0.01`` keeps
    the band correct for both 1-cent (tick 0.01) and 0.1-cent (tick 0.001)
    markets.

    The old ``max_spread * tick_size`` form only happened to be right when
    tick == 1 cent; on 0.1-cent markets it produced a band ~10x too narrow, so
    orders on those markets never earned rewards. Keep callers using this helper
    instead of multiplying by the tick size.
    """
    d = max_spread_cents * 0.01
    return midpoint - d, midpoint + d
