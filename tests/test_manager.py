"""tests/test_manager.py"""

import time
import pytest
from unittest.mock import MagicMock, patch
from engine.manager import EngineManager, WalletWorker


def _make_manager():
    db = MagicMock()
    db.get_settings.return_value = {
        "scan_interval_sec": 30,
        "fill_check_interval_sec": 5,
        "cooldown_minutes": 20,
        "stop_loss_pct": 15.0,
        "min_reward_usd": 100.0,
        "max_spread_cents": 3.0,
        "min_price_cents": 10.0,
        "max_price_cents": 50.0,
        "min_settlement_days": 4,
    }
    db.list_wallets.return_value = [
        {"address": "0xABC", "encrypted_key": "enc1", "enabled": 1},
        {"address": "0xDEF", "encrypted_key": "enc2", "enabled": 1},
    ]
    db.get_open_buy_orders.return_value = []
    manager = EngineManager(db, encryption_key=b"x" * 32)
    return manager, db


class TestEngineLifecycle:
    def test_start_creates_threads(self):
        manager, db = _make_manager()
        with patch("engine.manager.decrypt", return_value="0x_fake_key"):
            with patch("engine.manager.PolymarketAPI"):
                manager.start_all()
                assert len(manager.engines) == 2
                manager.stop_all()

    def test_stop_cancels_buy_orders(self):
        manager, db = _make_manager()
        mock_api = MagicMock()
        mock_api.get_open_orders.return_value = [
            {"id": "o1", "side": "BUY"},
            {"id": "o2", "side": "SELL"},
        ]
        with patch("engine.manager.decrypt", return_value="0x_fake_key"):
            with patch("engine.manager.PolymarketAPI", return_value=mock_api):
                manager.start_all()
                # Reset mocks after start_all so the assertion only counts cancels from stop()
                mock_api.cancel_orders.reset_mock()
                manager.stop_all()
                # Batched cancel: cancel_orders called once per wallet (2 wallets),
                # each call with only the BUY order ids
                assert mock_api.cancel_orders.call_count == 2
                assert all(
                    c.args == (["o1"],) for c in mock_api.cancel_orders.call_args_list
                )

    def test_get_status(self):
        manager, db = _make_manager()
        status = manager.get_status()
        assert isinstance(status, dict)
        assert "engines" in status


class TestWalletWorkerTick:
    def test_tick_runs_take_profit_between_fills_and_stoploss(self):
        worker = WalletWorker(
            MagicMock(), MagicMock(), "0xABC", {"fill_check_interval_sec": 5}
        )
        worker.monitor = MagicMock()

        worker._tick()

        worker.monitor.begin_status_tick.assert_called_once()
        worker.monitor.check_buy_orders.assert_called_once()
        worker.monitor.check_take_profit.assert_called_once()
        worker.monitor.check_stop_loss.assert_called_once()
        worker.monitor.check_sell_orders.assert_called_once()
        worker.monitor.publish_status.assert_called_once()


class TestTestPlaceOrders:
    def test_no_eligible_markets_returns_scan_hint(self):
        manager, db = _make_manager()
        manager.eligible_markets = []
        result = manager.test_place_orders()
        assert result == {"ok": False, "message": "请先扫描市场"}

    def test_no_enabled_wallet_returns_error(self):
        manager, db = _make_manager()
        db.list_wallets.return_value = [
            {"address": "0xABC", "encrypted_key": "e1", "enabled": 0},
        ]
        manager.eligible_markets = [{"market_competitiveness": 0.5}]
        result = manager.test_place_orders()
        assert result == {"ok": False, "message": "没有启用的钱包"}

    def test_no_running_worker_builds_transient_api_and_places(self):
        manager, db = _make_manager()
        manager.eligible_markets = [{"market_competitiveness": 0.5, "name": "m"}]
        # engines empty -> first enabled wallet has no running worker;
        # must construct a transient API/worker just to place the orders.
        fake_worker = MagicMock()
        with patch("engine.manager.decrypt", return_value="0xkey"), patch(
            "engine.manager.PolymarketAPI"
        ) as mock_api_cls, patch(
            "engine.manager.WalletWorker", return_value=fake_worker
        ):
            result = manager.test_place_orders()
        assert result["ok"] is True
        mock_api_cls.assert_called_once()
        fake_worker.place_orders.assert_called_once()
        _, kwargs = fake_worker.place_orders.call_args
        assert kwargs.get("limit") == 3
        # transient worker must NOT start a monitor thread
        fake_worker.start.assert_not_called()

    def test_places_on_first_enabled_running_worker_with_limit_3(self):
        manager, db = _make_manager()
        manager.eligible_markets = [
            {"market_competitiveness": 0.9, "name": "high"},
            {"market_competitiveness": 0.1, "name": "low"},
        ]
        worker = MagicMock()
        worker.running = True
        # db.list_wallets()[0] is 0xABC (enabled) per _make_manager
        manager.engines = {"0xABC": worker}
        result = manager.test_place_orders()
        assert result["ok"] is True
        worker.place_orders.assert_called_once()
        args, kwargs = worker.place_orders.call_args
        passed_markets = args[0]
        # sorted ascending by competitiveness: low (0.1) before high (0.9)
        assert [m["name"] for m in passed_markets] == ["low", "high"]
        assert kwargs.get("limit") == 3

    def test_place_orders_exception_returns_error_dict(self):
        manager, db = _make_manager()
        manager.eligible_markets = [{"market_competitiveness": 0.5}]
        worker = MagicMock()
        worker.running = True
        worker.place_orders.side_effect = RuntimeError("boom")
        manager.engines = {"0xABC": worker}
        result = manager.test_place_orders()
        assert result["ok"] is False
        assert "boom" in result["message"]

    def test_skips_disabled_or_not_running_picks_first_valid(self):
        manager, db = _make_manager()
        db.list_wallets.return_value = [
            {"address": "0xABC", "encrypted_key": "e1", "enabled": 0},
            {"address": "0xDEF", "encrypted_key": "e2", "enabled": 1},
        ]
        manager.eligible_markets = [{"market_competitiveness": 0.5, "name": "m"}]
        stopped = MagicMock()
        stopped.running = False
        good = MagicMock()
        good.running = True
        manager.engines = {"0xABC": stopped, "0xDEF": good}
        result = manager.test_place_orders()
        assert result["ok"] is True
        good.place_orders.assert_called_once()
        stopped.place_orders.assert_not_called()


class TestScanMarketsLastScanTime:
    def test_last_scan_time_only_updates_at_round_completion(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()  # skip API-construction branch

        observed = []

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def scan(self, on_progress=None, on_found=None):
                on_found({"market_id": "m1"})
                observed.append(manager.last_scan_time)
                on_found({"market_id": "m2"})
                observed.append(manager.last_scan_time)
                return [{"market_id": "m1"}, {"market_id": "m2"}]

        assert manager.last_scan_time == 0
        with patch("engine.manager.MarketScanner", FakeScanner):
            manager.scan_markets()

        assert observed == [0, 0]
        assert manager.last_scan_time > 0
        assert manager.scan_status == "done"
        assert manager.eligible_markets == [{"market_id": "m1"}, {"market_id": "m2"}]


class TestSharedScanWithStatus:
    def test_manual_scan_sets_scanning_then_done(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        seen = []

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def scan(self, on_progress=None, on_found=None):
                on_progress(1, 2, "checking")
                seen.append(manager.scan_status)
                on_found({"market_id": "m1"})
                return [{"market_id": "m1"}]

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager.scan_markets()

        assert seen == ["scanning"]
        assert manager.scan_status == "done"
        assert manager.last_scan_time > 0
        assert manager.eligible_markets == [{"market_id": "m1"}]
        db.save_eligible_markets.assert_called_once_with([{"market_id": "m1"}])

    def test_auto_do_scan_reports_status_and_distributes(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xABC": worker}
        seen = []

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def scan(self, on_progress=None, on_found=None):
                on_progress(3, 3, "done-ish")
                seen.append(manager.scan_status)
                return [{"market_id": "m9"}]

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager._do_scan()

        assert seen == ["scanning"]
        assert manager.scan_status == "done"
        assert manager.last_scan_time > 0
        worker.place_orders.assert_called_once_with([{"market_id": "m9"}])
        db.save_eligible_markets.assert_not_called()

    def test_scan_failure_resets_status_and_keeps_last_scan_time(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager.last_scan_time = 12345.0
        manager.eligible_markets = [{"market_id": "prev"}]

        class BoomScanner:
            def __init__(self, api, db, addr):
                pass

            def scan(self, on_progress=None, on_found=None):
                raise RuntimeError("scanner blew up")

        with patch("engine.manager.MarketScanner", BoomScanner):
            with pytest.raises(RuntimeError):
                manager._scan_with_status()

        assert manager.scan_status == "done"
        assert manager.last_scan_time == 12345.0
        assert manager.eligible_markets == [{"market_id": "prev"}]
