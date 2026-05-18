# tests/test_fills.py
from engine.fills import select_new_trades


def _t(tid, side="BUY", size="10", ts=100):
    return {
        "id": tid,
        "side": side,
        "size": size,
        "trader_side": "MAKER",
        "price": "0.5",
        "asset_id": "A",
        "market": "M",
        "match_time": ts,
    }


def test_returns_unseen_buy_trades_only():
    trades = [_t("t1"), _t("t2"), _t("t3", side="SELL")]
    seen = {"t1"}
    new = select_new_trades(trades, seen, ts_field="match_time")
    assert [t["id"] for t in new] == ["t2"]


def test_empty_when_all_seen():
    trades = [_t("t1"), _t("t2")]
    assert select_new_trades(trades, {"t1", "t2"}, ts_field="match_time") == []


def test_sorted_by_timestamp_ascending():
    trades = [_t("t2", ts=200), _t("t1", ts=100)]
    new = select_new_trades(trades, set(), ts_field="match_time")
    assert [t["id"] for t in new] == ["t1", "t2"]
