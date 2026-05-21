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


def test_records_place_buy_action_on_success():
    # A successful placement is logged to the actions table as "place_buy" so
    # the operations log (操作记录) covers buy orders too, not just sells.
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders([_market(0)])
    api.place_limit_buy.assert_called_once()
    calls = [c for c in db.record_action.call_args_list]
    assert any(c.kwargs.get("action_type") == "place_buy" for c in calls)
    pb = next(c for c in calls if c.kwargs.get("action_type") == "place_buy")
    assert pb.kwargs["side"] == "买入"
    assert pb.kwargs["price"] == 0.40
    assert pb.kwargs["market_id"] == "m0"


def test_no_place_buy_action_when_placement_fails():
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    api.place_limit_buy.side_effect = Exception("rejected")
    worker = _worker(api, db)
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders([_market(0)])  # must not raise
    ats = [c.kwargs.get("action_type") for c in db.record_action.call_args_list]
    assert "place_buy" not in ats


def test_skips_market_when_balance_below_min_cost():
    # Each wallet gates on its own balance vs the recorded min_cost BEFORE the
    # detailed strategy (so it doesn't even fetch the orderbook).
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_balance.return_value = 100.0
    worker = _worker(api, db)
    m = _market(0)
    m["min_cost"] = 500.0  # 100 balance < 500 threshold
    worker.place_orders([m])
    api.get_orderbook.assert_not_called()
    api.place_limit_buy.assert_not_called()


def test_places_when_balance_above_min_cost():
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    m = _market(0)
    m["min_cost"] = 50.0  # 1000 balance > 50 threshold
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders([m])
    api.place_limit_buy.assert_called_once()


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
