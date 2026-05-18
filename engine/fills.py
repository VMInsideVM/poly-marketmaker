"""Pure fill-detection logic (no network/IO)."""


def is_buy_side_trade(trade: dict) -> bool:
    """True if this trade represents a fill of one of our maker BUY orders.

    `side` reflects the trade's taker side per Polymarket; for our resting
    maker BUY the relevant fills are BUY-side. Field names verified in Task 0;
    keep this the single place that encodes the rule.
    """
    return str(trade.get("side", "")).upper() == "BUY"


def select_new_trades(trades: list[dict], seen_ids: set, ts_field: str) -> list[dict]:
    """Return unseen buy-side trades, sorted oldest-first by ts_field."""
    new = [t for t in trades if t.get("id") not in seen_ids and is_buy_side_trade(t)]
    new.sort(key=lambda t: float(t.get(ts_field, 0) or 0))
    return new
