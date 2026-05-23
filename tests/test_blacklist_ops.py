"""tests/test_blacklist_ops.py"""

from engine.blacklist_ops import buy_order_ids_for_condition


def _orders():
    return [
        {"id": "b1", "side": "BUY", "market": "0xc1"},
        {"id": "s1", "side": "SELL", "market": "0xc1"},  # SELL: 不挑
        {"id": "b2", "side": "BUY", "market": "0xOTHER"},  # 别的市场: 不挑
        {"id": "b3", "side": "BUY", "market": "0xc1"},
    ]


def test_picks_only_matching_buys():
    assert buy_order_ids_for_condition(_orders(), "0xc1") == ["b1", "b3"]


def test_excludes_sell_orders():
    assert "s1" not in buy_order_ids_for_condition(_orders(), "0xc1")


def test_excludes_other_markets():
    assert "b2" not in buy_order_ids_for_condition(_orders(), "0xc1")


def test_no_match_returns_empty():
    assert buy_order_ids_for_condition(_orders(), "0xNONE") == []


def test_skips_orders_without_id():
    orders = [{"side": "BUY", "market": "0xc1"}]  # 无 id
    assert buy_order_ids_for_condition(orders, "0xc1") == []
