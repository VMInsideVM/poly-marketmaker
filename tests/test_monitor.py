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
    db.get_template_for.return_value = default_settings
    db.get_blacklist_ids.return_value = set()
    # funder address via api.get_funder()
    api.get_funder.return_value = "0xFUNDER"
    # sane default so methods that read get_trades don't iterate a MagicMock
    api.get_trades.return_value = []
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

    def _buy_trade(self, tid, oid, asset="tok1", ts="100"):
        return {
            "id": tid,
            "market": "mkt1",
            "match_time": ts,
            "maker_orders": [
                {
                    "order_id": oid,
                    "maker_address": "0xFUNDER",
                    "side": "BUY",
                    "matched_amount": "100",
                    "price": "0.3",
                    "asset_id": asset,
                }
            ],
        }

    def test_seeds_seen_keys_from_existing_fills(self):
        # 启动恢复:把当前已存在的成交预灌进去重集合,使其能跨重启覆盖历史成交。
        monitor, api, db = _make_monitor()
        db.get_trade_history.return_value = []
        db.get_actions.return_value = []
        api.get_trades.return_value = [self._buy_trade("T-old", "O-old")]
        monitor.init_watermark()
        assert ("T-old", "O-old") in monitor._seen_fill_keys

    def test_restart_does_not_reprocess_already_seen_fill(self):
        # 2026-05-29 实盘现象:重启后 _seen_fill_keys 本是空的 + 水位线只是粗下界,
        # 旧成交被 get_trades 重新捞回、当成"新成交"重放 -> 给已平仓市场重写
        # cancel_remainder / 重设冷却。init_watermark 预灌 seen 后,启动后不再重放。
        monitor, api, db = _make_monitor()
        db.get_trade_history.return_value = []
        db.get_actions.return_value = []
        old_buy = self._buy_trade("T-old", "O-old")
        api.get_trades.return_value = [old_buy]

        monitor.init_watermark()  # 启动恢复:预灌 seen
        monitor.check_buy_orders()  # 下一 tick:同一笔旧成交不应再被处理

        db.set_cooldown.assert_not_called()
        api.cancel_orders.assert_not_called()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "cancel_remainder" not in ats

    def test_new_fill_after_startup_still_handled(self):
        # 反向保证:启动后真正新发生的成交照常处理(设冷却 + 撤余单)。
        monitor, api, db = _make_monitor()
        db.get_trade_history.return_value = []
        db.get_actions.return_value = []
        api.get_trades.return_value = []  # 启动时无历史成交
        monitor.init_watermark()

        api.get_trades.return_value = [self._buy_trade("T-new", "O-new", ts="200")]
        monitor.check_buy_orders()

        db.set_cooldown.assert_called_once()
        api.cancel_orders.assert_called_once_with(["O-new"])
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "cancel_remainder" in ats


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
        # price 0.48, mid=0.50, max_spread=3 → band [0.47, 0.53] → in-band → keep
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

        monitor.check_sell_orders()

        api.cancel_orders.assert_not_called()

    def test_cancel_outofband_order(self):
        # price 0.40 is outside reward band [0.47, 0.53] → cancel, no re-place
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
        # max_spread=3 from API: mid=0.50 → band [0.47, 0.53]; price 0.48 in-band → keep
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

        monitor.check_sell_orders()

        api.cancel_orders.assert_not_called()

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

    def test_log_outofband_cancel_has_detail(self, caplog):
        # price 0.40 out of reward band [0.47, 0.53] → logged cancel
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
        with caplog.at_level(logging.INFO, logger="engine.monitor"):
            monitor.check_sell_orders()
        text = caplog.text
        assert "[Step3]" in text
        assert "o1" in text and "cid1" in text
        assert "cancel" in text

    def test_log_keep_has_detail(self, caplog):
        # price 0.48 in reward band [0.47, 0.53] → logged keep
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
        with caplog.at_level(logging.INFO, logger="engine.monitor"):
            monitor.check_sell_orders()
        assert "[Step3]" in caplog.text
        assert "keep" in caplog.text

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

    def test_outofband_records_cancel_action(self):
        # price 0.40 out of reward band → step3_cancel_outofband, no re-place
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["step3_cancel_outofband"]
        call = db.record_action.call_args_list[0]
        assert call.kwargs["side"] == "-"
        assert call.kwargs["price"] == -1
        assert "奖励区间" in call.kwargs["reason"]
        api.place_limit_buy.assert_not_called()

    def test_inband_records_no_action(self):
        # price 0.48 in reward band [0.47, 0.53] → keep, no action recorded
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order(price="0.48")]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
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

    def test_cancel_failure_records_no_action(self):
        # If the cancel API call fails, no action should be recorded
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.cancel_orders.side_effect = RuntimeError("network")
        monitor.check_sell_orders()  # must not raise
        db.record_action.assert_not_called()


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

    def test_cancels_when_price_above_band(self):
        # 挂单价 0.55 = 55c > 50c 上限：price_band_cancel，不重挂。
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = [self._order(price="0.55")]
        api.get_orderbook.return_value = self._ob(best_bid="0.55", best_ask="0.56")
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_not_called()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["price_band_cancel"]
        call = db.record_action.call_args_list[0]
        assert call.kwargs["side"] == "-"
        assert call.kwargs["price"] == -1
        assert "单价区间" in call.kwargs["reason"]

    def test_cancels_when_price_below_band(self):
        # 挂单价 0.08 = 8c < 10c 下限：price_band_cancel，不重挂。
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = [self._order(price="0.08")]
        api.get_orderbook.return_value = self._ob(best_bid="0.08", best_ask="0.09")
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_not_called()

    def test_keeps_when_price_at_band_edge(self):
        # 含端点：挂单价 0.50 = 50c 恰在上限，且在奖励区间内，保持不动。
        # mid = (0.50+0.51)/2 = 0.505; max_spread=3 → band [0.475, 0.535]; 0.50 in-band.
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = [self._order(price="0.50")]
        api.get_orderbook.return_value = self._ob(best_bid="0.50", best_ask="0.51")
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()

    def test_within_price_band_and_reward_band_keeps(self):
        # 挂单价 0.48 在单价区间 [10c,50c] 内，且在奖励区间内：保持不动。
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = [self._order(price="0.48")]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()
        api.place_limit_buy.assert_not_called()

    def test_no_band_keys_is_noop(self):
        # 默认 settings 不含价格区间键：price_band 闸门 no-op。
        # 挂单价 0.40 在奖励区间外 → step3_cancel_outofband。
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order(price="0.40")]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["step3_cancel_outofband"]
        api.place_limit_buy.assert_not_called()


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
        # buy price=0.30, bid=0.30, ask=0.31 → mid=0.305, band [0.275, 0.335] → in-band → keep
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(
            best_bid="0.30", best_ask="0.31"
        )  # 1c < 3c
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()

    def test_dropped_dimensions_do_not_cause_cancel(self):
        # buy price=0.30, bid=0.60, ask=0.61 → 1c spread → passes eligibility.
        # mid=0.605, max_spread=3 → band [0.575, 0.635]. Order price 0.30 is out
        # of reward band → step3_cancel_outofband (not eligibility_cancel).
        # The comment below preserves the original intent: spread check alone does
        # not cancel when spread is narrow, even if the price seems off.
        monitor, api, db = _make_monitor(self.SETTINGS)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(
            best_bid="0.60", best_ask="0.61"
        )  # 1c spread
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        monitor.check_sell_orders()
        # eligibility cancel must NOT fire (spread 1c < threshold 3c)
        ats = [c.kwargs.get("action_type") for c in db.record_action.call_args_list]
        assert "eligibility_cancel" not in ats


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
        cost, lots = monitor._cost_lots("tok1", 400, "mkt1")
        assert cost == pytest.approx(0.24)

    def test_cost_cached_within_tick(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = [self._buy_trade("tok1", 0.28, 361)]
        monitor.begin_status_tick()
        monitor._cost_lots("tok1", 361, "mkt1")
        monitor._cost_lots("tok1", 361, "mkt1")
        assert api.get_trades.call_count == 1  # 同 tick 只取一次

    def test_cost_none_when_get_trades_fails(self):
        monitor, api, db = _make_monitor()
        api.get_trades.side_effect = Exception("boom")
        monitor.begin_status_tick()
        cost, lots = monitor._cost_lots("tok1", 361, "mkt1")
        assert cost is None

    def test_cost_none_when_no_buy_fills(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        monitor.begin_status_tick()
        cost, lots = monitor._cost_lots("tok1", 361, "mkt1")
        assert cost is None

    def test_cost_uses_market_filter_when_asset_filter_broken(self):
        # 真实事故(2026-05-29):Polymarket /trades 的服务端 asset_id 过滤失效——
        # 同一 asset 明明有成交,按 asset_id 查却返回空;按 market(conditionId)查
        # 或全量查能拿到。_cost_lots 必须按 market 过滤,否则成本永远算不出 -> 全仓
        # 裸奔。本测试用 side_effect 复现该 server 行为:asset_id 查询返回空,market
        # 查询返回真实成交,断言成本仍能被重建(0.45 / 263 股)。
        monitor, api, db = _make_monitor()
        buy = self._buy_trade("tok1", 0.45, 263, ts="1")  # market="mkt1"

        def fake_get_trades(params):
            if getattr(params, "asset_id", None):  # 服务端 asset 过滤:坏的
                return []
            if getattr(params, "market", None) == "mkt1":  # market 过滤:可用
                return [buy]
            return []

        api.get_trades.side_effect = fake_get_trades
        monitor.begin_status_tick()
        cost, lots = monitor._cost_lots("tok1", 263, "mkt1")
        assert cost == pytest.approx(0.45)


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


class TestMonitorReadsTemplate:
    def test_check_exit_reads_template(self):
        from engine.monitor import OrderMonitor
        from unittest.mock import MagicMock

        db = MagicMock()
        db.get_template_for.return_value = {
            "theta_loss_cents": 2,
            "theta_stop_cents": 5,
            "case_a_mode": "ask",
        }
        db.get_settings.return_value = {
            "cooldown_minutes": 20,
            "rewards_cache_ttl_sec": 600,
        }
        api = MagicMock()
        api.get_user_positions.return_value = []
        mon = OrderMonitor(api, db, "0xW")
        mon.check_exit()
        db.get_template_for.assert_called_with("0xW")


class TestCheckExit:
    def _setup(
        self,
        cost,
        size,
        bids,
        asks,
        sells=None,
        mode="ask",
        theta_loss=2,
        theta_stop=5,
    ):
        monitor, api, db = _make_monitor(
            settings={
                "theta_loss_cents": theta_loss,
                "theta_stop_cents": theta_stop,
                "case_a_mode": mode,
            }
        )
        api.get_user_positions.return_value = [
            {
                "asset": "A-y",
                "size": size,
                "curPrice": (bids[0][0] if bids else 0),
                "conditionId": "A",
            }
        ]
        api.get_open_orders.return_value = sells or []
        api.get_orderbook.return_value = {
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
            "tick_size": "0.01",
        }
        monitor._cost_lots = lambda a, s, c: (
            cost,
            [{"price": cost, "take": s, "ts": 0, "trade_id": "t"}],
        )
        return monitor, api, db

    def test_case_a_rests_one_sell_at_ask(self):
        monitor, api, db = self._setup(0.30, 100, [(0.31, 500)], [(0.33, 500)])
        monitor.check_exit()
        api.place_limit_sell.assert_called_once()
        a, k = api.place_limit_sell.call_args
        assert a[0] == "A-y" and abs(a[1] - 0.33) < 1e-9 and a[2] == 100
        api.place_market_sell.assert_not_called()

    def test_case_a_market_mode_clears(self):
        monitor, api, db = self._setup(
            0.30, 100, [(0.31, 500)], [(0.33, 500)], mode="market"
        )
        monitor.check_exit()
        api.place_market_sell.assert_called_once_with("A-y", 100)
        api.place_limit_sell.assert_not_called()

    def test_b0_market_clear_and_record(self):
        monitor, api, db = self._setup(0.40, 100, [(0.35, 500)], [(0.36, 500)])
        monitor.check_exit()
        api.place_market_sell.assert_called_once_with("A-y", 100)
        db.record_trade.assert_called_once()

    def test_b0_cancels_existing_sell_before_market_clear(self):
        # 已有一笔挂卖单 -> B0 必须先撤再市价清仓(避免与残留卖单并存/重复卖)。
        sells = [
            {
                "id": "s1",
                "asset_id": "A-y",
                "side": "SELL",
                "price": "0.42",
                "original_size": "100",
                "size_matched": "0",
            }
        ]
        calls = []
        monitor, api, db = self._setup(
            0.40, 100, [(0.35, 500)], [(0.36, 500)], sells=sells
        )
        api.cancel_orders.side_effect = lambda ids: calls.append(("cancel", ids))
        api.place_market_sell.side_effect = lambda a, s: calls.append(("market", a, s))
        monitor.check_exit()
        assert calls[0][0] == "cancel" and "s1" in calls[0][1]
        assert calls[1][0] == "market"

    def test_a_market_does_not_record_trade(self):
        # case A 市价(无损)不是止损,不应落 stop_loss 记录。
        monitor, api, db = self._setup(
            0.30, 100, [(0.31, 500)], [(0.33, 500)], mode="market"
        )
        monitor.check_exit()
        api.place_market_sell.assert_called_once_with("A-y", 100)
        db.record_trade.assert_not_called()

    def test_b_sweep_marketable_limit_at_floor(self):
        monitor, api, db = self._setup(0.40, 100, [(0.39, 500)], [(0.41, 500)])
        monitor.check_exit()
        api.place_marketable_limit_sell.assert_called_once()
        a, k = api.place_marketable_limit_sell.call_args
        assert a[0] == "A-y" and abs(a[1] - 0.38) < 1e-9 and a[2] == 100

    def test_b_park_rests_at_ask(self):
        monitor, api, db = self._setup(0.40, 100, [(0.37, 500)], [(0.41, 500)])
        monitor.check_exit()
        api.place_limit_sell.assert_called_once()
        a, k = api.place_limit_sell.call_args
        assert abs(a[1] - 0.41) < 1e-9
        api.place_market_sell.assert_not_called()

    def test_naked_skips_no_sell(self):
        monitor, api, db = self._setup(0.30, 100, [(0.31, 500)], [(0.33, 500)])
        monitor._cost_lots = lambda a, s, c: (None, [])
        monitor.check_exit()
        api.place_limit_sell.assert_not_called()
        api.place_market_sell.assert_not_called()
        api.place_marketable_limit_sell.assert_not_called()

    def test_rest_keeps_existing_matching_sell(self):
        sells = [
            {
                "id": "s1",
                "asset_id": "A-y",
                "side": "SELL",
                "price": "0.33",
                "original_size": "100",
                "size_matched": "0",
            }
        ]
        monitor, api, db = self._setup(
            0.30, 100, [(0.31, 500)], [(0.33, 500)], sells=sells
        )
        monitor.check_exit()
        api.place_limit_sell.assert_not_called()
        api.cancel_orders.assert_not_called()
