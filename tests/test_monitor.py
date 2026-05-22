"""tests/test_monitor.py — API-driven OrderMonitor unit tests."""

import logging
import pytest
from unittest.mock import MagicMock, call, patch
from engine.monitor import OrderMonitor
from engine.monitor_status import get_snapshot, clear_snapshot


def _make_monitor(settings=None):
    api = MagicMock()
    db = MagicMock()
    default_settings = {
        "stop_loss_pct": 15.0,
        "cooldown_minutes": 20,
        "rewards_cache_ttl_sec": 600,
    }
    if settings:
        default_settings.update(settings)
    db.get_settings.return_value = default_settings
    # funder address via api.get_funder()
    api.get_funder.return_value = "0xFUNDER"
    monitor = OrderMonitor(api, db, "0xABC")
    return monitor, api, db


# ---------------------------------------------------------------------------
# Step 1: check_buy_orders — fill detection via get_trades
# ---------------------------------------------------------------------------


class TestCheckBuyOrders:
    def test_filled_order_sets_cooldown_and_cancels_remainder(self):
        # Take-profit is position-driven (check_take_profit) and buy "history"
        # is the live Data API position now, so Step 1 writes NO trade row — it
        # only sets the cooldown and cancels the filled buy's remainder.
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []

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

        api.place_limit_sell.assert_not_called()  # no per-fill sell anymore
        db.record_trade.assert_not_called()  # no trades-table write anymore
        db.set_cooldown.assert_called_with("0xABC", "mkt1", 20)
        api.cancel_orders.assert_called_with(["ord1"])
        action_types = [
            c.kwargs["action_type"] for c in db.record_action.call_args_list
        ]
        assert "take_profit_sell" not in action_types
        assert "cancel_remainder" in action_types

    def test_partial_fill_writes_no_trade(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []

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

        api.place_limit_sell.assert_not_called()
        db.record_trade.assert_not_called()

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

    def test_handle_fill_exception_still_marks_seen(self):
        """If a fill handler op raises, seen-key and watermark must still update."""
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        db.set_cooldown.side_effect = Exception("boom")

        with patch("engine.monitor.select_new_buy_fills") as mock_fills:
            mock_fills.return_value = [
                {
                    "trade_id": "T",
                    "order_id": "O",
                    "asset_id": "A",
                    "price": 0.5,
                    "size": 10.0,
                    "market": "M",
                    "ts": 123.0,
                }
            ]
            monitor.check_buy_orders()  # must not raise

        assert ("T", "O") in monitor._seen_fill_keys
        assert monitor._after_ts == 123.0


class TestInitWatermark:
    """Watermark seeds from trades AND actions, so clearing the trades table
    does not reset it to 0 (which would refetch all history every restart)."""

    def test_seeds_from_actions_when_trades_empty(self):
        monitor, api, db = _make_monitor()
        db.get_trade_history.return_value = []
        db.get_actions.return_value = [{"created_at": 555.0}]
        monitor.init_watermark()
        assert monitor._after_ts == 555.0

    def test_seeds_from_max_of_trades_and_actions(self):
        monitor, api, db = _make_monitor()
        db.get_trade_history.return_value = [{"created_at": 100.0}]
        db.get_actions.return_value = [{"created_at": 555.0}]
        monitor.init_watermark()
        assert monitor._after_ts == 555.0

    def test_zero_when_both_empty(self):
        monitor, api, db = _make_monitor()
        db.get_trade_history.return_value = []
        db.get_actions.return_value = []
        monitor.init_watermark()
        assert monitor._after_ts == 0.0


# ---------------------------------------------------------------------------
# Step 1b: check_take_profit — position-driven take-profit (one sell at cost)
# ---------------------------------------------------------------------------


class TestCheckTakeProfit:
    def _pos(self, size=222.08, avg=0.30, asset="tok1", cid="mkt1"):
        return {
            "asset": asset,
            "size": size,
            "avgPrice": avg,
            "curPrice": avg,
            "conditionId": cid,
        }

    def _sell(self, oid, price, original, asset="tok1", matched=0):
        return {
            "id": oid,
            "asset_id": asset,
            "side": "SELL",
            "price": str(price),
            "original_size": str(original),
            "size_matched": str(matched),
        }

    def test_places_one_sell_at_cost_when_none_exist(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos()]
        api.get_open_orders.return_value = []
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        api.place_limit_sell.assert_called_once_with(
            "tok1", 0.30, 222.08, tick_size="0.01"
        )
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "take_profit_sell" in ats
        tp = next(
            c
            for c in db.record_action.call_args_list
            if c.kwargs["action_type"] == "take_profit_sell"
        )
        assert tp.kwargs["price"] == 0.30
        assert "avgPrice" in tp.kwargs["price_basis"]

    def test_keeps_correct_single_sell(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos()]
        api.get_open_orders.return_value = [self._sell("s1", 0.30, 222.08)]
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        api.cancel_orders.assert_not_called()
        api.place_limit_sell.assert_not_called()

    def test_replaces_phantom_and_split_sells_with_one(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos()]
        api.get_open_orders.return_value = [
            self._sell("a", 0.38, 177.77),
            self._sell("b", 0.38, 22.23),
            self._sell("c", 0.30, 25.0),
            self._sell("d", 0.30, 22.857),
            self._sell("e", 0.30, 8.171),
            self._sell("f", 0.30, 7.471),
            self._sell("g", 0.30, 7.28),
            self._sell("h", 0.30, 7.128),
        ]
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        cancelled = api.cancel_orders.call_args[0][0]
        assert set(cancelled) == {"a", "b", "c", "d", "e", "f", "g", "h"}
        api.place_limit_sell.assert_called_once_with(
            "tok1", 0.30, 222.08, tick_size="0.01"
        )
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "take_profit_recancel" in ats
        assert "take_profit_sell" in ats

    def test_positions_api_failure_skips(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.side_effect = Exception("Data API down")

        monitor.check_take_profit()  # must not raise

        api.place_limit_sell.assert_not_called()

    def test_open_orders_failure_skips(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos()]
        api.get_open_orders.side_effect = Exception("timeout")

        monitor.check_take_profit()  # must not raise

        api.place_limit_sell.assert_not_called()

    def test_zero_size_position_skipped(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(size=0.0)]
        api.get_open_orders.return_value = []
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        api.place_limit_sell.assert_not_called()

    def test_orderbook_failure_falls_back_to_default_tick(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(avg=0.30)]
        api.get_open_orders.return_value = []
        api.get_orderbook.side_effect = Exception("ob down")

        monitor.check_take_profit()

        api.place_limit_sell.assert_called_once_with(
            "tok1", 0.30, 222.08, tick_size="0.01"
        )

    def test_ignores_other_assets_and_buy_orders(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(asset="tok1")]
        api.get_open_orders.return_value = [
            self._sell("other", 0.50, 100.0, asset="tok2"),  # different asset
            {  # our asset but a BUY — must not be cancelled/counted
                "id": "buy1",
                "asset_id": "tok1",
                "side": "BUY",
                "price": "0.30",
                "original_size": "222.08",
                "size_matched": "0",
            },
        ]
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        # No matching SELL for tok1 -> place one; never cancel the other-asset
        # sell or our own buy.
        api.place_limit_sell.assert_called_once_with(
            "tok1", 0.30, 222.08, tick_size="0.01"
        )
        if api.cancel_orders.called:
            cancelled = api.cancel_orders.call_args[0][0]
            assert "other" not in cancelled
            assert "buy1" not in cancelled


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
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]

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
                "market": "cid1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
                "neg_risk": False,
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
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
                "market": "cid1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]

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

    def test_uses_real_max_spread_from_rewards_api(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]

        with patch("engine.monitor.needs_replace", return_value="keep"), patch(
            "engine.monitor.determine_order_price", return_value=0.48
        ) as dop:
            monitor.check_sell_orders()

        assert dop.call_args.kwargs["max_spread"] == 3

    def test_rewards_cache_hit_single_api_call(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            },
            {
                "id": "o2",
                "side": "BUY",
                "asset_id": "tok2",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            },
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]

        with patch("engine.monitor.needs_replace", return_value="keep"):
            monitor.check_sell_orders()

        assert api.get_rewards_for_market.call_count == 1

    def test_skip_when_rewards_api_fails(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            },
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.side_effect = Exception("boom")

        monitor.check_sell_orders()

        api.cancel_orders.assert_not_called()
        api.place_limit_buy.assert_not_called()

    def test_skip_when_max_spread_unparseable(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            },
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{}]

        monitor.check_sell_orders()

        api.cancel_orders.assert_not_called()
        api.place_limit_buy.assert_not_called()

    def test_log_replace_has_detail(self, caplog):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
                "neg_risk": False,
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.place_limit_buy.return_value = {"orderID": "o2"}
        with caplog.at_level(logging.INFO, logger="engine.monitor"), patch(
            "engine.monitor.needs_replace", return_value="replace"
        ), patch("engine.monitor.determine_order_price", return_value=0.48):
            monitor.check_sell_orders()
        text = caplog.text
        assert "[Step3]" in text
        assert "o1" in text and "cid1" in text
        assert "max_spread=3" in text
        assert "replace" in text
        assert "撤单并重挂" in text

    def test_log_keep_has_detail(self, caplog):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        with caplog.at_level(logging.INFO, logger="engine.monitor"), patch(
            "engine.monitor.needs_replace", return_value="keep"
        ), patch("engine.monitor.determine_order_price", return_value=0.48):
            monitor.check_sell_orders()
        assert "[Step3]" in caplog.text
        assert "keep" in caplog.text

    def test_log_cancel_has_detail(self, caplog):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        with caplog.at_level(logging.INFO, logger="engine.monitor"), patch(
            "engine.monitor.needs_replace", return_value="cancel"
        ), patch("engine.monitor.determine_order_price", return_value=None):
            monitor.check_sell_orders()
        assert "[Step3]" in caplog.text
        assert "cancel" in caplog.text
        assert "无" in caplog.text

    def test_log_skip_when_max_spread_unknown(self, caplog):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{}]
        with caplog.at_level(logging.INFO, logger="engine.monitor"):
            monitor.check_sell_orders()
        assert "[Step3]" in caplog.text
        assert "rewards_max_spread" in caplog.text
        assert "跳过" in caplog.text

    def test_log_skip_when_empty_orderbook(self, caplog):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        with caplog.at_level(logging.INFO, logger="engine.monitor"):
            monitor.check_sell_orders()
        assert "[Step3]" in caplog.text
        assert "盘口为空" in caplog.text


class TestMonitorStatusSnapshot:
    @pytest.fixture(autouse=True)
    def _clean_snap(self):
        clear_snapshot()
        yield
        clear_snapshot()

    def _ob(self):
        return {
            "bids": [{"price": "0.48", "size": "1000"}],
            "asks": [{"price": "0.52", "size": "1000"}],
            "tick_size": "0.01",
        }

    def test_step3_keep_records_row(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.begin_status_tick()
        with patch("engine.monitor.needs_replace", return_value="keep"), patch(
            "engine.monitor.determine_order_price", return_value=0.48
        ):
            monitor.check_sell_orders()
        monitor.publish_status()
        rows = get_snapshot()["rows"]
        r = next(x for x in rows if x.get("stage") == "Step3")
        assert r["wallet"] == "0xABC"
        assert "keep" in r["action"]
        assert "cid1" in r["market"]

    def test_step3_skip_empty_orderbook_records_row(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        monitor.begin_status_tick()
        monitor.check_sell_orders()
        monitor.publish_status()
        rows = get_snapshot()["rows"]
        assert any("盘口为空" in x.get("action", "") for x in rows)

    def test_sell_order_gets_a_row(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "s1",
                "side": "SELL",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.60",
                "original_size": "200",
            }
        ]
        monitor.begin_status_tick()
        monitor.check_sell_orders()
        monitor.publish_status()
        rows = get_snapshot()["rows"]
        r = next(x for x in rows if x["side"] == "卖出")
        assert r["stage"] == "止盈卖单"
        assert r["action"] == "挂单中"

    def test_step1_fill_records_row(self):
        monitor, api, db = _make_monitor()
        db.get_settings.return_value = {
            "cooldown_minutes": 20,
            "rewards_cache_ttl_sec": 600,
            "stop_loss_pct": 15.0,
        }
        monitor.begin_status_tick()
        monitor._handle_fill(
            {
                "size": 120,
                "price": 0.60,
                "asset_id": "tok1",
                "market": "cid1",
                "order_id": "o9",
            },
            set(),
        )
        monitor.publish_status()
        rows = get_snapshot()["rows"]
        r = next(x for x in rows if x["stage"] == "Step1")
        assert "成交" in r["action"]
        assert r["market"] == "cid1"
        assert r["wallet"] == "0xABC"

    def test_begin_tick_clears_previous_rows(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "s1",
                "side": "SELL",
                "asset_id": "t",
                "market": "c",
                "size_matched": "0",
                "price": "0.6",
                "original_size": "1",
            }
        ]
        monitor.begin_status_tick()
        monitor.check_sell_orders()
        monitor.publish_status()
        first = len(get_snapshot()["rows"])
        monitor.begin_status_tick()
        monitor.check_sell_orders()
        monitor.publish_status()
        assert len(get_snapshot()["rows"]) == first
        assert len(get_snapshot()["rows"]) == 1


class TestStep1ActionLog:
    def test_no_sell_no_cancel_when_no_order_id(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        with patch("engine.monitor.select_new_buy_fills") as mf:
            mf.return_value = [
                {
                    "trade_id": "t1",
                    "order_id": None,
                    "asset_id": "tok1",
                    "price": 0.25,
                    "size": 100.0,
                    "market": "mkt1",
                    "ts": 1.0,
                }
            ]
            monitor.check_buy_orders()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "take_profit_sell" not in ats
        assert "cancel_remainder" not in ats
        api.place_limit_sell.assert_not_called()

    def test_record_action_never_breaks_fill(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        db.record_action.side_effect = RuntimeError("db down")
        with patch("engine.monitor.select_new_buy_fills") as mf:
            mf.return_value = [
                {
                    "trade_id": "t1",
                    "order_id": "o1",
                    "asset_id": "tok1",
                    "price": 0.25,
                    "size": 100.0,
                    "market": "mkt1",
                    "ts": 1.0,
                }
            ]
            monitor.check_buy_orders()  # must not raise
        api.place_limit_sell.assert_not_called()
        db.record_trade.assert_not_called()
        api.cancel_orders.assert_called_once_with(["o1"])


class TestStep2ActionLog:
    def _pos(self):
        return [
            {
                "asset": "tok1",
                "size": 1000.0,
                "avgPrice": 0.30,
                "curPrice": 0.24,
                "conditionId": "mkt1",
            }
        ]

    def test_stop_loss_records_cancel_and_market_sell(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        api.get_user_positions.return_value = self._pos()
        api.get_open_orders.return_value = [
            {"id": "sell1", "asset_id": "tok1", "side": "SELL"},
        ]
        with patch("engine.monitor.stop_loss_triggered", return_value=True):
            monitor.check_stop_loss()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "stoploss_cancel_sell" in ats
        assert "stoploss_market_sell" in ats
        ms = next(
            c
            for c in db.record_action.call_args_list
            if c.kwargs["action_type"] == "stoploss_market_sell"
        )
        assert ms.kwargs["side"] == "卖出"
        assert ms.kwargs["price"] == 0.24
        assert "avgPrice=0.3000" in ms.kwargs["price_basis"]
        assert "Data API" in ms.kwargs["price_basis"]
        assert "止损阈值" in ms.kwargs["reason"]

    def test_no_cancel_action_when_no_sell_orders(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        api.get_user_positions.return_value = self._pos()
        api.get_open_orders.return_value = []
        with patch("engine.monitor.stop_loss_triggered", return_value=True):
            monitor.check_stop_loss()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "stoploss_cancel_sell" not in ats
        assert "stoploss_market_sell" in ats

    def test_no_action_when_not_triggered(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        api.get_user_positions.return_value = self._pos()
        api.get_open_orders.return_value = []
        with patch("engine.monitor.stop_loss_triggered", return_value=False):
            monitor.check_stop_loss()
        db.record_action.assert_not_called()


class TestStep3ActionLog:
    def _ob(self):
        return {
            "bids": [{"price": "0.48", "size": "1000"}],
            "asks": [{"price": "0.52", "size": "1000"}],
            "tick_size": "0.01",
        }

    def _order(self, price="0.40"):
        return {
            "id": "o1",
            "side": "BUY",
            "asset_id": "tok1",
            "market": "cid1",
            "size_matched": "0",
            "price": price,
            "original_size": "500",
            "neg_risk": False,
        }

    def test_replace_records_cancel_old_and_replace_new(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.place_limit_buy.return_value = {"orderID": "o2"}
        with patch("engine.monitor.needs_replace", return_value="replace"), patch(
            "engine.monitor.determine_order_price", return_value=0.48
        ):
            monitor.check_sell_orders()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["step3_cancel_old", "step3_replace_new"]
        new = db.record_action.call_args_list[1]
        assert new.kwargs["side"] == "买入"
        assert new.kwargs["price"] == 0.48
        assert "determine_order_price" in new.kwargs["price_basis"]
        assert "奖励区间" in new.kwargs["reason"]

    def test_cancel_nocompliant_records_single_action(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        with patch("engine.monitor.needs_replace", return_value="cancel"), patch(
            "engine.monitor.determine_order_price", return_value=None
        ):
            monitor.check_sell_orders()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["step3_cancel_nocompliant"]
        call = db.record_action.call_args_list[0]
        assert call.kwargs["side"] == "-"
        assert call.kwargs["price"] == -1
        assert "无合规价" in call.kwargs["reason"]
        api.place_limit_buy.assert_not_called()

    def test_keep_records_no_action(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order(price="0.48")]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        with patch("engine.monitor.needs_replace", return_value="keep"), patch(
            "engine.monitor.determine_order_price", return_value=0.48
        ):
            monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_empty_orderbook_records_no_action(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_no_max_spread_records_no_action(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{}]
        monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_partial_fill_records_no_action(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "100",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_replace_place_fails_records_only_cancel_old(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.place_limit_buy.side_effect = RuntimeError("network")
        with patch("engine.monitor.needs_replace", return_value="replace"), patch(
            "engine.monitor.determine_order_price", return_value=0.48
        ):
            monitor.check_sell_orders()  # must not raise
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["step3_cancel_old"]


class TestMarketRewardsInfo:
    def test_returns_max_spread_and_reward_total(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = [
            {
                "rewards_max_spread": 3,
                "rewards_config": [{"rate_per_day": 40}, {"rate_per_day": 60}],
            }
        ]
        api.get_market_end_ts.return_value = 4_100_000_000.0
        info = monitor._market_rewards_info("cid1")
        assert info["max_spread"] == 3
        assert info["reward_total"] == 100.0
        assert info["end_ts"] == 4_100_000_000.0

    def test_reward_total_none_when_no_rate_present(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.get_market_end_ts.return_value = None
        info = monitor._market_rewards_info("cid1")
        assert info["reward_total"] is None
        assert info["max_spread"] == 3

    def test_reward_total_none_when_items_empty(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = []
        api.get_market_end_ts.return_value = None
        info = monitor._market_rewards_info("cid1")
        assert info["reward_total"] is None
        assert info["max_spread"] is None

    def test_end_ts_none_when_non_numeric(self):
        # A bare MagicMock api (get_market_end_ts unset) returns a MagicMock;
        # it must be treated as None, not a number.
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        # deliberately do NOT set api.get_market_end_ts.return_value
        info = monitor._market_rewards_info("cid1")
        assert info["end_ts"] is None

    def test_rewards_failure_yields_none_fields(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.side_effect = Exception("boom")
        api.get_market_end_ts.return_value = None
        info = monitor._market_rewards_info("cid1")
        assert info["max_spread"] is None
        assert info["reward_total"] is None

    def test_caches_within_ttl_single_api_call(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 200}]}
        ]
        api.get_market_end_ts.return_value = 4_100_000_000.0
        monitor._market_rewards_info("cid1")
        monitor._market_rewards_info("cid1")
        assert api.get_rewards_for_market.call_count == 1
        assert api.get_market_end_ts.call_count == 1


class TestStep3EligibilityRecheck:
    # Full scanner thresholds so the gates are active.
    THRESH = {
        "min_reward_usd": 100.0,
        "min_settlement_days": 4,
        "min_price_cents": 10.0,
        "max_price_cents": 50.0,
        "max_spread_cents": 3.0,
    }

    def _ob(self, best_bid="0.30", best_ask="0.31", tick="0.01"):
        return {
            "bids": [{"price": best_bid, "size": "1000"}],
            "asks": [{"price": best_ask, "size": "1000"}],
            "tick_size": tick,
        }

    def _buy(self):
        return [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.30",
                "original_size": "500",
            }
        ]

    def _far_future(self):
        import time as _t

        return _t.time() + 60 * 86400  # 60 days out

    def test_cancels_when_reward_below_threshold(self):
        monitor, api, db = _make_monitor(self.THRESH)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 50}]}
        ]
        api.get_market_end_ts.return_value = self._far_future()
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_not_called()

    def test_cancels_when_near_settlement(self):
        import time as _t

        monitor, api, db = _make_monitor(self.THRESH)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 500}]}
        ]
        api.get_market_end_ts.return_value = _t.time() + 1 * 86400  # 1 day < 4
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])

    def test_cancels_when_bid_out_of_band(self):
        monitor, api, db = _make_monitor(self.THRESH)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(best_bid="0.60", best_ask="0.61")
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 500}]}
        ]
        api.get_market_end_ts.return_value = self._far_future()
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])

    def test_cancels_when_spread_too_wide(self):
        monitor, api, db = _make_monitor(self.THRESH)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(
            best_bid="0.30", best_ask="0.35"
        )  # 5c >= 3c
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 500}]}
        ]
        api.get_market_end_ts.return_value = self._far_future()
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])

    def test_records_eligibility_cancel_action(self):
        monitor, api, db = _make_monitor(self.THRESH)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 50}]}
        ]
        api.get_market_end_ts.return_value = self._far_future()
        monitor.check_sell_orders()
        ats = [c.kwargs.get("action_type") for c in db.record_action.call_args_list]
        assert "eligibility_cancel" in ats

    def test_eligibility_cancel_failure_does_not_record_action(self):
        monitor, api, db = _make_monitor(self.THRESH)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 50}]}
        ]
        api.get_market_end_ts.return_value = self._far_future()
        api.cancel_orders.side_effect = Exception("network error")
        monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_keeps_and_runs_compliance_when_all_pass(self):
        monitor, api, db = _make_monitor(self.THRESH)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob()  # bid 30c in band, spread 1c
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 500}]}
        ]
        api.get_market_end_ts.return_value = self._far_future()
        with patch("engine.monitor.needs_replace", return_value="keep"):
            monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()

    def test_keeps_when_reward_and_end_ts_unknown(self):
        # rewards items lack rate, end_ts unknown -> those gates skipped;
        # bid in band & spread narrow -> keep -> falls to compliance.
        monitor, api, db = _make_monitor(self.THRESH)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.get_market_end_ts.return_value = None
        with patch("engine.monitor.needs_replace", return_value="keep"):
            monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()
