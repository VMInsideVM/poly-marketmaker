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


def cost_basis_from_buy_fills(buy_fills: list[dict], size: float) -> float | None:
    """当前持仓的加权成本,来自我们的真实买入成交。

    按 ts 从新到旧累计取份额直至覆盖 size,返回这些份额的加权均价。size<=0 或无成交
    -> None;买入总量不足 size(部分份额非 maker 买入/历史截断)-> 按已有份额加权。
    单笔买入全持有时精确等于买入价;多笔时为真实加权(不退化成单笔价)。
    """
    if size <= 0 or not buy_fills:
        return None
    fills = sorted(buy_fills, key=lambda f: f.get("ts", 0) or 0, reverse=True)
    remaining = size
    cost_sum = 0.0
    qty_sum = 0.0
    for f in fills:
        if remaining <= 0:
            break
        fsize = float(f.get("size", 0) or 0)
        if fsize <= 0:
            continue
        take = min(fsize, remaining)
        cost_sum += float(f.get("price", 0) or 0) * take
        qty_sum += take
        remaining -= take
    if qty_sum <= 0:
        return None
    return cost_sum / qty_sum


def take_profit_price(cost: float, best_bid: float | None, tick: float) -> float:
    """止盈卖价 = max(ceil_to_tick(cost), best_bid + tick) 的穿价护栏。

    保证卖价严格高于买一 -> 永远是挂得住的 maker 单,绝不穿价市价清仓。best_bid 为
    None(盘口某侧缺失)时退回 ceil_to_tick(cost)(盘口空时本就无买盘可穿)。
    """
    base = ceil_to_tick(cost, tick)
    if best_bid is not None:
        base = max(base, round(best_bid + tick, 10))
    return base
