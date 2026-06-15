"""tests/test_scanner.py — Tests updated to match Polymarket API response format."""

import time
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from engine.scanner import MarketScanner


def _make_scanner(balance=500.0, settings=None):
    """Create a scanner with mocked API and DB."""
    api = MagicMock()
    db = MagicMock()

    api.get_balance.return_value = balance
    # Default: get_spread returns 0.01 (1 cent, passes 3-cent filter)
    api.get_spread.return_value = 0.01
    # Default: get_rewards_for_market returns matching rewards_config
    api.get_rewards_for_market.return_value = [
        {"rewards_config": [{"rate_per_day": 150.0}]}
    ]

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
    db.get_default_template_id.return_value = 1
    db.get_template.return_value = default_settings
    db.get_template_for.return_value = default_settings
    db.is_in_cooldown.return_value = False
    db.get_blacklist_ids.return_value = set()

    return MarketScanner(api, db, "0xABC"), api, db


def _future_date(days=10):
    """Return a date string N days in the future."""
    dt = datetime.now() + timedelta(days=days)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _sample_market(**overrides):
    """Create a sample market matching Polymarket /rewards/markets/multi format."""
    base = {
        "condition_id": "0xabc123",
        "market_id": "12345",
        "question": "Test Market?",
        "end_date": _future_date(10),
        "rewards_max_spread": 2,
        "rewards_min_size": 100,
        "spread": 0.01,
        "tokens": [
            {"token_id": "tok1", "outcome": "Yes", "price": 0.30},
        ],
        "rewards_config": [
            {
                "rate_per_day": 150.0,
                "total_rewards": 5000,
                "end_date": "2500-12-31",
            }
        ],
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
        assert results[0]["market_id"] == "0xabc123"
        assert results[0]["token_id"] == "tok1"
        # min_cost = rewards_min_size(100) * lowest tick in reward range.
        # mid=0.305, max_spread=2, tick=0.01 -> range_min=0.285 -> ceil tick 0.29
        assert results[0]["min_cost"] == pytest.approx(100 * 0.29)

    def test_rejects_low_reward(self):
        scanner, api, db = _make_scanner()
        api.get_rewards_markets.return_value = [
            _sample_market(
                rewards_config=[{"rate_per_day": 50.0, "total_rewards": 100}]
            )
        ]
        api.get_orderbook.return_value = _sample_orderbook()

        results = scanner.scan()
        assert len(results) == 0

    def test_rejects_near_settlement(self):
        scanner, api, db = _make_scanner()
        api.get_rewards_markets.return_value = [
            _sample_market(end_date=_future_date(2))  # 2 days
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
        api.get_rewards_markets.return_value = [
            _sample_market(
                tokens=[{"token_id": "tok1", "outcome": "Yes", "price": 0.05}]
            )
        ]
        api.get_orderbook.return_value = {
            "bids": [{"price": "0.05", "size": "1000"}],
            "asks": [{"price": "0.06", "size": "1000"}],
        }
        results = scanner.scan()
        assert len(results) == 0

    def test_low_balance_still_eligible_with_min_cost(self):
        # Scanning no longer filters by balance (each wallet has its own).
        # The market is listed and carries a min_cost threshold instead.
        scanner, api, db = _make_scanner(balance=1.0)  # Only $1
        api.get_rewards_markets.return_value = [_sample_market(rewards_min_size=1000)]
        api.get_orderbook.return_value = _sample_orderbook()

        results = scanner.scan()
        assert len(results) == 1
        # 1000 * lowest tick in reward range (0.29) = 290
        assert results[0]["min_cost"] == pytest.approx(1000 * 0.29)

    def test_rejects_cooldown_market(self):
        scanner, api, db = _make_scanner()
        db.is_in_cooldown.return_value = True
        api.get_rewards_markets.return_value = [_sample_market()]
        api.get_orderbook.return_value = _sample_orderbook()

        results = scanner.scan()
        assert len(results) == 0

    def test_evaluates_each_token_independently(self):
        """Each token (YES/NO) in a market should be evaluated separately."""
        scanner, api, db = _make_scanner(balance=500.0)
        api.get_rewards_markets.return_value = [
            _sample_market(
                tokens=[
                    {"token_id": "tok_yes", "outcome": "Yes", "price": 0.30},
                    {"token_id": "tok_no", "outcome": "No", "price": 0.70},
                ]
            )
        ]

        # tok_yes gets valid orderbook, tok_no price is out of range (70 > 50 cents)
        def mock_orderbook(token_id):
            if token_id == "tok_yes":
                return _sample_orderbook()
            return {
                "bids": [
                    {"price": "0.70", "size": "3000"},
                    {"price": "0.69", "size": "500"},
                ],
                "asks": [{"price": "0.71", "size": "1000"}],
            }

        api.get_orderbook.side_effect = mock_orderbook

        results = scanner.scan()
        # tok_yes should pass, tok_no should be rejected (price > 50 cents)
        assert len(results) == 1
        assert results[0]["token_id"] == "tok_yes"


def test_scan_upserts_market_meta_with_name_and_slugs():
    scanner, api, db = _make_scanner()
    api.get_rewards_markets.return_value = [
        _sample_market(market_slug="ms-1", event_slug="es-1")
    ]
    api.get_orderbook.return_value = _sample_orderbook()
    scanner.scan()
    db.upsert_market_meta.assert_any_call("0xabc123", "Test Market?", "ms-1", "es-1")


def test_scan_excludes_blacklisted_market():
    scanner, api, db = _make_scanner()
    db.get_blacklist_ids.return_value = {"0xabc123"}  # _sample_market 的 condition_id
    api.get_rewards_markets.return_value = [_sample_market()]
    api.get_orderbook.return_value = _sample_orderbook()
    results = scanner.scan()
    assert len(results) == 0


class TestFetchCandidatesCategoryWiring:
    def test_queries_full_plus_each_category_and_subtracts(self):
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [
                    {"condition_id": "A", "tokens": [], "rewards_config": []},
                    {"condition_id": "B", "tokens": [], "rewards_config": []},
                    {"condition_id": "C", "tokens": [], "rewards_config": []},
                ]
            return {
                "sports": [{"condition_id": "A"}],
                "weather": [{"condition_id": "B"}],
                "esports": [],
            }.get(tag_slug, [])

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        templates = [
            {"excluded_categories": ["sports", "weather"], "min_reward_usd": 0},
            {
                "excluded_categories": ["sports", "weather", "esports"],
                "min_reward_usd": 0,
            },
        ]
        pool = scanner.fetch_candidates(templates, skip_orderbook=True)
        assert {m["condition_id"] for m in pool} == {"C"}

    def test_no_price_computed(self):
        api = MagicMock()
        api.get_rewards_markets.return_value = [
            {"condition_id": "C", "tokens": [], "rewards_config": []}
        ]
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        pool = scanner.fetch_candidates(
            [{"excluded_categories": [], "min_reward_usd": 0}], skip_orderbook=True
        )
        assert all("order_price" not in m for m in pool)


class TestFilterForTemplate:
    def _candidate(self, cid, tags, daily_reward=50, bid=0.30, ask=0.31):
        bid2 = round(bid - 0.01, 2)
        return {
            "condition_id": cid,
            "question": "M",
            "market_slug": "",
            "event_slug": "",
            "market_competitiveness": 0,
            "end_date": "",
            "neg_risk": False,
            "rewards_max_spread": 2,
            "rewards_min_size": 100,
            "tags": tags,
            "market_reward": daily_reward,
            "tokens": [{"token_id": cid + "-y", "outcome": "Yes", "price": bid}],
            "rewards_config": [{"rate_per_day": daily_reward}],
            "_orderbooks": {
                cid
                + "-y": {
                    "bids": [
                        {"price": str(bid), "size": "3000"},
                        {"price": str(bid2), "size": "500"},
                    ],
                    "asks": [{"price": str(ask), "size": "5000"}],
                    "tick_size": "0.01",
                    "spread": ask - bid,
                }
            },
        }

    def _template(self, **over):
        t = {
            "min_reward_usd": 6,
            "min_price_cents": 10,
            "max_price_cents": 90,
            "max_spread_cents": 6,
            "min_settlement_days": 0,
            "excluded_categories": [],
        }
        t.update(over)
        return t

    def _scanner(self):
        db = MagicMock()
        db.is_in_cooldown.return_value = False  # filter_for_template 会查冷却
        return MarketScanner(MagicMock(), db, "")

    def test_category_narrow_drops_excluded_tag(self):
        scanner = self._scanner()
        pool = [self._candidate("A", ["esports"]), self._candidate("B", [])]
        out = scanner.filter_for_template(
            pool, self._template(excluded_categories=["esports"]), "0xW"
        )
        ids = {e["market_id"] for e in out}
        assert "A" not in ids and "B" in ids

    def test_reward_floor_filters(self):
        scanner = self._scanner()
        pool = [self._candidate("C", [], daily_reward=3)]
        assert scanner.filter_for_template(pool, self._template(), "0xW") == []

    def test_cooldown_market_skipped(self):
        db = MagicMock()
        db.is_in_cooldown.return_value = True
        scanner = MarketScanner(MagicMock(), db, "")
        pool = [self._candidate("B", [])]
        assert scanner.filter_for_template(pool, self._template(), "0xW") == []

    def test_two_templates_yield_different_lists(self):
        scanner = self._scanner()
        pool = [self._candidate("A", ["esports"]), self._candidate("B", [])]
        strict = {
            e["market_id"]
            for e in scanner.filter_for_template(
                pool, self._template(excluded_categories=["esports"]), "0xW"
            )
        }
        loose = {
            e["market_id"]
            for e in scanner.filter_for_template(
                pool, self._template(excluded_categories=[]), "0xW"
            )
        }
        assert strict != loose and "A" in loose and "A" not in strict
