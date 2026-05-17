"""tests/test_scanner.py"""

import time
import pytest
from unittest.mock import MagicMock
from engine.scanner import MarketScanner


def _make_scanner(balance=500.0, settings=None):
    """Create a scanner with mocked API and DB."""
    api = MagicMock()
    db = MagicMock()

    api.get_balance.return_value = balance

    default_settings = {
        "min_reward_usd": 100.0,
        "max_spread_cents": 3.0,
        "min_price_cents": 10.0,
        "max_price_cents": 50.0,
        "min_settlement_days": 4,
    }
    if settings:
        default_settings.update(settings)
    db.get_settings.return_value = default_settings
    db.is_in_cooldown.return_value = False

    return MarketScanner(api, db, "0xABC"), api, db


def _sample_market(**overrides):
    base = {
        "market_id": "mkt1",
        "token_id": "tok1",
        "market_name": "Test Market",
        "end_date": time.time() + 86400 * 10,  # 10 days from now
        "reward_usd": 200.0,
        "max_spread": 2,
        "min_size": 100,
        "reward_range_min": 0.10,
        "reward_range_max": 0.50,
        "tick_size": 0.01,
    }
    base.update(overrides)
    return base


def _sample_orderbook():
    return {
        "bids": [
            {"price": "0.30", "size": "3000"},
            {"price": "0.29", "size": "500"},
        ],
        "asks": [
            {"price": "0.31", "size": "1000"},
        ],
    }


class TestMarketFiltering:
    def test_accepts_valid_market(self):
        scanner, api, db = _make_scanner(balance=500.0)
        api.get_rewards_markets.return_value = [_sample_market()]
        api.get_orderbook.return_value = _sample_orderbook()

        results = scanner.scan()
        assert len(results) == 1

    def test_rejects_low_reward(self):
        scanner, api, db = _make_scanner()
        api.get_rewards_markets.return_value = [_sample_market(reward_usd=50.0)]
        api.get_orderbook.return_value = _sample_orderbook()

        results = scanner.scan()
        assert len(results) == 0

    def test_rejects_near_settlement(self):
        scanner, api, db = _make_scanner()
        api.get_rewards_markets.return_value = [
            _sample_market(end_date=time.time() + 86400 * 2)  # 2 days
        ]
        results = scanner.scan()
        assert len(results) == 0

    def test_rejects_wide_spread(self):
        scanner, api, db = _make_scanner()
        api.get_rewards_markets.return_value = [_sample_market()]
        api.get_orderbook.return_value = {
            "bids": [{"price": "0.25", "size": "1000"}],
            "asks": [{"price": "0.30", "size": "1000"}],  # 5-cent spread
        }
        results = scanner.scan()
        assert len(results) == 0

    def test_rejects_price_out_of_range(self):
        scanner, api, db = _make_scanner()
        api.get_rewards_markets.return_value = [_sample_market()]
        api.get_orderbook.return_value = {
            "bids": [{"price": "0.05", "size": "1000"}],  # below 10 cents
            "asks": [{"price": "0.06", "size": "1000"}],
        }
        results = scanner.scan()
        assert len(results) == 0

    def test_rejects_insufficient_balance(self):
        scanner, api, db = _make_scanner(balance=1.0)  # Only $1
        api.get_rewards_markets.return_value = [
            _sample_market(min_size=1000)  # 1000 * 0.30 = $300 needed
        ]
        api.get_orderbook.return_value = _sample_orderbook()

        results = scanner.scan()
        assert len(results) == 0

    def test_rejects_cooldown_market(self):
        scanner, api, db = _make_scanner()
        db.is_in_cooldown.return_value = True
        api.get_rewards_markets.return_value = [_sample_market()]
        api.get_orderbook.return_value = _sample_orderbook()

        results = scanner.scan()
        assert len(results) == 0
