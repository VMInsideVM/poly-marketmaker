"""engine/take_profit.py — Pure position-driven exit planning (no IO).

``cost`` is the position's weighted-average cost reconstructed from our real CLOB
``get_trades`` fills (``position_cost_with_lots``: replay buys/sells, FIFO-net,
remaining lots = current position), NOT the Polymarket Data
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
    than bumping a whole tick up — a get_trades weighted cost carries float
    dirt like 0.30000002 that must be treated as 0.30, not 0.31.
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

    want_price 由调用方预先算好(已对齐 tick、已含穿价护栏),本函数不再加工价格。返回 {"action","price","size",
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


def position_cost_with_lots(fills: list[dict], size: float):
    """当前持仓的加权成本 + 剩余逐笔持仓明细,严格由成交流(买入∪卖出)重建。

    按 ts 正序回放我们在该 token 的全部成交:买入入 FIFO 队列,卖出从最早一笔开始
    抵消;回放完后队列里剩下的就是当前真实持仓,其加权均价即成本。已卖出的旧买单会被
    抵消、移出队列,绝不污染成本(老 bug:不看卖出、从新到旧凑 size,会把早已平掉的
    旧买入当成本,导致按错价亏本市价卖)。重建出的剩余持仓数量必须与 Data API 持仓
    size 吻合(容差 max(1.0, 0.01*size)),否则成本不可信(成交流相对 Data API 滞后、
    或外部转入的仓)-> (None, []),由调用方走"跳过+⚠️告警"、下个 tick 自愈。
    返回 (cost_or_None, lots);lots 形如 {price, take(剩余持仓量), ts, trade_id}。
    """
    if size <= 0 or not fills:
        return None, []
    ordered = sorted(fills, key=lambda f: f.get("ts", 0) or 0)
    lots: list[dict] = []  # FIFO 存货队列,最早的在前
    for f in ordered:
        fsize = float(f.get("size", 0) or 0)
        if fsize <= 0:
            continue
        side = str(f.get("side", "")).upper()
        if side == "BUY":
            lots.append(
                {
                    "price": float(f.get("price", 0) or 0),
                    "remaining": fsize,
                    "ts": float(f.get("ts", 0) or 0),
                    "trade_id": f.get("trade_id", ""),
                }
            )
        elif side == "SELL":
            qty = fsize
            while qty > 1e-9 and lots:
                lot = lots[0]
                take = min(lot["remaining"], qty)
                lot["remaining"] -= take
                qty -= take
                if lot["remaining"] <= 1e-9:
                    lots.pop(0)
    recon = sum(l["remaining"] for l in lots)
    if recon <= 0:
        return None, []
    if abs(recon - size) > max(1.0, 0.01 * size):
        return None, []
    cost_sum = sum(l["price"] * l["remaining"] for l in lots)
    result_lots = [
        {
            "price": l["price"],
            "take": l["remaining"],
            "ts": l["ts"],
            "trade_id": l["trade_id"],
        }
        for l in lots
    ]
    return cost_sum / recon, result_lots


_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def _fmt_share(x: float) -> str:
    """份额去掉无意义的 .0;非整数保留两位小数。"""
    return str(int(round(x))) if abs(x - round(x)) < 1e-9 else f"{x:.2f}"


def _short_tid(tid) -> str:
    tid = str(tid or "")
    return tid if len(tid) <= 12 else f"{tid[:6]}..{tid[-4:]}"


def describe_cost_basis(cost, lots: list[dict], max_lots: int = 6) -> str:
    """成本构成的中文片段(纯函数),供止盈/止损卖单理由引用。

    lots 来自 position_cost_with_lots,按 ts 正序(最早->最新)逐笔列:
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


def plan_exit(
    cost, best_bid, best_ask, tick, theta_loss, theta_stop, case_a_mode, size
):
    """三段式离场决策(v4 §7)。theta_loss/theta_stop 为价位单位(=¢/100)。

    返回 {"tier","action","price","size"};action ∈ {"rest","market","sweep","noop"}。
    cost>0、size>0 由调用方在调用前保证(成本取不到 -> 裸奔跳过,不进此函数)。
    - rest 价 = 卖一(best_ask),无则回退 ceil_to_tick(cost)。
    - sweep 价 = ceil_to_tick(最低卖出价)(限价卖向上取整,绝不卖穿到下限以下)。
    """

    def _rest_price():
        if best_ask is not None and best_ask > 0:
            return best_ask
        return ceil_to_tick(cost, tick)

    if best_bid is None:
        if best_ask is not None and best_ask > 0:
            return {
                "tier": "B_park",
                "action": "rest",
                "price": _rest_price(),
                "size": size,
            }
        return {"tier": "none", "action": "noop", "price": None, "size": 0.0}

    if cost <= best_bid:
        if case_a_mode == "market":
            return {"tier": "A", "action": "market", "price": None, "size": size}
        return {"tier": "A", "action": "rest", "price": _rest_price(), "size": size}

    loss = cost - best_bid
    if loss >= theta_stop:
        return {"tier": "B0", "action": "market", "price": None, "size": size}
    floor = cost - theta_loss
    if best_bid >= floor:
        return {
            "tier": "B_sweep",
            "action": "sweep",
            "price": ceil_to_tick(floor, tick),
            "size": size,
        }
    return {"tier": "B_park", "action": "rest", "price": _rest_price(), "size": size}
