"""tests/test_scanner.py — Tests updated to match Polymarket API response format."""

import time
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from engine.scanner import MarketScanner


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


class TestDiscoverAndRefreshSplit:
    def test_discover_candidates_has_no_orderbooks(self):
        api = MagicMock()
        api.get_rewards_markets.return_value = [
            {"condition_id": "C", "tokens": [{"token_id": "C-y"}], "rewards_config": []}
        ]
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        pool = scanner.discover_candidates(
            [{"excluded_categories": [], "min_reward_usd": 0}]
        )
        assert pool and all("_orderbooks" not in m for m in pool)
        api.get_spread.assert_not_called()  # 发现阶段不抓订单簿
        api.get_orderbook.assert_not_called()

    def test_refresh_orderbooks_fills_and_overwrites(self):
        api = MagicMock()
        api.get_spread.return_value = 0.01
        api.get_orderbook.return_value = {
            "bids": [{"price": "0.30", "size": "100"}],
            "asks": [{"price": "0.31", "size": "100"}],
            "tick_size": "0.01",
        }
        scanner = MarketScanner(api, MagicMock(), "")
        pool = [
            {
                "condition_id": "A",
                "tokens": [{"token_id": "A-y"}],
                "_orderbooks": {"STALE": {}},
            }  # 旧簿应被覆盖
        ]
        scanner.refresh_orderbooks(pool)
        assert "A-y" in pool[0]["_orderbooks"]
        assert "STALE" not in pool[0]["_orderbooks"]  # 覆盖写,不留陈旧

    def test_fetch_candidates_still_includes_orderbooks(self):
        api = MagicMock()
        api.get_rewards_markets.return_value = [
            {"condition_id": "C", "tokens": [{"token_id": "C-y"}], "rewards_config": []}
        ]
        api.get_spread.return_value = 0.01
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        pool = scanner.fetch_candidates(
            [{"excluded_categories": [], "min_reward_usd": 0}]
        )
        assert all("_orderbooks" in m for m in pool)


class TestFilterForTemplate:
    def _candidate(self, cid, tags, daily_reward=50, bid=0.30, ask=0.31, min_size=100):
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
            "rewards_min_size": min_size,
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

    def test_eligible_entry_has_no_price(self):
        scanner = self._scanner()
        pool = [self._candidate("B", [])]
        out = scanner.filter_for_template(pool, self._template(), "0xW")
        assert out and all("order_price" not in e for e in out)
        e = out[0]
        assert e["token_id"] and "rewards_min_size" in e and "rewards_max_spread" in e

    def test_per_share_below_threshold_excluded(self):
        scanner = self._scanner()
        # market_reward 20 / min_size 100 = 0.20 < 0.30 默认 -> 剔除
        pool = [self._candidate("A", [], daily_reward=20)]
        assert scanner.filter_for_template(pool, self._template(), "0xW") == []

    def test_per_share_at_or_above_threshold_passes(self):
        scanner = self._scanner()
        # 50 / 100 = 0.50 >= 0.30 -> 通过
        pool = [self._candidate("B", [], daily_reward=50)]
        out = scanner.filter_for_template(pool, self._template(), "0xW")
        assert any(e["market_id"] == "B" for e in out)

    def test_min_size_over_250_excluded(self):
        scanner = self._scanner()
        # 单份奖励够高且总额够,但 min_size 300 > 250 -> 剔除
        pool = [self._candidate("C", [], daily_reward=200, min_size=300)]
        assert scanner.filter_for_template(pool, self._template(), "0xW") == []

    def test_per_bracket_thresholds_independent(self):
        scanner = self._scanner()
        # P: min_size 100 单份 0.40;Q: min_size 250 单份 0.40
        # 档100 阈值调 0.50(剔 P),档250 留 0.30(过 Q)
        c_100 = self._candidate("P", [], daily_reward=40, min_size=100)
        c_250 = self._candidate("Q", [], daily_reward=100, min_size=250)
        tmpl = self._template(per_share_reward_thresholds={"100": 0.50, "250": 0.30})
        out = scanner.filter_for_template([c_100, c_250], tmpl, "0xW")
        ids = {e["market_id"] for e in out}
        assert "P" not in ids and "Q" in ids

    def test_rewards_min_size_exact_match_only(self):
        scanner = self._scanner()
        pool = [
            self._candidate("A", [], daily_reward=20, min_size=20),
            self._candidate("B", [], daily_reward=50, min_size=50),
        ]
        tmpl = self._template(rewards_min_size_min=20, rewards_min_size_max=20)
        out = scanner.filter_for_template(pool, tmpl, "0xW")
        assert {e["rewards_min_size"] for e in out} == {20}

    def test_rewards_min_size_default_range_passes_all(self):
        scanner = self._scanner()
        pool = [
            self._candidate("A", [], daily_reward=20, min_size=20),
            self._candidate("B", [], daily_reward=50, min_size=50),
        ]
        out = scanner.filter_for_template(pool, self._template(), "0xW")
        assert {e["rewards_min_size"] for e in out} == {20, 50}


class TestRewardBracket:
    def test_upward_bracket_mapping(self):
        from engine.scanner import reward_bracket

        assert reward_bracket(20) == 20
        assert reward_bracket(21) == 50
        assert reward_bracket(50) == 50
        assert reward_bracket(100) == 100
        assert reward_bracket(101) == 200
        assert reward_bracket(200) == 200
        assert reward_bracket(250) == 250

    def test_over_250_or_nonpositive_is_none(self):
        from engine.scanner import reward_bracket

        assert reward_bracket(251) is None
        assert reward_bracket(0) is None
        assert reward_bracket(-5) is None
