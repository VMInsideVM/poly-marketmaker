"""engine/take_profit.py — Pure position-driven take-profit planning (no IO).

The monitor maintains exactly ONE resting SELL per position at the position's
cost price (``avgPrice`` from the Polymarket Data API), rounded UP to the market
tick so we never sell below cost ("原价卖出不亏本金，赚流动性奖励").

This replaces the old per-fill sell, which placed one sell per ``get_trades``
fill event at that event's reported price — splitting a single position into
many orders and trusting per-fill prices that could diverge from the true
average cost (observed: 200 shares resting at 0.38 for a position whose real
avgPrice was 0.30).
"""

import math


def ceil_to_tick(price: float, tick: float) -> float:
    """Smallest tick-aligned price >= ``price`` (sells never go below cost).

    A price already sitting on a tick (within float noise) stays put rather
    than bumping a whole tick up — the Data API avgPrice carries dirt like
    0.30000002 that must be treated as 0.30, not 0.31.
    """
    if tick <= 0:
        return price
    units = price / tick
    nearest = round(units)
    if abs(units - nearest) < 1e-4:  # on a tick (modulo float dirt)
        return round(nearest * tick, 10)
    return round(math.ceil(units) * tick, 10)


def _remaining(o: dict) -> float:
    return float(o.get("original_size", 0) or 0) - float(o.get("size_matched", 0) or 0)


def _price_matches(a: float, b: float, tick: float) -> bool:
    return abs(a - b) < tick / 2


def _size_matches(a: float, b: float) -> bool:
    # Tolerate sub-share drift (partial fills / float) so we don't churn.
    return abs(a - b) <= max(1.0, 0.01 * b)


def plan_take_profit(
    size: float, avg: float, tick: float, existing_sells: list[dict]
) -> dict:
    """Decide how to reconcile resting SELLs for ONE position.

    Returns ``{"action", "price", "size", "cancel_ids"}`` where action is:
    - ``"noop"``   — nothing to sell (no position / no cost basis)
    - ``"keep"``   — exactly one resting sell already at the right price & size
    - ``"replace"``— cancel ``cancel_ids`` and place one sell at ``price`` x ``size``
    """
    if size <= 0 or avg <= 0 or tick <= 0:
        return {"action": "noop", "price": None, "size": 0.0, "cancel_ids": []}
    want = ceil_to_tick(avg, tick)
    ids = [o.get("id") for o in existing_sells]
    remaining = sum(_remaining(o) for o in existing_sells)
    if (
        len(existing_sells) == 1
        and _price_matches(float(existing_sells[0].get("price", 0) or 0), want, tick)
        and _size_matches(remaining, size)
    ):
        return {"action": "keep", "price": want, "size": size, "cancel_ids": []}
    return {"action": "replace", "price": want, "size": size, "cancel_ids": ids}
