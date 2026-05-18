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
