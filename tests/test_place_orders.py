"""tests/test_place_orders.py"""

from unittest.mock import MagicMock, patch
from engine.manager import WalletWorker


def _worker(api, db):
    return WalletWorker(api, db, "0xWALLET", {"fill_check_interval_sec": 5})


def _market(i):
    return {
        "market_id": f"m{i}",
        "market_name": f"Market {i}",
        "token_id": f"t{i}",
        "outcome": "YES",
        "order_size": 10,
        "rewards_max_spread": 2,
        "neg_risk": False,
        "market_competitiveness": 0.0,
    }


def _ok_orderbook():
    return {
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.42", "size": "100"}],
        "tick_size": "0.01",
    }


def test_limit_stops_after_n_successful_placements():
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    markets = [_market(i) for i in range(5)]
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders(markets, limit=3)
    assert api.place_limit_buy.call_count == 3


def test_no_limit_places_on_all_markets_regression():
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    markets = [_market(i) for i in range(5)]
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders(markets)
    assert api.place_limit_buy.call_count == 5


def test_skipped_markets_do_not_count_toward_limit():
    api = MagicMock()
    db = MagicMock()
    # m0 in cooldown (skip, no count); m1 price None (skip, no count);
    # m2..m6 placeable. limit=3 -> place on m2,m3,m4 then stop.
    db.is_in_cooldown.side_effect = lambda w, mid: mid == "m0"
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    markets = [_market(i) for i in range(7)]

    # m0 is skipped by cooldown (determine_order_price never called for it).
    # determine_order_price is called for m1.. — first returns None (skip,
    # no count), the rest return a valid price.
    prices = [None, 0.40, 0.40, 0.40, 0.40, 0.40]
    with patch("engine.strategy.determine_order_price", side_effect=prices):
        worker.place_orders(markets, limit=3)
    assert api.place_limit_buy.call_count == 3
