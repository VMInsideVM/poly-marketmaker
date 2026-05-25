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
    db.get_blacklist_ids.return_value = set()
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

    def _buy(self, price=0.30, size=222.08, asset="tok1", ts="100"):
        return {
            "id": f"t-{ts}",
            "market": "mkt1",
            "match_time": ts,
            "maker_orders": [
                {
                    "order_id": f"o-{ts}",
                    "maker_address": "0xFUNDER",
                    "side": "BUY",
                    "matched_amount": str(size),
                    "price": str(price),
                    "asset_id": asset,
                }
            ],
        }

    def test_places_one_sell_at_cost_when_none_exist(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos()]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = [self._buy()]
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
        assert "get_trades" in tp.kwargs["price_basis"]

    def test_keeps_correct_single_sell(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos()]
        api.get_open_orders.return_value = [self._sell("s1", 0.30, 222.08)]
        api.get_trades.return_value = [self._buy()]
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
        api.get_trades.return_value = [self._buy()]
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
        api.get_trades.return_value = [self._buy()]
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
        api.get_trades.return_value = [self._buy(asset="tok1")]
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

    def test_lifts_sell_above_bid_when_in_profit(self):
        # 事故场景:成本 0.21 < 买一 0.27 -> 卖价上移到 0.28,绝不穿价市价清仓
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(size=361.0)]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = [self._buy(price=0.21, size=361.0)]
        api.get_orderbook.return_value = {
            "tick_size": "0.01",
            "bids": [{"price": "0.27", "size": "9999"}],
            "asks": [{"price": "0.29", "size": "9999"}],
        }

        monitor.check_take_profit()

        api.place_limit_sell.assert_called_once_with(
            "tok1", pytest.approx(0.28), 361.0, tick_size="0.01"
        )

    def test_skips_when_both_sources_unavailable(self):
        # get_trades 无成交 且 avgPrice<=0 -> 两源皆空 -> 不动卖单
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(avg=0.0)]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = []
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        api.place_limit_sell.assert_not_called()
        api.cancel_orders.assert_not_called()

    def test_falls_back_to_avgprice_when_no_trades(self):
        # get_trades 0 笔 + avgPrice=0.30 -> 用 avgPrice 经穿价护栏挂卖
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(size=222.08, avg=0.30)]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = []  # get_trades 取不到
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        api.place_limit_sell.assert_called_once_with(
            "tok1", 0.30, 222.08, tick_size="0.01"
        )
        tp = next(
            c
            for c in db.record_action.call_args_list
            if c.kwargs["action_type"] == "take_profit_sell"
        )
        assert "avgPrice兜底" in tp.kwargs["price_basis"]

    def test_no_fallback_when_get_trades_has_data(self):
        # get_trades 有成交 -> 用加权成本,不碰 avgPrice(门控验证)
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(size=222.08, avg=0.99)]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = [self._buy(price=0.30, size=222.08)]
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        api.place_limit_sell.assert_called_once_with(
            "tok1",
            0.30,
            222.08,
            tick_size="0.01",  # 0.30 来自 get_trades,非 avgPrice 0.99
        )
        tp = next(
            c
            for c in db.record_action.call_args_list
            if c.kwargs["action_type"] == "take_profit_sell"
        )
        assert "get_trades加权" in tp.kwargs["price_basis"]

    def _taker_buy(self, price=0.33, size=100.0, asset="tok1", ts="200"):
        # 我们当 taker 的买入:成交在顶层,maker_orders 是对手方
        return {
            "id": f"tt-{ts}",
            "market": "mkt1",
            "match_time": ts,
            "trader_side": "TAKER",
            "side": "BUY",
            "asset_id": asset,
            "size": str(size),
            "price": str(price),
            "maker_orders": [
                {
                    "order_id": f"cp-{ts}",
                    "maker_address": "0xCOUNTERPARTY",
                    "side": "SELL",
                    "matched_amount": str(size),
                    "price": str(price),
                    "asset_id": asset,
                }
            ],
        }

    def test_cost_query_omits_maker_address(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos()]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = [self._buy()]
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        params = api.get_trades.call_args.args[0]
        assert params.maker_address is None
        assert params.asset_id == "tok1"

    def test_places_sell_for_taker_acquired_position(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(size=100.0, avg=0.33)]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = [self._taker_buy(price=0.33, size=100.0)]
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        api.place_limit_sell.assert_called_once_with(
            "tok1", 0.33, 100.0, tick_size="0.01"
        )


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
        api.get_trades.return_value = [
            {
                "id": "t",
                "market": "mkt1",
                "match_time": "1",
                "maker_orders": [
                    {
                        "order_id": "o",
                        "maker_address": "0xFUNDER",
                        "side": "BUY",
                        "matched_amount": "1000",
                        "price": "0.30",
                        "asset_id": "tok1",
                    }
                ],
            }
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
        api.get_trades.return_value = [
            {
                "id": "t",
                "market": "mkt1",
                "match_time": "1",
                "maker_orders": [
                    {
                        "order_id": "o",
                        "maker_address": "0xFUNDER",
                        "side": "BUY",
                        "matched_amount": "1000",
                        "price": "0.30",
                        "asset_id": "tok1",
                    }
                ],
            }
        ]

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

    def test_skips_stop_loss_when_both_sources_unavailable(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        api.get_user_positions.return_value = [
            {
                "asset": "tok1",
                "size": 1000.0,
                "avgPrice": 0.0,  # 无 avgPrice
                "curPrice": 0.10,
                "conditionId": "mkt1",
            }
        ]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = []  # get_trades 也空 -> 两源皆空

        monitor.check_stop_loss()

        api.place_market_sell.assert_not_called()

    def test_stop_loss_falls_back_to_avgprice(self):
        # get_trades 0 笔 + avgPrice=0.30,现价 0.24 -> 用 avgPrice 判定并市价平仓
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
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
        api.get_trades.return_value = []  # get_trades 取不到 -> 回落 avgPrice

        monitor.check_stop_loss()

        api.cancel_orders.assert_called_with(["sell1"])
        api.place_market_sell.assert_called_with("tok1", 1000.0)
        sl = next(
            c
            for c in db.record_action.call_args_list
            if c.kwargs["action_type"] == "stoploss_market_sell"
        )
        assert "avgPrice兜底" in sl.kwargs["price_basis"]


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
        api.get_trades.return_value = [
            {
                "id": "t",
                "market": "mkt1",
                "match_time": "1",
                "maker_orders": [
                    {
                        "order_id": "o",
                        "maker_address": "0xFUNDER",
                        "side": "BUY",
                        "matched_amount": "1000",
                        "price": "0.30",
                        "asset_id": "tok1",
                    }
                ],
            }
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
        assert "get_trades加权 0.3000" in ms.kwargs["price_basis"]
        assert "Data API" in ms.kwargs["price_basis"]
        assert "止损阈值" in ms.kwargs["reason"]

    def test_no_cancel_action_when_no_sell_orders(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        api.get_user_positions.return_value = self._pos()
        api.get_open_orders.return_value = []
        api.get_trades.return_value = [
            {
                "id": "t",
                "market": "mkt1",
                "match_time": "1",
                "maker_orders": [
                    {
                        "order_id": "o",
                        "maker_address": "0xFUNDER",
                        "side": "BUY",
                        "matched_amount": "1000",
                        "price": "0.30",
                        "asset_id": "tok1",
                    }
                ],
            }
        ]
        with patch("engine.monitor.stop_loss_triggered", return_value=True):
            monitor.check_stop_loss()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "stoploss_cancel_sell" not in ats
        assert "stoploss_market_sell" in ats

    def test_no_action_when_not_triggered(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        api.get_user_positions.return_value = self._pos()
        api.get_open_orders.return_value = []
        # Provide a real BUY trade so _cost() returns a non-None value and the
        # code reaches the stop_loss_triggered() branch (patched to False).
        # Without this, _cost() returns None (no fills) and the method returns
        # early — never reaching the patched check, so the test was passing for
        # the wrong reason.
        api.get_trades.return_value = [
            {
                "id": "t-notriggered",
                "market": "mkt1",
                "match_time": "1",
                "maker_orders": [
                    {
                        "order_id": "o-notriggered",
                        "maker_address": "0xFUNDER",
                        "side": "BUY",
                        "matched_amount": "1000",
                        "price": "0.30",
                        "asset_id": "tok1",
                    }
                ],
            }
        ]
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


class TestStep3PriceBand:
    """调单算出的目标价超出配置「单价区间」[min,max]美分时，撤买单不重挂。"""

    SETTINGS = {"min_price_cents": 10.0, "max_price_cents": 50.0}

    def _ob(self, best_bid="0.48", best_ask="0.52", tick="0.01"):
        return {
            "bids": [{"price": best_bid, "size": "1000"}],
            "asks": [{"price": best_ask, "size": "1000"}],
            "tick_size": tick,
        }

    def _order(self, price="0.48"):
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

    def test_cancels_when_want_above_band(self):
        # 目标价 0.55 = 55c > 50c 上限：撤单不重挂，即便策略判定 replace。
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = [self._order(price="0.48")]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        with patch("engine.monitor.needs_replace", return_value="replace"), patch(
            "engine.monitor.determine_order_price", return_value=0.55
        ):
            monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_not_called()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["price_band_cancel"]
        call = db.record_action.call_args_list[0]
        assert call.kwargs["side"] == "-"
        assert call.kwargs["price"] == -1
        assert "单价区间" in call.kwargs["reason"]

    def test_cancels_when_want_below_band(self):
        # 目标价 0.08 = 8c < 10c 下限：撤单不重挂。
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = [self._order(price="0.12")]
        api.get_orderbook.return_value = self._ob(best_bid="0.09", best_ask="0.11")
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        with patch("engine.monitor.needs_replace", return_value="replace"), patch(
            "engine.monitor.determine_order_price", return_value=0.08
        ):
            monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_not_called()

    def test_keeps_when_want_at_band_edge(self):
        # 含端点：目标价 0.50 = 50c 恰在上限，合规，按 keep 不撤单。
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = [self._order(price="0.50")]
        api.get_orderbook.return_value = self._ob(best_bid="0.50", best_ask="0.51")
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        with patch("engine.monitor.needs_replace", return_value="keep"), patch(
            "engine.monitor.determine_order_price", return_value=0.50
        ):
            monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()

    def test_within_band_replaces_normally(self):
        # 目标价 0.48 在区间内：维持原有 replace 行为。
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = [self._order(price="0.40")]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.place_limit_buy.return_value = {"orderID": "o2"}
        with patch("engine.monitor.needs_replace", return_value="replace"), patch(
            "engine.monitor.determine_order_price", return_value=0.48
        ):
            monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_called_once()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["step3_cancel_old", "step3_replace_new"]

    def test_no_band_keys_is_noop(self):
        # 默认 settings 不含价格区间键：闸门为 no-op，目标价 0.55 仍按 replace。
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order(price="0.40")]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.place_limit_buy.return_value = {"orderID": "o2"}
        with patch("engine.monitor.needs_replace", return_value="replace"), patch(
            "engine.monitor.determine_order_price", return_value=0.55
        ):
            monitor.check_sell_orders()
        api.place_limit_buy.assert_called_once()


class TestStep3EligibilityRecheck:
    SETTINGS = {"max_spread_cents": 3.0}

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

    def test_cancels_when_spread_at_or_over_threshold(self):
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(
            best_bid="0.30", best_ask="0.35"
        )  # 5c >= 3c
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_not_called()

    def test_records_eligibility_cancel_action(self):
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(best_bid="0.30", best_ask="0.36")
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        ats = [c.kwargs.get("action_type") for c in db.record_action.call_args_list]
        assert "eligibility_cancel" in ats

    def test_eligibility_cancel_failure_does_not_record_action(self):
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(best_bid="0.30", best_ask="0.36")
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.cancel_orders.side_effect = Exception("network error")
        monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_keeps_and_runs_compliance_when_spread_narrow(self):
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(
            best_bid="0.30", best_ask="0.31"
        )  # 1c < 3c
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        with patch("engine.monitor.needs_replace", return_value="keep"):
            monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()

    def test_dropped_dimensions_do_not_cause_cancel(self):
        # reward / settlement / price-band are no longer checked: a market that
        # would have failed those (e.g. bid far out of the old price band) must
        # NOT be cancelled as long as the spread is within threshold.
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(
            best_bid="0.60", best_ask="0.61"
        )  # 1c spread
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        with patch("engine.monitor.needs_replace", return_value="keep"):
            monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()


class TestCostHelper:
    def _buy_trade(self, asset, price, size, ts="100"):
        return {
            "id": f"t-{asset}-{ts}",
            "market": "mkt1",
            "match_time": ts,
            "maker_orders": [
                {
                    "order_id": f"o-{asset}-{ts}",
                    "maker_address": "0xFUNDER",
                    "side": "BUY",
                    "matched_amount": str(size),
                    "price": str(price),
                    "asset_id": asset,
                }
            ],
        }

    def test_cost_from_get_trades_weighted(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = [
            self._buy_trade("tok1", 0.20, 200, ts="1"),
            self._buy_trade("tok1", 0.28, 200, ts="2"),
        ]
        monitor.begin_status_tick()
        assert monitor._cost("tok1", 400) == pytest.approx(0.24)

    def test_cost_cached_within_tick(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = [self._buy_trade("tok1", 0.28, 361)]
        monitor.begin_status_tick()
        monitor._cost("tok1", 361)
        monitor._cost("tok1", 361)
        assert api.get_trades.call_count == 1  # 同 tick 只取一次

    def test_cost_none_when_get_trades_fails(self):
        monitor, api, db = _make_monitor()
        api.get_trades.side_effect = Exception("boom")
        monitor.begin_status_tick()
        assert monitor._cost("tok1", 361) is None

    def test_cost_none_when_no_buy_fills(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        monitor.begin_status_tick()
        assert monitor._cost("tok1", 361) is None


class TestStep3Blacklist:
    def test_check_compliance_cancels_blacklisted_buy_no_replace(self):
        monitor, api, db = _make_monitor()
        db.get_blacklist_ids.return_value = {"cid1"}
        o = {
            "asset_id": "t1",
            "market": "cid1",
            "id": "o1",
            "price": "0.40",
            "original_size": "100",
            "size_matched": "0",
        }
        monitor._check_compliance(o)
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_not_called()
        api.get_orderbook.assert_not_called()  # 命中黑名单提前返回,不取盘口
