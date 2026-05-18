"""tests/test_manager.py"""

import time
import pytest
from unittest.mock import MagicMock, patch
from engine.manager import EngineManager


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
                # Reset after startup_recovery calls
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
