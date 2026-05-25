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

    返回 [{price, size, ts, trade_id}, ...](不去重、不依赖 seen_keys)。两种来源:
    - maker:我们挂的买单被吃,成交在 trade.maker_orders 里(按 funder 过滤,
      side==BUY,asset 匹配);price=mo.price,size=mo.matched_amount。
    - taker:我们的 GTC 买单下单瞬间可成交而以 taker 立即成交,成交在 trade 顶层
      (maker_orders 里是对手方,不是我们)。当 trade.trader_side==TAKER 且顶层
      side==BUY 且 asset 匹配:price=trade.price,size=trade.size。
    一笔 trade 对我们而言要么是 maker 要么是 taker,两分支天然互斥,不会重复计入。
    ts 取 trade 顶层 match_time。
    """
    f = (funder or "").lower()
    a = str(asset_id)
    out = []
    for tr in trades:
        trade_id = tr.get("id")
        ts = float(tr.get("match_time", 0) or 0)
        we_are_maker = False
        for mo in tr.get("maker_orders", []) or []:
            if str(mo.get("maker_address", "")).lower() != f:
                continue
            we_are_maker = True  # 我们在这笔 trade 里是 maker -> 不可能同时是 taker
            if str(mo.get("side", "")).upper() != "BUY":
                continue
            if str(mo.get("asset_id", "")) != a:
                continue
            out.append(
                {
                    "price": float(mo.get("price", 0) or 0),
                    "size": float(mo.get("matched_amount", 0) or 0),
                    "ts": ts,
                    "trade_id": trade_id,
                }
            )
        if (
            not we_are_maker
            and str(tr.get("trader_side", "")).upper() == "TAKER"
            and str(tr.get("side", "")).upper() == "BUY"
            and str(tr.get("asset_id", "")) == a
        ):
            out.append(
                {
                    "price": float(tr.get("price", 0) or 0),
                    "size": float(tr.get("size", 0) or 0),
                    "ts": ts,
                    "trade_id": trade_id,
                }
            )
    return out
