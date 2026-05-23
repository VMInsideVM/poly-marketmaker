# engine/fills.py
"""Pure fill-detection logic (no network/IO).

Polymarket get_trades: top-level fields are the taker/aggregate view. OUR
fills are in trade["maker_orders"], filtered by maker_address == our funder.
A resting BUY of ours that got filled appears as a maker_orders entry with
side == "BUY". Dedup is per (trade_id, order_id).
"""


def select_new_buy_fills(trades: list[dict], funder: str, seen_keys: set) -> list[dict]:
    """Flatten get_trades into our unseen BUY fill events, oldest-first.

    Each event: {trade_id, order_id, asset_id, price, size, market, ts}
    where price/size/asset_id come from the matching maker_orders entry and
    market/ts come from the trade's top-level fields. seen_keys holds
    (trade_id, order_id) tuples already processed.
    """
    f = (funder or "").lower()
    events = []
    for tr in trades:
        trade_id = tr.get("id")
        market = tr.get("market", "")
        ts = float(tr.get("match_time", 0) or 0)
        for mo in tr.get("maker_orders", []) or []:
            if str(mo.get("maker_address", "")).lower() != f:
                continue
            if str(mo.get("side", "")).upper() != "BUY":
                continue
            order_id = mo.get("order_id")
            if (trade_id, order_id) in seen_keys:
                continue
            events.append(
                {
                    "trade_id": trade_id,
                    "order_id": order_id,
                    "asset_id": mo.get("asset_id", ""),
                    "price": float(mo.get("price", 0) or 0),
                    "size": float(mo.get("matched_amount", 0) or 0),
                    "market": market,
                    "ts": ts,
                }
            )
    events.sort(key=lambda e: e["ts"])
    return events


def extract_buy_fills(trades: list[dict], funder: str, asset_id: str) -> list[dict]:
    """挑出我们在某 token 上的全部 BUY 成交,用于算加权成本。

    返回 [{price, size, ts}, ...](不去重、不依赖 seen_keys)。只看 maker_orders
    里 maker_address==funder 且 side==BUY 且 asset_id 匹配的条目;price 取该 maker
    条目的 price,size 取 matched_amount,ts 取 trade 顶层 match_time。
    """
    f = (funder or "").lower()
    a = str(asset_id)
    out = []
    for tr in trades:
        ts = float(tr.get("match_time", 0) or 0)
        for mo in tr.get("maker_orders", []) or []:
            if str(mo.get("maker_address", "")).lower() != f:
                continue
            if str(mo.get("side", "")).upper() != "BUY":
                continue
            if str(mo.get("asset_id", "")) != a:
                continue
            out.append(
                {
                    "price": float(mo.get("price", 0) or 0),
                    "size": float(mo.get("matched_amount", 0) or 0),
                    "ts": ts,
                }
            )
    return out
