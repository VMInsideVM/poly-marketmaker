"""tests/test_monitor.py — API-driven OrderMonitor unit tests."""

import pytest
from unittest.mock import MagicMock, call, patch
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
    # funder address via api.client.funder
    api.client.funder = "0xFUNDER"
    monitor = OrderMonitor(api, db, "0xABC")
    return monitor, api, db


# ---------------------------------------------------------------------------
# Step 1: check_buy_orders — fill detection via get_trades
# ---------------------------------------------------------------------------


class TestCheckBuyOrders:
    def test_filled_order_triggers_sell(self):
        monitor, api, db = _make_monitor()
        trade = {
            "id": "trade1",
            "maker_orders": [
                {
                    "order_id": "ord1",
                    "asset_id": "tok1",
                    "matched_amount": "1000",
                    "price": "0.25",
                    "side": "BUY",
                    "maker_address": "0xFUNDER",
                    "market": "mkt1",
                    "match_time": "1000000",
                }
            ],
        }
        api.get_trades.return_value = [trade]
        api.place_limit_sell.return_value = {"orderID": "sell1"}

        with patch("engine.monitor.select_new_buy_fills") as mock_fills:
            mock_fills.return_value = [
                {
                    "trade_id": "trade1",
                    "order_id": "ord1",
                    "asset_id": "tok1",
                    "price": 0.25,
                    "size": 1000.0,
                    "market": "mkt1",
                    "ts": 1000000.0,
                }
            ]
            monitor.check_buy_orders()

        api.place_limit_sell.assert_called_with("tok1", 0.25, 1000.0)
        db.record_trade.assert_called_once()
        db.set_cooldown.assert_called_with("0xABC", "mkt1", 20)
        api.cancel_orders.assert_called_with(["ord1"])

    def test_partial_fill_places_sell_for_filled_portion(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        api.place_limit_sell.return_value = {"orderID": "sell1"}

        with patch("engine.monitor.select_new_buy_fills") as mock_fills:
            mock_fills.return_value = [
                {
                    "trade_id": "trade2",
                    "order_id": "ord1",
                    "asset_id": "tok1",
                    "price": 0.25,
                    "size": 600.0,
                    "market": "mkt1",
                    "ts": 1000001.0,
                }
            ]
            monitor.check_buy_orders()

        api.place_limit_sell.assert_called_with("tok1", 0.25, 600.0)

    def test_unfilled_order_no_action(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []

        with patch("engine.monitor.select_new_buy_fills") as mock_fills:
            mock_fills.return_value = []
            monitor.check_buy_orders()

        api.place_limit_sell.assert_not_called()

    def test_get_trades_failure_logs_and_returns(self):
        monitor, api, db = _make_monitor()
        api.get_trades.side_effect = Exception("network error")

        monitor.check_buy_orders()  # must not raise

        api.place_limit_sell.assert_not_called()

    def test_seen_fill_keys_updated(self):
        """Watermark and seen-key are updated after each fill."""
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        api.place_limit_sell.return_value = {}

        with patch("engine.monitor.select_new_buy_fills") as mock_fills:
            mock_fills.return_value = [
                {
                    "trade_id": "t1",
                    "order_id": "o1",
                    "asset_id": "tok1",
                    "price": 0.5,
                    "size": 100.0,
                    "market": "mkt1",
                    "ts": 9999.0,
                }
            ]
            monitor.check_buy_orders()

        assert ("t1", "o1") in monitor._seen_fill_keys
        assert monitor._after_ts == 9999.0


# ---------------------------------------------------------------------------
# Step 2: check_stop_loss — Data API positions
# ---------------------------------------------------------------------------


class TestStopLoss:
    def test_triggers_stop_loss_when_price_drops(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        # Position: bought at 0.30, now at 0.24 (20% drop > 15% threshold)
        api.get_user_positions.return_value = [
            {
                "asset": "tok1",
                "size": 1000.0,
                "avgPrice": 0.30,
                "curPrice": 0.24,
                "conditionId": "mkt1",
            }
        ]
        api.get_open_orders.return_value = [
            {"id": "sell1", "asset_id": "tok1", "side": "SELL"},
        ]

        with patch("engine.monitor.stop_loss_triggered", return_value=True):
            monitor.check_stop_loss()

        api.cancel_orders.assert_called_with(["sell1"])
        api.place_market_sell.assert_called_with("tok1", 1000.0)
        db.record_trade.assert_called_once()

    def test_no_stop_loss_when_price_stable(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        api.get_user_positions.return_value = [
            {
                "asset": "tok1",
                "size": 1000.0,
                "avgPrice": 0.30,
                "curPrice": 0.28,
                "conditionId": "mkt1",
            }
        ]
        api.get_open_orders.return_value = []

        with patch("engine.monitor.stop_loss_triggered", return_value=False):
            monitor.check_stop_loss()

        api.cancel_orders.assert_not_called()
        api.place_market_sell.assert_not_called()

    def test_positions_api_failure_skips_stop_loss(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.side_effect = Exception("Data API down")

        monitor.check_stop_loss()  # must not raise

        api.place_market_sell.assert_not_called()

    def test_zero_size_position_skipped(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [
            {
                "asset": "tok1",
                "size": 0.0,
                "avgPrice": 0.30,
                "curPrice": 0.10,
                "conditionId": "mkt1",
            }
        ]
        api.get_open_orders.return_value = []

        monitor.check_stop_loss()

        api.place_market_sell.assert_not_called()


# ---------------------------------------------------------------------------
# Step 3: check_sell_orders — strategy compliance on resting buys
# ---------------------------------------------------------------------------


class TestCheckSellOrders:
    def _ob(self, best_bid="0.48", best_ask="0.52", tick="0.01"):
        return {
            "bids": [{"price": best_bid, "size": "1000"}],
            "asks": [{"price": best_ask, "size": "1000"}],
            "tick_size": tick,
        }

    def test_keep_compliant_order(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()

        with patch("engine.monitor.needs_replace", return_value="keep"):
            monitor.check_sell_orders()

        api.cancel_orders.assert_not_called()

    def test_replace_non_compliant_order(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
                "neg_risk": False,
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.place_limit_buy.return_value = {"orderID": "o2"}

        with patch("engine.monitor.needs_replace", return_value="replace"), patch(
            "engine.monitor.determine_order_price", return_value=0.48
        ):
            monitor.check_sell_orders()

        api.cancel_orders.assert_called_with(["o1"])
        api.place_limit_buy.assert_called_once()

    def test_cancel_non_compliant_order(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()

        with patch("engine.monitor.needs_replace", return_value="cancel"), patch(
            "engine.monitor.determine_order_price", return_value=0.48
        ):
            monitor.check_sell_orders()

        api.cancel_orders.assert_called_with(["o1"])
        api.place_limit_buy.assert_not_called()

    def test_skip_sell_side_orders(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "SELL",
                "asset_id": "tok1",
                "size_matched": "0",
                "price": "0.60",
            }
        ]

        monitor.check_sell_orders()

        api.get_orderbook.assert_not_called()

    def test_skip_partially_filled_buy(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "size_matched": "100",
                "price": "0.48",
            }
        ]

        monitor.check_sell_orders()

        api.get_orderbook.assert_not_called()

    def test_get_open_orders_failure_returns_gracefully(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.side_effect = Exception("timeout")

        monitor.check_sell_orders()  # must not raise

        api.get_orderbook.assert_not_called()

    def test_empty_orderbook_skips_compliance(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}

        monitor.check_sell_orders()

        api.cancel_orders.assert_not_called()
