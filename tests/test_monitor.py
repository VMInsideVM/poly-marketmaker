"""tests/test_monitor.py"""

import pytest
from unittest.mock import MagicMock, call
from engine.monitor import OrderMonitor


def _make_monitor(settings=None):
    api = MagicMock()
    db = MagicMock()
    default_settings = {
        "stop_loss_pct": 15.0,
        "cooldown_minutes": 20,
    }
    if settings:
        default_settings.update(settings)
    db.get_settings.return_value = default_settings
    monitor = OrderMonitor(api, db, "0xABC")
    return monitor, api, db


class TestCheckBuyOrders:
    def test_filled_order_triggers_sell(self):
        monitor, api, db = _make_monitor()
        db.get_open_buy_orders.return_value = [
            {
                "order_id": "ord1",
                "market_id": "mkt1",
                "token_id": "tok1",
                "market_name": "Test",
                "price": 0.25,
                "size": 1000,
            }
        ]
        api.get_order.return_value = {"status": "MATCHED", "size_matched": "1000"}
        api.place_limit_sell.return_value = {"orderID": "sell1"}

        monitor.check_buy_orders()

        db.update_order_status.assert_called_with("ord1", "filled")
        api.place_limit_sell.assert_called_with("tok1", 0.25, 1000)
        db.record_position.assert_called_once()
        db.set_cooldown.assert_called_with("0xABC", "mkt1", 20)

    def test_partial_fill_places_sell_for_filled_portion(self):
        monitor, api, db = _make_monitor()
        db.get_open_buy_orders.return_value = [
            {
                "order_id": "ord1",
                "market_id": "mkt1",
                "token_id": "tok1",
                "market_name": "Test",
                "price": 0.25,
                "size": 1000,
            }
        ]
        api.get_order.return_value = {"status": "OPEN", "size_matched": "600"}
        api.place_limit_sell.return_value = {"orderID": "sell1"}

        monitor.check_buy_orders()

        # Should place sell for 600, keep order open
        api.place_limit_sell.assert_called_with("tok1", 0.25, 600)
        db.update_order_status.assert_not_called()

    def test_unfilled_order_no_action(self):
        monitor, api, db = _make_monitor()
        db.get_open_buy_orders.return_value = [
            {
                "order_id": "ord1",
                "market_id": "mkt1",
                "token_id": "tok1",
                "market_name": "Test",
                "price": 0.25,
                "size": 1000,
            }
        ]
        api.get_order.return_value = {"status": "OPEN", "size_matched": "0"}

        monitor.check_buy_orders()

        api.place_limit_sell.assert_not_called()


class TestStopLoss:
    def test_triggers_stop_loss_when_price_drops(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        db.get_positions.return_value = [
            {
                "id": 1,
                "token_id": "tok1",
                "market_id": "mkt1",
                "market_name": "Test",
                "buy_price": 0.30,
                "size": 1000,
                "sell_order_id": "sell1",
            }
        ]
        # Price dropped to 0.24 = 20% drop, exceeds 15% threshold
        api.get_last_trade_price.return_value = 0.24

        monitor.check_stop_loss()

        api.cancel_order.assert_called_with("sell1")
        api.place_market_sell.assert_called_with("tok1", 1000)
        db.close_position.assert_called_with(1)

    def test_no_stop_loss_when_price_stable(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        db.get_positions.return_value = [
            {
                "id": 1,
                "token_id": "tok1",
                "market_id": "mkt1",
                "market_name": "Test",
                "buy_price": 0.30,
                "size": 1000,
                "sell_order_id": "sell1",
            }
        ]
        # Price at 0.28 = 6.7% drop, within threshold
        api.get_last_trade_price.return_value = 0.28

        monitor.check_stop_loss()

        api.cancel_order.assert_not_called()
