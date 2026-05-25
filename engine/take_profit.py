"""engine/take_profit.py — Pure position-driven take-profit planning (no IO).

The monitor maintains exactly ONE resting SELL per position. The sell price is
``take_profit_price(cost, best_bid, tick)`` = ``max(ceil_to_tick(cost), best_bid
+ tick)``: at/above the real cost so we never sell below it, and strictly above
the best bid so the order always rests as a maker and never crosses the book
(原价不亏本金、不穿价市价清仓、赚流动性奖励).

``cost`` is the position's weighted-average cost computed from our real CLOB
``get_trades`` buy fills (``cost_basis_from_buy_fills``), NOT the Polymarket Data
API ``avgPrice`` — the Data API avgPrice was observed to glitch on freshly
opened positions (a real 0.28 buy read as ~0.21, dumping the position at
market). A single resting sell also replaces the older per-fill sells that split
one position into many orders priced off divergent per-fill data.
"""

import math
import time


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
    size: float, want_price: float, tick: float, existing_sells: list[dict]
) -> dict:
    """对一个持仓的 SELL 们做对账,使恰好一笔卖单挂在 want_price、覆盖整个 size。

    want_price 由调用方用 take_profit_price(cost, best_bid, tick) 预先算好(已对齐
    tick、已含穿价护栏),本函数不再加工价格。返回 {"action","price","size",
    "cancel_ids"}:noop / keep / replace。
    """
    if size <= 0 or want_price is None or want_price <= 0 or tick <= 0:
        return {"action": "noop", "price": None, "size": 0.0, "cancel_ids": []}
    want = want_price
    ids = [o.get("id") for o in existing_sells]
    remaining = sum(_remaining(o) for o in existing_sells)
    if (
        len(existing_sells) == 1
        and _price_matches(float(existing_sells[0].get("price", 0) or 0), want, tick)
        and _size_matches(remaining, size)
    ):
        return {"action": "keep", "price": want, "size": size, "cancel_ids": []}
    return {"action": "replace", "price": want, "size": size, "cancel_ids": ids}


def cost_basis_with_lots(buy_fills: list[dict], size: float):
    """当前持仓的加权成本 + 被消耗的逐笔买入明细,来自我们的真实买入成交。

    算法同 cost_basis_from_buy_fills:按 ts 从新到旧累计取份额直至覆盖 size。额外
    返回每笔被消耗份额 {price, take(本笔实取量), ts, trade_id},供卖单理由溯源。
    返回 (cost_or_None, lots)。size<=0 或无成交 -> (None, [])。
    """
    if size <= 0 or not buy_fills:
        return None, []
    fills = sorted(buy_fills, key=lambda f: f.get("ts", 0) or 0, reverse=True)
    remaining = size
    cost_sum = 0.0
    qty_sum = 0.0
    lots: list[dict] = []
    for f in fills:
        if remaining <= 0:
            break
        fsize = float(f.get("size", 0) or 0)
        if fsize <= 0:
            continue
        take = min(fsize, remaining)
        price = float(f.get("price", 0) or 0)
        cost_sum += price * take
        qty_sum += take
        remaining -= take
        lots.append(
            {
                "price": price,
                "take": take,
                "ts": float(f.get("ts", 0) or 0),
                "trade_id": f.get("trade_id", ""),
            }
        )
    if qty_sum <= 0:
        return None, []
    return cost_sum / qty_sum, lots


def cost_basis_from_buy_fills(buy_fills: list[dict], size: float) -> float | None:
    """当前持仓的加权成本(仅成本,不含明细)。见 cost_basis_with_lots。"""
    return cost_basis_with_lots(buy_fills, size)[0]


def take_profit_price(cost: float, best_bid: float | None, tick: float) -> float:
    """止盈卖价 = max(ceil_to_tick(cost), best_bid + tick) 的穿价护栏。

    保证卖价严格高于买一 -> 永远是挂得住的 maker 单,绝不穿价市价清仓。best_bid 为
    None(盘口某侧缺失)时退回 ceil_to_tick(cost)(盘口空时本就无买盘可穿)。
    """
    base = ceil_to_tick(cost, tick)
    if best_bid is not None:
        base = max(base, round(best_bid + tick, 10))
    return base


_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def _fmt_share(x: float) -> str:
    """份额去掉无意义的 .0;非整数保留两位小数。"""
    return str(int(round(x))) if abs(x - round(x)) < 1e-9 else f"{x:.2f}"


def _short_tid(tid) -> str:
    tid = str(tid or "")
    return tid if len(tid) <= 12 else f"{tid[:6]}..{tid[-4:]}"


def describe_cost_basis(cost, lots: list[dict], max_lots: int = 6) -> str:
    """成本构成的中文片段(纯函数),供止盈/止损卖单理由引用。

    lots 来自 cost_basis_with_lots,按 ts 正序(最早->最新)逐笔列:
    "①时间 价格×份额股 [trade 缩写id]"。超过 max_lots 笔时列前 max_lots 笔
    + "…等共N笔"。时间用本地时区 MM-DD HH:MM。cost 为 None 时给降级文案。
    """
    n = len(lots)
    if cost is None or n == 0:
        return "成本=无（无买入成交）"
    ordered = sorted(lots, key=lambda l: l.get("ts", 0) or 0)
    total_take = sum(float(l.get("take", 0) or 0) for l in ordered)
    parts = []
    for i, l in enumerate(ordered[:max_lots]):
        mark = _CIRCLED[i] if i < len(_CIRCLED) else f"{i + 1}."
        t = time.strftime("%m-%d %H:%M", time.localtime(float(l.get("ts", 0) or 0)))
        parts.append(
            f"{mark}{t} {float(l.get('price', 0) or 0):.4f}"
            f"×{_fmt_share(float(l.get('take', 0) or 0))}股 "
            f"[trade {_short_tid(l.get('trade_id', ''))}]"
        )
    more = f" …等共{n}笔" if n > max_lots else ""
    return (
        f"成本={cost:.4f}（加权自{n}笔买入成交："
        f"{' '.join(parts)}{more} 共取{_fmt_share(total_take)}股）"
    )
