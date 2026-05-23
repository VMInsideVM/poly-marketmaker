"""tests/test_place_orders.py"""

from unittest.mock import MagicMock, patch
from engine.manager import WalletWorker


def _worker(api, db, cap=5):
    # The buy-order cap is read live from db.get_settings() at placement time
    # (not the constructor snapshot), so tests seed it on the db mock.
    db.get_settings.return_value = {
        "max_buy_orders_per_wallet": cap,
        "cooldown_minutes": 20,
    }
    db.get_blacklist_ids.return_value = set()
    api.get_user_positions.return_value = []  # default: no held positions
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


def _worker_capped(api, db, cap):
    db.get_settings.return_value = {
        "max_buy_orders_per_wallet": cap,
        "cooldown_minutes": 20,
    }
    db.get_blacklist_ids.return_value = set()
    api.get_user_positions.return_value = []  # default: no held positions
    return WalletWorker(
        api,
        db,
        "0xWALLET",
        {"fill_check_interval_sec": 5, "max_buy_orders_per_wallet": cap},
    )


def test_caps_new_buys_at_per_wallet_max():
    # max_buy_orders_per_wallet caps the TOTAL open buy orders per wallet.
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []  # 0 existing buys
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker_capped(api, db, cap=2)
    markets = [_market(i) for i in range(5)]
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders(markets)
    assert api.place_limit_buy.call_count == 2  # capped at 2, not 5


def test_no_new_buys_when_cap_already_reached():
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = [
        {"id": "o1", "side": "BUY", "asset_id": "x1"},
        {"id": "o2", "side": "BUY", "asset_id": "x2"},
    ]  # already 2 open buys == cap
    api.get_balance.return_value = 1000.0
    worker = _worker_capped(api, db, cap=2)
    markets = [_market(i) for i in range(5)]
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders(markets)
    api.get_orderbook.assert_not_called()
    api.place_limit_buy.assert_not_called()


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


def test_cap_read_live_from_db_not_constructor_snapshot():
    # The cap must be read live from db.get_settings() so a config change takes
    # effect on the next placement without restarting the engine. Constructor
    # snapshot says 10, live DB says 2 -> only 2 buys are placed.
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    api.get_user_positions.return_value = []
    worker = WalletWorker(
        api,
        db,
        "0xWALLET",
        {"fill_check_interval_sec": 5, "max_buy_orders_per_wallet": 10},
    )
    db.get_settings.return_value = {
        "max_buy_orders_per_wallet": 2,
        "cooldown_minutes": 20,
    }
    markets = [_market(i) for i in range(5)]
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders(markets)
    assert api.place_limit_buy.call_count == 2  # live cap 2, not snapshot 10


def test_cancels_excess_buys_over_cap_keeping_oldest():
    # Existing open buys exceed the live cap -> cancel the NEWEST excess
    # (keep the oldest by created_at = best queue priority) down to the cap.
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = [
        {"id": "old1", "side": "BUY", "asset_id": "a1", "created_at": 100},
        {"id": "old2", "side": "BUY", "asset_id": "a2", "created_at": 200},
        {"id": "new1", "side": "BUY", "asset_id": "a3", "created_at": 300},
        {"id": "new2", "side": "BUY", "asset_id": "a4", "created_at": 400},
    ]
    api.get_balance.return_value = 1000.0
    worker = _worker_capped(api, db, cap=2)
    worker.place_orders([])  # no markets; only cap enforcement runs
    api.cancel_orders.assert_called_once()
    cancelled = set(api.cancel_orders.call_args[0][0])
    assert cancelled == {"new1", "new2"}
    api.place_limit_buy.assert_not_called()  # over cap -> no room for new


def test_excess_cancel_logged_to_actions():
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = [
        {"id": "old1", "side": "BUY", "asset_id": "a1", "created_at": 100},
        {"id": "new1", "side": "BUY", "asset_id": "a2", "created_at": 300},
    ]
    api.get_balance.return_value = 1000.0
    worker = _worker_capped(api, db, cap=1)
    worker.place_orders([])
    ats = [c.kwargs.get("action_type") for c in db.record_action.call_args_list]
    assert "cap_cancel_excess" in ats


def test_no_excess_cancel_when_at_cap():
    # Exactly at cap (not over) -> existing buys are kept untouched.
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = [
        {"id": "o1", "side": "BUY", "asset_id": "a1", "created_at": 100},
        {"id": "o2", "side": "BUY", "asset_id": "a2", "created_at": 200},
    ]
    api.get_balance.return_value = 1000.0
    worker = _worker_capped(api, db, cap=2)
    worker.place_orders([])
    api.cancel_orders.assert_not_called()


def test_excess_cancel_ignores_sell_orders():
    # Only BUY orders count toward the buy cap; resting SELLs (take-profit) are
    # never cancelled by cap enforcement.
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = [
        {"id": "b1", "side": "BUY", "asset_id": "a1", "created_at": 100},
        {"id": "s1", "side": "SELL", "asset_id": "a2", "created_at": 50},
        {"id": "b2", "side": "BUY", "asset_id": "a3", "created_at": 200},
    ]
    api.get_balance.return_value = 1000.0
    worker = _worker_capped(api, db, cap=1)
    worker.place_orders([])
    cancelled = set(api.cancel_orders.call_args[0][0])
    assert cancelled == {"b2"}  # newest BUY only; SELL s1 untouched


def test_order_size_uses_full_balance():
    # 每笔买单按全部可用余额下单：floor(balance/price),不是 rewards_min_size。
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    with patch("engine.strategy.determine_order_price", return_value=0.50):
        worker.place_orders([_market(0)])
    api.place_limit_buy.assert_called_once()
    assert api.place_limit_buy.call_args.args[2] == 2000  # floor(1000/0.50)


def test_balance_not_decremented_across_markets():
    # 同一笔余额垫付所有挂单,跨市场不递减 —— 每个市场都拿到全额份数。
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    markets = [_market(i) for i in range(3)]
    with patch("engine.strategy.determine_order_price", return_value=0.50):
        worker.place_orders(markets)
    sizes = [c.args[2] for c in api.place_limit_buy.call_args_list]
    assert sizes == [2000, 2000, 2000]


def test_skip_when_full_balance_below_min_reward_size():
    # 全额都买不够 rewards_min_size 份时跳过该市场(挂更少拿不到奖励)。
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 3.0  # floor(3.0/0.50)=6 份
    worker = _worker(api, db)
    m = _market(0)
    m["rewards_min_size"] = 10  # 需要 >=10,只买得起 6 -> 跳过
    with patch("engine.strategy.determine_order_price", return_value=0.50):
        worker.place_orders([m])
    api.place_limit_buy.assert_not_called()


def test_place_buy_action_records_full_size():
    # 动作记录里 size 是真实下单的全额份数,不是扫描时的最小份额。
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    with patch("engine.strategy.determine_order_price", return_value=0.50):
        worker.place_orders([_market(0)])
    pb = next(
        c
        for c in db.record_action.call_args_list
        if c.kwargs.get("action_type") == "place_buy"
    )
    assert pb.kwargs["size"] == 2000


def test_order_size_floors_on_inexact_division():
    # 余额除不尽时向下取整(floor),不是四舍五入 —— 取整向上会在成交时超出余额。
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    with patch("engine.strategy.determine_order_price", return_value=0.30):
        worker.place_orders([_market(0)])
    api.place_limit_buy.assert_called_once()
    assert (
        api.place_limit_buy.call_args.args[2] == 3333
    )  # floor(1000/0.30)=floor(3333.33)


def test_skips_blacklisted_market():
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    db.get_blacklist_ids.return_value = {"m0"}  # m0 在黑名单
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders([_market(0)])
    api.place_limit_buy.assert_not_called()


def test_non_blacklisted_market_still_placed():
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    db.get_blacklist_ids.return_value = {"mOTHER"}  # 别的市场在黑名单
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders([_market(0)])
    api.place_limit_buy.assert_called_once()


def test_skips_market_with_existing_position():
    # 该 market(condition_id)已有持仓 -> 不再为它挂买单;非持仓的合格市场照常挂。
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    api.get_user_positions.return_value = [
        {"conditionId": "m0", "size": 100.0, "asset": "t0"}
    ]
    markets = [_market(0), _market(1)]
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders(markets)
    placed_tokens = [c.args[0] for c in api.place_limit_buy.call_args_list]
    assert "t0" not in placed_tokens  # m0 已持仓 -> 跳过
    assert "t1" in placed_tokens  # m1 未持仓 -> 照常挂


def test_no_placement_when_positions_unavailable():
    # 取不到持仓(Data API 报错) -> 该轮不挂任何单(保守兜底)。
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    api.get_user_positions.side_effect = Exception("Data API down")
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders([_market(0)])  # must not raise
    api.place_limit_buy.assert_not_called()
