"""tests/test_scanner.py — Tests updated to match Polymarket API response format."""

import time
import pytest
from datetime import datetime, timedelta, date, timezone
from unittest.mock import MagicMock
from engine.scanner import (
    MarketScanner,
    ScanSuperseded,
    market_age_hours,
    loosest_new_market_hours,
)


def _tier(size, shares=None, **over):
    t = {
        "size": size,
        "enabled": True,
        "shares": shares or size,
        "rule1_min_coeff": 0,
        "rule2_min_coeff": 0,
        "rule3_min_coeff": 0,
        "gap_high_coeff_sum_min": 20,
        "amount_value_table": [{"upper": 1.0, "value": 1}],
    }
    t.update(over)
    return t


def _created_hours_ago(hours: float) -> str:
    """构造「hours 小时前创建」的 created_at（UTC，奖励端点的格式）。"""
    return datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


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
    def test_whitelist_keeps_only_included_union(self):
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [
                    {"condition_id": "A", "tokens": [], "rewards_config": []},
                    {"condition_id": "B", "tokens": [], "rewards_config": []},
                    {"condition_id": "C", "tokens": [], "rewards_config": []},
                ]
            return {
                "politics": [{"condition_id": "A"}],
                "economy": [{"condition_id": "B"}],
            }.get(tag_slug, [])

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        templates = [
            {
                "included_categories": ["politics"],
                "include_other": False,
                "min_reward_usd": 0,
            },
            {
                "included_categories": ["economy"],
                "include_other": False,
                "min_reward_usd": 0,
            },
        ]
        pool = scanner.fetch_candidates(templates, skip_orderbook=True)
        # A(politics)+B(economy) 收;C 无 curated 标签且无人收其他 -> 丢
        assert {m["condition_id"] for m in pool} == {"A", "B"}

    def test_include_other_keeps_untagged(self):
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [
                    {"condition_id": "A", "tokens": [], "rewards_config": []},
                    {"condition_id": "C", "tokens": [], "rewards_config": []},
                ]
            return {"politics": [{"condition_id": "A"}]}.get(tag_slug, [])

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        templates = [
            {
                "included_categories": ["politics"],
                "include_other": True,
                "min_reward_usd": 0,
            }
        ]
        pool = scanner.fetch_candidates(templates, skip_orderbook=True)
        assert {m["condition_id"] for m in pool} == {"A", "C"}  # C 落入其他

    def test_slug_query_failure_tolerated(self):
        # 单个品类查询抛错(奖励端点偶发 500)不该拖垮整轮发现:该 slug 记空集,
        # 冷启动无缓存池时也仍能产出候选池。
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [
                    {"condition_id": "A", "tokens": [], "rewards_config": []},
                    {"condition_id": "C", "tokens": [], "rewards_config": []},
                ]
            if tag_slug == "politics":
                return [{"condition_id": "A"}]
            raise RuntimeError("boom")  # 其余品类查询全挂

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        templates = [
            {
                "included_categories": ["politics"],
                "include_other": True,
                "min_reward_usd": 0,
            }
        ]
        pool = scanner.fetch_candidates(templates, skip_orderbook=True)
        # A 命中 politics(查询成功);C 无标签(其余 slug 失败记空集)-> 落其他,include_other 收。
        # 关键:整轮未因单 slug 抛错而崩。
        assert {m["condition_id"] for m in pool} == {"A", "C"}

    def test_no_price_computed(self):
        api = MagicMock()
        api.get_rewards_markets.return_value = [
            {"condition_id": "C", "tokens": [], "rewards_config": []}
        ]
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        pool = scanner.fetch_candidates(
            [{"included_categories": [], "include_other": True, "min_reward_usd": 0}],
            skip_orderbook=True,
        )
        assert all("order_price" not in m for m in pool)

    def test_tag_market_not_in_full_still_included(self):
        # W 只在 weather tag 查询里,不在(不带品类的)full —— 修前被 tag_pool 丢掉。
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [{"condition_id": "HI", "tokens": [], "rewards_config": []}]
            if tag_slug == "weather":
                return [{"condition_id": "W", "tokens": [], "rewards_config": []}]
            return []

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        pool = scanner.fetch_candidates(
            [
                {
                    "included_categories": ["weather"],
                    "include_other": False,
                    "min_reward_usd": 0,
                }
            ],
            skip_orderbook=True,
        )
        assert {m["condition_id"] for m in pool} == {"W"}

    def test_no_untagged_full_fetch_when_not_include_other(self):
        # include_other=False 不应触发不带品类的 full 抓取。
        def fake_rewards(tag_slug=None, **kw):
            return (
                [{"condition_id": "W", "tokens": [], "rewards_config": []}]
                if tag_slug == "weather"
                else []
            )

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        scanner.fetch_candidates(
            [
                {
                    "included_categories": ["weather"],
                    "include_other": False,
                    "min_reward_usd": 0,
                }
            ],
            skip_orderbook=True,
        )
        assert all(
            c.kwargs.get("tag_slug") is not None
            for c in api.get_rewards_markets.call_args_list
        ), "include_other=False 不应调用不带品类的 get_rewards_markets"

    def test_same_market_in_two_slugs_deduped(self):
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug in ("weather", "politics"):
                return [{"condition_id": "X", "tokens": [], "rewards_config": []}]
            return []

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        pool = scanner.fetch_candidates(
            [
                {
                    "included_categories": ["weather", "politics"],
                    "include_other": False,
                    "min_reward_usd": 0,
                }
            ],
            skip_orderbook=True,
        )
        assert [m["condition_id"] for m in pool] == ["X"]

    def test_max_pages_threaded_to_every_rewards_call(self):
        def fake_rewards(tag_slug=None, max_pages=5, **kw):
            return (
                [{"condition_id": "W", "tokens": [], "rewards_config": []}]
                if tag_slug == "weather"
                else []
            )

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        scanner.fetch_candidates(
            [
                {
                    "included_categories": ["weather"],
                    "include_other": False,
                    "min_reward_usd": 0,
                }
            ],
            skip_orderbook=True,
            max_pages=17,
        )
        assert api.get_rewards_markets.call_args_list  # 至少调用过一次
        for c in api.get_rewards_markets.call_args_list:
            assert c.kwargs.get("max_pages") == 17


class TestDiscoverAndRefreshSplit:
    def test_discover_candidates_has_no_orderbooks(self):
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [
                    {
                        "condition_id": "C",
                        "tokens": [{"token_id": "C-y"}],
                        "rewards_config": [],
                    }
                ]
            return []  # C 无 curated 标签 -> 落「其他」

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        pool = scanner.discover_candidates(
            [{"included_categories": [], "include_other": True, "min_reward_usd": 0}]
        )
        assert pool and all("_orderbooks" not in m for m in pool)
        api.get_spread.assert_not_called()  # 发现阶段不抓订单簿
        api.get_orderbook.assert_not_called()

    def test_refresh_orderbooks_takes_whole_pool_in_one_batch(self):
        """整池所有 token 一次批量取完,不再每 token 一个请求。

        刷簿的请求数是 O(候选池 × 每市场 token 数):~180 市场就是 360 次单发往返,
        过代理时这一步能吃掉几十分钟(2026-07-04 实盘:filter 拿不到簿 -> 不挂单)。
        """
        ob = {
            "bids": [{"price": "0.30", "size": "100"}],
            "asks": [{"price": "0.31", "size": "100"}],
            "tick_size": "0.01",
        }
        api = MagicMock()
        api.get_orderbooks.return_value = {"A-y": ob, "A-n": ob, "B-y": ob}
        scanner = MarketScanner(api, MagicMock(), "")
        pool = [
            {
                "condition_id": "A",
                "tokens": [{"token_id": "A-y"}, {"token_id": "A-n"}],
            },
            {"condition_id": "B", "tokens": [{"token_id": "B-y"}]},
        ]

        scanner.refresh_orderbooks(pool)

        assert api.get_orderbook.call_count == 0, "不该再每 token 单发"
        assert api.get_orderbooks.call_count == 1, "整池一次拿完"
        assert set(api.get_orderbooks.call_args[0][0]) == {"A-y", "A-n", "B-y"}
        # 分发回各自的市场,不能串台
        assert set(pool[0]["_orderbooks"]) == {"A-y", "A-n"}
        assert set(pool[1]["_orderbooks"]) == {"B-y"}

    def test_refresh_orderbooks_skips_tokens_the_batch_did_not_return(self):
        """批量没返回的 token 不进该市场的簿(等价于旧的单发失败 continue)。

        filter 对没有簿的 token 会跳过;写进去一个空壳簿反而会被当成真实盘口判定。
        """
        ob = {"bids": [], "asks": [], "tick_size": "0.01"}
        api = MagicMock()
        api.get_orderbooks.return_value = {"A-y": ob, "A-n": None}
        scanner = MarketScanner(api, MagicMock(), "")
        pool = [
            {"condition_id": "A", "tokens": [{"token_id": "A-y"}, {"token_id": "A-n"}]}
        ]

        scanner.refresh_orderbooks(pool)

        assert set(pool[0]["_orderbooks"]) == {"A-y"}

    def test_refresh_orderbooks_fills_and_overwrites(self):
        api = MagicMock()
        api.get_spread.return_value = 0.01
        api.get_orderbooks.side_effect = lambda ids: {
            t: {
                "bids": [{"price": "0.30", "size": "100"}],
                "asks": [{"price": "0.31", "size": "100"}],
                "tick_size": "0.01",
            }
            for t in ids
        }
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
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [
                    {
                        "condition_id": "C",
                        "tokens": [{"token_id": "C-y"}],
                        "rewards_config": [],
                    }
                ]
            return []  # C 无 curated 标签 -> 落「其他」

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        api.get_spread.return_value = 0.01
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        pool = scanner.fetch_candidates(
            [{"included_categories": [], "include_other": True, "min_reward_usd": 0}]
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
            "included_categories": ["esports", "politics"],
            "include_other": True,
            "size_tiers": [_tier(100)],
        }
        t.update(over)
        return t

    def _scanner(self):
        db = MagicMock()
        db.is_in_cooldown.return_value = False  # filter_for_template 会查冷却
        return MarketScanner(MagicMock(), db, "")

    def test_category_whitelist_keeps_only_included(self):
        scanner = self._scanner()
        pool = [self._candidate("A", ["esports"]), self._candidate("B", ["politics"])]
        out = scanner.filter_for_template(
            pool,
            self._template(included_categories=["politics"], include_other=False),
            "0xW",
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
        pool = [self._candidate("A", ["esports"]), self._candidate("B", ["politics"])]
        strict = {
            e["market_id"]
            for e in scanner.filter_for_template(
                pool,
                self._template(included_categories=["politics"], include_other=False),
                "0xW",
            )
        }
        loose = {
            e["market_id"]
            for e in scanner.filter_for_template(
                pool,
                self._template(
                    included_categories=["politics", "esports"], include_other=False
                ),
                "0xW",
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

    def test_no_matching_tier_excluded(self):
        scanner = self._scanner()
        # 奖励够高,但 min_size 300 没有任何已启用档位模块能精确对上 -> 剔除。
        pool = [self._candidate("C", [], daily_reward=200, min_size=300)]
        assert scanner.filter_for_template(pool, self._template(), "0xW") == []

    # --- 结算窗口 [最短, 最长](按整天:0=今天,1=明天…)---
    def _on_day(self, cid, n):
        """构造一个「距今 n 个日历日结算」的候选(其余门槛全过,只变结算日)。"""
        c = self._candidate(cid, [])
        c["end_date"] = (date.today() + timedelta(days=n)).strftime("%Y-%m-%d")
        return c

    def _ids(self, scanner, pool, tmpl):
        return {e["market_id"] for e in scanner.filter_for_template(pool, tmpl, "0xW")}

    def test_window_only_today(self):
        scanner = self._scanner()
        pool = [self._on_day("D0", 0), self._on_day("D1", 1), self._on_day("D2", 2)]
        tmpl = self._template(min_settlement_days=0, max_settlement_days=0)
        assert self._ids(scanner, pool, tmpl) == {"D0"}

    def test_window_today_and_tomorrow(self):
        scanner = self._scanner()
        pool = [self._on_day("D0", 0), self._on_day("D1", 1), self._on_day("D2", 2)]
        tmpl = self._template(min_settlement_days=0, max_settlement_days=1)
        assert self._ids(scanner, pool, tmpl) == {"D0", "D1"}

    def test_window_only_tomorrow(self):
        scanner = self._scanner()
        pool = [self._on_day("D0", 0), self._on_day("D1", 1), self._on_day("D2", 2)]
        tmpl = self._template(min_settlement_days=1, max_settlement_days=1)
        assert self._ids(scanner, pool, tmpl) == {"D1"}

    def test_window_two_days_or_more_no_cap(self):
        scanner = self._scanner()
        pool = [
            self._on_day("D0", 0),
            self._on_day("D1", 1),
            self._on_day("D2", 2),
            self._on_day("D10", 10),
        ]
        tmpl = self._template(min_settlement_days=2, max_settlement_days=None)
        assert self._ids(scanner, pool, tmpl) == {"D2", "D10"}

    def test_window_max_absent_means_no_cap(self):
        # 不设 max(键缺失)-> 沿用旧口径:只有下限,无上限
        scanner = self._scanner()
        pool = [self._on_day("D2", 2), self._on_day("D10", 10)]
        tmpl = self._template(min_settlement_days=0)  # 无 max_settlement_days 键
        assert self._ids(scanner, pool, tmpl) == {"D2", "D10"}

    def test_window_unparseable_end_date_kept(self):
        # 结算日缺失/解析不了 -> fail-open 保留(与旧口径一致)
        scanner = self._scanner()
        c = self._candidate("NODATE", [])
        c["end_date"] = ""
        tmpl = self._template(min_settlement_days=2, max_settlement_days=3)
        assert self._ids(scanner, pool=[c], tmpl=tmpl) == {"NODATE"}

    def test_tier_exact_match_only(self):
        scanner = self._scanner()
        pool = [
            self._candidate("A", [], daily_reward=20, min_size=20),
            self._candidate("B", [], daily_reward=50, min_size=50),
        ]
        tmpl = self._template(size_tiers=[_tier(20)])
        out = scanner.filter_for_template(pool, tmpl, "0xW")
        assert {e["rewards_min_size"] for e in out} == {20}

    def test_multiple_enabled_tiers_pass_their_sizes(self):
        scanner = self._scanner()
        pool = [
            self._candidate("A", [], daily_reward=20, min_size=20),
            self._candidate("B", [], daily_reward=50, min_size=50),
        ]
        tmpl = self._template(size_tiers=[_tier(20), _tier(50)])
        out = scanner.filter_for_template(pool, tmpl, "0xW")
        assert {e["rewards_min_size"] for e in out} == {20, 50}

    def test_disabled_tier_excluded(self):
        scanner = self._scanner()
        pool = [
            self._candidate("A", [], daily_reward=20, min_size=20),
            self._candidate("B", [], daily_reward=50, min_size=50),
        ]
        tmpl = self._template(size_tiers=[_tier(20), _tier(50, enabled=False)])
        out = scanner.filter_for_template(pool, tmpl, "0xW")
        assert {e["rewards_min_size"] for e in out} == {20}

    def test_no_tiers_yields_empty(self):
        scanner = self._scanner()
        pool = [self._candidate("A", [], daily_reward=20, min_size=20)]
        assert (
            scanner.filter_for_template(pool, self._template(size_tiers=[]), "0xW")
            == []
        )


class TestCategoryCounts:
    def test_counts_and_other(self):
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [{"condition_id": c} for c in ("A", "B", "C", "D")]
            return {
                "politics": [{"condition_id": "A"}],
                "economy": [{"condition_id": "A"}, {"condition_id": "B"}],
            }.get(tag_slug, [])

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        scanner = MarketScanner(api, MagicMock(), "")
        catalog = [
            {"slug": "politics", "label": "政治"},
            {"slug": "economy", "label": "经济"},
        ]
        out = scanner.category_counts(catalog)
        counts = {c["slug"]: c["count"] for c in out["categories"]}
        assert counts == {"politics": 1, "economy": 2}
        assert [c["label"] for c in out["categories"]] == ["政治", "经济"]
        assert out["other_count"] == 2  # C、D

    def test_parallel_slug_calls_carry_proxy(self):
        # 回归护栏:并发查各 slug 时,每个 worker 线程必须重设 current_proxy —— 否则
        # 静态 rewards 调用会直连、泄露真实 IP(contextvar 不会继承到线程池 worker)。
        from api.proxy import current_proxy

        seen = []

        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [{"condition_id": "A"}]
            seen.append(current_proxy.get())  # list.append 在 GIL 下原子
            return []

        api = MagicMock()
        api.proxy_url = "http://p:1"
        api.get_rewards_markets.side_effect = fake_rewards
        scanner = MarketScanner(api, MagicMock(), "")
        catalog = [{"slug": s, "label": s} for s in ("a", "b", "c")]
        scanner.category_counts(catalog)

        assert seen == ["http://p:1"] * 3

    def test_category_counts_threads_max_pages(self):
        def fake_rewards(tag_slug=None, max_pages=5, **kw):
            return [{"condition_id": "A"}]

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        scanner = MarketScanner(api, MagicMock(), "")
        scanner.category_counts([{"slug": "weather", "label": "天气"}], max_pages=13)
        assert api.get_rewards_markets.call_args_list
        for c in api.get_rewards_markets.call_args_list:
            assert c.kwargs.get("max_pages") == 13

    def test_refresh_orderbooks_cancel_raises(self):
        from engine.scanner import ScanSuperseded

        scanner = MarketScanner(MagicMock(), MagicMock(), "")
        with pytest.raises(ScanSuperseded):
            scanner.refresh_orderbooks([{"tokens": []}], cancel=lambda: True)

    def test_discover_cancel_raises(self):
        from engine.scanner import ScanSuperseded

        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [{"condition_id": "A", "tokens": [], "rewards_config": []}]
            return []

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        with pytest.raises(ScanSuperseded):
            scanner.discover_candidates(
                [
                    {
                        "included_categories": [],
                        "include_other": True,
                        "min_reward_usd": 0,
                    }
                ],
                cancel=lambda: True,
            )

    def test_catalog_payload_intersects_with_full(self):
        from config import CATEGORY_CATALOG

        s0, s1 = CATEGORY_CATALOG[0]["slug"], CATEGORY_CATALOG[1]["slug"]
        scanner = MarketScanner(MagicMock(), MagicMock(), "")
        full = [{"condition_id": c} for c in ("A", "B", "C", "D")]
        category_ids = {s0: {"A", "B", "Z"}, s1: {"B"}}  # Z 不在 full,应被交集剔除
        out = scanner._catalog_payload(full, category_ids)
        counts = {c["slug"]: c["count"] for c in out["categories"]}
        assert counts[s0] == 2 and counts[s1] == 1
        assert out["other_count"] == 2  # C、D 未被任何品类覆盖
        assert out["ready"] is True

    def test_discover_sets_last_catalog(self):
        # 扫描(发现)顺带把品类计数快照挂到 scanner.last_catalog —— manager 据此免联网刷新
        from config import CATEGORY_CATALOG

        s0 = CATEGORY_CATALOG[0]["slug"]

        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [
                    {
                        "condition_id": "A",
                        "tokens": [{"token_id": "A-y"}],
                        "rewards_config": [],
                    },
                    {
                        "condition_id": "B",
                        "tokens": [{"token_id": "B-y"}],
                        "rewards_config": [],
                    },
                ]
            return [{"condition_id": "A"}] if tag_slug == s0 else []

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        scanner.discover_candidates(
            [{"included_categories": [], "include_other": True, "min_reward_usd": 0}]
        )
        cat = scanner.last_catalog
        assert cat["ready"] is True
        counts = {c["slug"]: c["count"] for c in cat["categories"]}
        assert counts[s0] == 1  # A 命中 s0
        assert cat["other_count"] == 1  # B 落「其他」

    def test_discover_reports_progress_incrementally(self):
        # 精确奖励并发拉时应「边完成边报」进度 + on_found(前端进度条/候选列表逐渐增长),
        # 而不是并发跑完才一次性 emit(2026-07-05 用户反馈「卡 0 然后突然 100+」)。
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [
                    {
                        "condition_id": "A",
                        "tokens": [{"token_id": "A-y"}],
                        "rewards_config": [{"rate_per_day": 50}],
                    },
                    {
                        "condition_id": "B",
                        "tokens": [{"token_id": "B-y"}],
                        "rewards_config": [{"rate_per_day": 50}],
                    },
                ]
            return []

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        api.get_rewards_for_market.return_value = []  # 退回批量 total_rate
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        found, progress = [], []
        scanner.discover_candidates(
            [{"included_categories": [], "include_other": True, "min_reward_usd": 0}],
            on_progress=lambda c, t, m: progress.append((c, t)),
            on_found=lambda mk: found.append(mk.get("condition_id")),
        )
        assert set(found) == {"A", "B"}  # 每个候选都 on_found
        assert 2 in {t for _, t in progress}  # 进度总数=候选数(前端据此渲染进度条)
        checks = [c for c, t in progress if t == 2]
        assert checks == sorted(checks)  # 单调不减
        assert checks[-1] == 2 and 1 in checks  # 逐步到 2、中间有 1(非一次性跳到 2)


class TestDiscoverNeededSlugsOnly:
    def _api_db(self, fake_rewards):
        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        return api, db

    def test_only_needed_slugs_queried_without_include_other(self):
        # 不收「其他」时:只查各模板 included 并集,不再固定查全 14。
        queried = []

        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [
                    {"condition_id": "W", "tokens": [], "rewards_config": []},
                    {"condition_id": "S", "tokens": [], "rewards_config": []},
                ]
            queried.append(tag_slug)
            return {"weather": [{"condition_id": "W"}]}.get(tag_slug, [])

        api, db = self._api_db(fake_rewards)
        scanner = MarketScanner(api, db, "")
        templates = [
            {
                "included_categories": ["weather"],
                "include_other": False,
                "min_reward_usd": 0,
            }
        ]
        pool = scanner.discover_candidates(templates)
        assert set(queried) == {"weather"}  # 只查 weather,其余 13 个没查
        assert {m["condition_id"] for m in pool} == {"W"}  # S 无标签、无人收其他 -> 丢

    def test_multi_template_union_queried(self):
        # 多模板:查各自 included 的并集。
        queried = []

        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [
                    {"condition_id": "W", "tokens": [], "rewards_config": []},
                    {"condition_id": "G", "tokens": [], "rewards_config": []},
                ]
            queried.append(tag_slug)
            return {
                "weather": [{"condition_id": "W"}],
                "games": [{"condition_id": "G"}],
            }.get(tag_slug, [])

        api, db = self._api_db(fake_rewards)
        scanner = MarketScanner(api, db, "")
        templates = [
            {
                "included_categories": ["weather"],
                "include_other": False,
                "min_reward_usd": 0,
            },
            {
                "included_categories": ["games"],
                "include_other": False,
                "min_reward_usd": 0,
            },
        ]
        pool = scanner.discover_candidates(templates)
        assert set(queried) == {"weather", "games"}
        assert {m["condition_id"] for m in pool} == {"W", "G"}

    def test_include_other_queries_all_slugs(self):
        # 收「其他」时:判其他绕不开,必须查全 14(回归护栏)。
        from config import CATALOG_SLUGS

        queried = set()

        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [{"condition_id": "A", "tokens": [], "rewards_config": []}]
            queried.add(tag_slug)
            return []

        api, db = self._api_db(fake_rewards)
        scanner = MarketScanner(api, db, "")
        templates = [
            {
                "included_categories": ["weather"],
                "include_other": True,
                "min_reward_usd": 0,
            }
        ]
        scanner.discover_candidates(templates)
        assert queried == set(CATALOG_SLUGS)

    def test_subset_does_not_set_last_catalog(self):
        # 只查子集算不出全 14 计数 -> 不覆盖 last_catalog(保留旧缓存)。
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [{"condition_id": "W", "tokens": [], "rewards_config": []}]
            return {"weather": [{"condition_id": "W"}]}.get(tag_slug, [])

        api, db = self._api_db(fake_rewards)
        scanner = MarketScanner(api, db, "")
        templates = [
            {
                "included_categories": ["weather"],
                "include_other": False,
                "min_reward_usd": 0,
            }
        ]
        scanner.discover_candidates(templates)
        assert getattr(scanner, "last_catalog", None) is None

    def test_all_categories_checked_without_include_other_does_not_set_last_catalog(
        self,
    ):
        # 勾满全 14 个 curated 品类、但不勾「其他」:slugs_needed 覆盖全 14,可 inc_other=False
        # 时 full 从未抓取——算出的计数会是假的全零,不能只看 slugs_needed 是否覆盖全 14,必须连同
        # inc_other 一起判(2026-07-09 review 发现的回归:曾经零计数还挂 ready=True 骗过配置页)。
        from config import CATALOG_SLUGS

        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [{"condition_id": "W", "tokens": [], "rewards_config": []}]
            return []

        api, db = self._api_db(fake_rewards)
        scanner = MarketScanner(api, db, "")
        templates = [
            {
                "included_categories": list(CATALOG_SLUGS),
                "include_other": False,
                "min_reward_usd": 0,
            }
        ]
        scanner.discover_candidates(templates)
        assert getattr(scanner, "last_catalog", None) is None


class TestSpreadComputedLocally:
    """价差从已抓回的订单簿本地算(卖一−买一),不再每 token 多发一次 /spread 请求。

    /spread 实测 1.27s/次、比抓订单簿本身还慢 3 倍,而它的值就是订单簿一减;整池刷簿
    时它占了一半的网络调用,是首单等待时间的大头(2026-07-14)。
    """

    def _scanner(self, bids, asks):
        api = MagicMock()
        api.get_orderbooks.side_effect = lambda ids: {
            t: {"bids": bids, "asks": asks, "tick_size": "0.01"} for t in ids
        }
        return api, MarketScanner(api, MagicMock(), "")

    def _refresh(self, scanner):
        pool = [{"condition_id": "A", "tokens": [{"token_id": "A-y"}]}]
        scanner.refresh_orderbooks(pool)
        return pool[0]["_orderbooks"]["A-y"]

    def test_spread_from_book_without_extra_request(self):
        api, scanner = self._scanner(
            [{"price": "0.30", "size": "100"}], [{"price": "0.33", "size": "100"}]
        )
        assert self._refresh(scanner)["spread"] == 0.03
        api.get_spread.assert_not_called()

    def test_spread_has_no_float_dust(self):
        # 0.53-0.51 裸减 = 0.020000000000000018 -> *100 后与 max_spread_cents 的比较会翻面。
        _, scanner = self._scanner(
            [{"price": "0.51", "size": "1"}], [{"price": "0.53", "size": "1"}]
        )
        assert self._refresh(scanner)["spread"] == 0.02

    def test_book_side_missing_spread_unknown(self):
        # 缺一边 -> 价差无从谈起,沿用 -1(filter 据此跳过该 token)。
        _, scanner = self._scanner([], [{"price": "0.33", "size": "1"}])
        assert self._refresh(scanner)["spread"] == -1


class TestPrefilterForTemplate:
    """把「不需要订单簿就能判定」的门槛单独暴露出来:下单轮据此只给幸存市场刷簿,
    而不是整池 300+ 市场全刷(其中真正能下单的只有几十个)。"""

    def _candidate(self, cid, min_size=100, reward=50, tags=None, price=0.30):
        return {
            "condition_id": cid,
            "question": "M",
            "tags": tags or [],
            "end_date": "",
            "rewards_max_spread": 2,
            "rewards_min_size": min_size,
            "market_reward": reward,
            "rewards_config": [{"rate_per_day": reward}],
            "tokens": [{"token_id": cid + "-y", "outcome": "Yes", "price": price}],
        }

    def _template(self, **over):
        t = {
            "min_reward_usd": 6,
            "min_price_cents": 10,
            "max_price_cents": 90,
            "max_spread_cents": 6,
            "min_settlement_days": 0,
            "included_categories": ["politics"],
            "include_other": True,
            "size_tiers": [_tier(100)],
        }
        t.update(over)
        return t

    def _scanner(self, cooldown=False):
        db = MagicMock()
        db.is_in_cooldown.return_value = cooldown
        return MarketScanner(MagicMock(), db, "")

    def _ids(self, scanner, pool, tmpl):
        return {
            m["condition_id"] for m in scanner.prefilter_for_template(pool, tmpl, "0xW")
        }

    def test_keeps_market_that_has_no_orderbook_yet(self):
        # 关键契约:prefilter 必须在「还没刷簿」的候选上就能判定,否则省不掉任何请求。
        scanner = self._scanner()
        pool = [self._candidate("A")]
        assert self._ids(scanner, pool, self._template()) == {"A"}

    def test_drops_unmatched_tier(self):
        scanner = self._scanner()
        pool = [self._candidate("A"), self._candidate("B", min_size=300)]
        assert self._ids(scanner, pool, self._template()) == {"A"}

    def test_drops_low_reward(self):
        scanner = self._scanner()
        pool = [self._candidate("A"), self._candidate("B", reward=3)]
        assert self._ids(scanner, pool, self._template()) == {"A"}

    def test_drops_excluded_category(self):
        scanner = self._scanner()
        pool = [
            self._candidate("A", tags=["politics"]),
            self._candidate("B", tags=["esports"]),
        ]
        tmpl = self._template(included_categories=["politics"], include_other=False)
        assert self._ids(scanner, pool, tmpl) == {"A"}

    def test_drops_cooldown(self):
        scanner = self._scanner(cooldown=True)
        assert self._ids(scanner, [self._candidate("A")], self._template()) == set()

    def test_drops_market_with_no_token_in_price_band(self):
        scanner = self._scanner()
        pool = [self._candidate("A"), self._candidate("B", price=0.95)]
        assert self._ids(scanner, pool, self._template()) == {"A"}

    def _aged(self, cid, hours, **over):
        m = self._candidate(cid, **over)
        m["created_at"] = _created_hours_ago(hours)
        return m

    def test_drops_new_market(self):
        scanner = self._scanner()
        pool = [self._aged("NEW", 5), self._aged("OLD", 100)]
        tmpl = self._template(skip_new_markets=True, new_market_hours=24)
        assert self._ids(scanner, pool, tmpl) == {"OLD"}

    def test_keeps_new_market_when_switch_off(self):
        scanner = self._scanner()
        pool = [self._aged("NEW", 5)]
        tmpl = self._template(skip_new_markets=False, new_market_hours=24)
        assert self._ids(scanner, pool, tmpl) == {"NEW"}

    def test_keeps_new_market_when_hours_zero(self):
        scanner = self._scanner()
        pool = [self._aged("NEW", 0.1)]
        tmpl = self._template(skip_new_markets=True, new_market_hours=0)
        assert self._ids(scanner, pool, tmpl) == {"NEW"}

    def test_keeps_market_without_created_at(self):
        # fail-open:created_at 取不到就保留（与结算日解析不出即保留同口径）
        scanner = self._scanner()
        tmpl = self._template(skip_new_markets=True, new_market_hours=24)
        assert self._ids(scanner, [self._candidate("A")], tmpl) == {"A"}

    def test_each_template_uses_own_hours(self):
        scanner = self._scanner()
        pool = [self._aged("M48", 48)]
        strict = self._template(skip_new_markets=True, new_market_hours=72)
        loose = self._template(skip_new_markets=True, new_market_hours=24)
        assert self._ids(scanner, pool, strict) == set()
        assert self._ids(scanner, pool, loose) == {"M48"}


class TestDiscoveryTierWindowGating:
    """发现阶段:拉「精确奖励」(每市场一次网络、0.78s/次)前,先用不需要订单簿也不需要
    精确奖励的并集门槛(档位 sizes 并集 + 各模板结算窗口)剔掉注定不被任何模板要的市场。

    整池 300+ 候选逐个拉精确奖励是发现阶段的大头;真正会被下单的只有几十个(2026-07-14)。
    """

    def _api(self, markets):
        api = MagicMock()

        def fake_rewards(tag_slug=None, **kw):
            return list(markets) if tag_slug is None else []

        api.get_rewards_markets.side_effect = fake_rewards
        api.get_rewards_for_market.return_value = []  # 精确奖励:记录被对谁调用
        return api

    def _mkt(self, cid, min_size=100, end_days=10, rate=50):
        end = (date.today() + timedelta(days=end_days)).strftime("%Y-%m-%d")
        return {
            "condition_id": cid,
            "question": cid,
            "tokens": [{"token_id": cid + "-y"}],
            "rewards_config": [{"rate_per_day": rate}],
            "rewards_min_size": min_size,
            "end_date": end,
        }

    def _tmpl(self, **over):
        t = {
            "included_categories": [],
            "include_other": True,
            "min_reward_usd": 0,
            "size_tiers": [_tier(100)],
            "min_settlement_days": 0,
            "max_settlement_days": None,
        }
        t.update(over)
        return t

    def _db(self):
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        return db

    def _priced(self, api):
        return {c.args[0] for c in api.get_rewards_for_market.call_args_list}

    def test_skips_precise_reward_for_unmatched_tier(self):
        api = self._api([self._mkt("A", min_size=100), self._mkt("B", min_size=300)])
        sc = MarketScanner(api, self._db(), "")
        pool = sc.discover_candidates([self._tmpl()])
        assert self._priced(api) == {"A"}  # B 档位对不上 -> 不拉精确奖励
        # 但 B 仍进候选池(市场发现页照常显示),只是用批量奖励兜底、下单时由 filter 再剔。
        assert {m["condition_id"] for m in pool} == {"A", "B"}

    def test_skips_precise_reward_outside_all_windows(self):
        api = self._api([self._mkt("A", end_days=0), self._mkt("B", end_days=10)])
        sc = MarketScanner(api, self._db(), "")
        pool = sc.discover_candidates([self._tmpl(max_settlement_days=0)])
        assert self._priced(api) == {"A"}  # B 结算在窗口外 -> 不拉精确奖励
        assert {m["condition_id"] for m in pool} == {"A", "B"}  # B 仍进池

    def test_unparseable_end_date_is_fail_open(self):
        m = self._mkt("A")
        m["end_date"] = ""  # 解析不出结算日 -> 保留(与 filter 一致)
        api = self._api([m])
        sc = MarketScanner(api, self._db(), "")
        sc.discover_candidates([self._tmpl(max_settlement_days=0)])
        assert api.get_rewards_for_market.called

    def test_union_across_templates(self):
        api = self._api([self._mkt("A", 100), self._mkt("B", 300), self._mkt("C", 500)])
        sc = MarketScanner(api, self._db(), "")
        pool = sc.discover_candidates(
            [self._tmpl(size_tiers=[_tier(100)]), self._tmpl(size_tiers=[_tier(300)])]
        )
        assert self._priced(api) == {"A", "B"}  # C 两个模板档位都不要 -> 不拉精确奖励
        assert {m["condition_id"] for m in pool} == {"A", "B", "C"}  # 但都进池

    def test_no_tiers_configured_does_not_gate_by_tier(self):
        # 无任何启用档位 -> 并集为空 -> 不按档位筛(退化为旧行为,避免全剔)。
        api = self._api([self._mkt("A", 100), self._mkt("B", 300)])
        sc = MarketScanner(api, self._db(), "")
        sc.discover_candidates([self._tmpl(size_tiers=[])])
        assert self._priced(api) == {"A", "B"}


class TestReviewHardening:
    """/code-review high 发现的加固(2026-07-14):
    [0] book_spread 价格解析放进容错,坏 level 不再掀翻整轮刷簿。
    [2] 交叉盘(bid>ask)负价差 clamp 到 0(与旧 get_spread 一致让其可下单),不被当无簿跳过。
    [1] 价带判断对 price=null 不崩。
    [6] extra 循环尊重 cancel。
    """

    # --- [0]/[2] book_spread 健壮化 ---
    def test_book_spread_malformed_level_is_unknown(self):
        from engine.scanner import book_spread

        # 某档缺 price 键 -> 价差不可知(-1),绝不抛(否则整轮刷簿/扫描中止)。
        assert book_spread([{"size": "1"}], [{"price": "0.33", "size": "1"}]) == -1

    def test_book_spread_none_price_is_unknown(self):
        from engine.scanner import book_spread

        assert book_spread([{"price": None}], [{"price": "0.33"}]) == -1

    def test_book_spread_crossed_book_clamps_to_zero(self):
        from engine.scanner import book_spread

        # 交叉盘 best_bid 0.55 > best_ask 0.53 -> 价差为负,clamp 到 0(最窄,过 max_spread)。
        assert book_spread([{"price": "0.55"}], [{"price": "0.53"}]) == 0.0

    def test_book_spread_normal_unchanged(self):
        from engine.scanner import book_spread

        assert book_spread([{"price": "0.30"}], [{"price": "0.33"}]) == 0.03

    # --- [6] extra 循环尊重 cancel ---
    def test_extra_loop_honors_cancel(self):
        api = MagicMock()

        def fake_rewards(tag_slug=None, **kw):
            # 两个市场,档位都对不上 -> 都进 extra(不拉精确奖励)。
            return (
                [
                    {
                        "condition_id": "A",
                        "question": "A",
                        "tokens": [{"token_id": "A-y"}],
                        "rewards_config": [{"rate_per_day": 50}],
                        "rewards_min_size": 999,
                        "end_date": "",
                    },
                    {
                        "condition_id": "B",
                        "question": "B",
                        "tokens": [{"token_id": "B-y"}],
                        "rewards_config": [{"rate_per_day": 50}],
                        "rewards_min_size": 999,
                        "end_date": "",
                    },
                ]
                if tag_slug is None
                else []
            )

        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        sc = MarketScanner(api, db, "")
        tmpl = {
            "included_categories": [],
            "include_other": True,
            "min_reward_usd": 0,
            "size_tiers": [_tier(100)],
            "min_settlement_days": 0,
            "max_settlement_days": None,
        }
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 2  # 前 2 次(pool 循环)False,第 3 次(extra 循环)True

        with pytest.raises(ScanSuperseded):
            sc.discover_candidates([tmpl], cancel=cancel)


class TestPrefilterNullPrice:
    """[1] 价带判断对 price=null 不崩(prefilter 是新暴露面,filter 的 valid_tokens 同修)。"""

    def _template(self):
        return {
            "min_reward_usd": 6,
            "min_price_cents": 10,
            "max_price_cents": 90,
            "max_spread_cents": 6,
            "min_settlement_days": 0,
            "included_categories": [],
            "include_other": True,
            "size_tiers": [_tier(100)],
        }

    def _candidate(self, cid, price):
        return {
            "condition_id": cid,
            "question": "M",
            "tags": [],
            "end_date": "",
            "rewards_max_spread": 2,
            "rewards_min_size": 100,
            "market_reward": 50,
            "rewards_config": [{"rate_per_day": 50}],
            "tokens": [{"token_id": cid + "-y", "outcome": "Yes", "price": price}],
        }

    def test_null_price_treated_as_out_of_band(self):
        db = MagicMock()
        db.is_in_cooldown.return_value = False
        scanner = MarketScanner(MagicMock(), db, "")
        pool = [self._candidate("A", 0.30), self._candidate("B", None)]
        out = scanner.prefilter_for_template(pool, self._template(), "0xW")
        assert {m["condition_id"] for m in out} == {"A"}  # B 的 null 价按 0 落带外,不崩


class TestMarketAgeHours:
    """市场创建至今的小时数。created_at 是真正的 UTC 时刻，解析口径与 end_date（日历日）不同。"""

    def _utc(self, y, mo, d, h=0, mi=0, s=0):
        return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()

    def test_six_digit_fraction(self):
        now = self._utc(2026, 7, 23, 23, 10, 3)
        assert market_age_hours("2026-07-22T23:10:03.086269Z", now) == pytest.approx(
            24.0
        )

    def test_two_digit_fraction(self):
        # fromisoformat 只认 3/6 位微秒，实测存在 2 位的样本 -> 必须靠正则丢掉小数秒
        now = self._utc(2026, 7, 23, 23, 10, 3)
        assert market_age_hours("2026-07-22T23:10:03.08Z", now) == pytest.approx(24.0)

    def test_no_fraction(self):
        now = self._utc(2026, 7, 23, 23, 10, 3)
        assert market_age_hours("2026-07-22T23:10:03Z", now) == pytest.approx(24.0)

    def test_age_exactly_at_threshold(self):
        """恰好满 N 小时算「不新」：两处判定都是 age < 门槛 才排除，等于门槛要保留。"""
        now = self._utc(2026, 7, 23, 23, 10, 3)
        assert market_age_hours("2026-07-22T23:10:03Z", now) == 24.0

    def test_missing_or_malformed_returns_none(self):
        now = self._utc(2026, 7, 23)
        assert market_age_hours("", now) is None
        assert market_age_hours(None, now) is None
        assert market_age_hours("not-a-date", now) is None

    def test_non_string_returns_none(self):
        """奖励端点若把 created_at 发成数字/对象,同样 fail-open 返回 None,绝不抛——
        抛出去会被扫描循环的 except 吞掉,表现为「候选池冻住」或「该钱包永远挂不出单」。"""
        now = self._utc(2026, 7, 23)
        assert market_age_hours(1753000000, now) is None
        assert market_age_hours([], now) is None
        assert market_age_hours({"a": 1}, now) is None

    def test_out_of_range_values_return_none(self):
        """形状合法但取值越界 -> 同样 fail-open 返回 None，绝不抛。"""
        now = self._utc(2026, 7, 23)
        assert market_age_hours("0000-00-00T00:00:00Z", now) is None
        assert market_age_hours("2026-13-45T00:00:00Z", now) is None
        assert market_age_hours("2026-07-23T99:00:00Z", now) is None

    def test_parsed_as_utc_not_local(self):
        """按 UTC 解析。套 _parse_end_date（naive 本地还原）会在北京机器上差 8 小时。"""
        now = self._utc(2026, 7, 23, 0, 0, 0)
        assert market_age_hours("2026-07-23T00:00:00Z", now) == pytest.approx(0.0)

    def test_space_separator(self):
        now = self._utc(2026, 7, 23, 0, 0, 0)
        assert market_age_hours("2026-07-22 00:00:00Z", now) == pytest.approx(24.0)


class TestLoosestNewMarketHours:
    """发现阶段是钱包无关的共享阶段，只能用「所有模板都会因太新排除它」的最宽松门槛。

    「会排除」现在是两个条件的合取：模板开了开关，且该市场的品类在这个模板的保护名单里。
    """

    def _t(self, on, hrs, cats=None, other=True):
        return {
            "skip_new_markets": on,
            "new_market_hours": hrs,
            "skip_new_categories": ["politics"] if cats is None else cats,
            "skip_new_other": other,
        }

    def test_all_on_takes_min(self):
        tmpls = [self._t(True, 48), self._t(True, 24)]
        assert loosest_new_market_hours(tmpls, ["politics"]) == 24

    def test_any_off_returns_zero(self):
        tmpls = [self._t(True, 48), self._t(False, 24)]
        assert loosest_new_market_hours(tmpls, ["politics"]) == 0

    def test_empty_returns_zero(self):
        assert loosest_new_market_hours([], ["politics"]) == 0

    def test_hours_none_treated_as_zero(self):
        assert loosest_new_market_hours([self._t(True, None)], ["politics"]) == 0

    def test_malformed_hours_treated_as_zero(self):
        """非数字/负数的保护期归 0(= 不筛),不抛——DB 值可能被手改。"""
        assert loosest_new_market_hours([self._t(True, "abc")], ["politics"]) == 0
        assert loosest_new_market_hours([self._t(True, -5)], ["politics"]) == 0

    def test_missing_keys_treated_as_off(self):
        assert loosest_new_market_hours([{}], ["politics"]) == 0

    def test_unprotected_category_returns_zero(self):
        tmpls = [self._t(True, 24, cats=["crypto"])]
        assert loosest_new_market_hours(tmpls, ["politics"]) == 0

    def test_any_tag_hit_protects(self):
        # 多标签市场:命中保护名单里任一个就够
        tmpls = [self._t(True, 24, cats=["crypto"])]
        assert loosest_new_market_hours(tmpls, ["politics", "crypto"]) == 24

    def test_untagged_market_follows_skip_new_other(self):
        assert loosest_new_market_hours([self._t(True, 24, other=True)], []) == 24
        assert loosest_new_market_hours([self._t(True, 24, other=False)], []) == 0

    def test_one_template_not_protecting_returns_zero(self):
        # A 保护 politics、B 只保护 crypto -> 共享阶段不能排除 politics 的新市场
        tmpls = [
            self._t(True, 24, cats=["politics"]),
            self._t(True, 24, cats=["crypto"]),
        ]
        assert loosest_new_market_hours(tmpls, ["politics"]) == 0

    def test_missing_category_keys_treated_as_unprotected(self):
        # 缺 skip_new_categories/skip_new_other 时 fail-open 到「不保护」(= 不排除)
        tmpls = [{"skip_new_markets": True, "new_market_hours": 24}]
        assert loosest_new_market_hours(tmpls, ["politics"]) == 0


class TestDiscoverySkipNewMarkets:
    """发现阶段跳过新建市场：模板全开时按最宽松门槛排除；任一模板没开则一个都不排。

    发现阶段是钱包无关的共享阶段，排早了会把别的模板要的市场也剔掉（与奖励地板用
    min_floor 兜底同一模式）。created_at 由奖励端点白拿，判定不发任何网络请求。
    """

    def _api(self, markets):
        api = MagicMock()

        def fake_rewards(tag_slug=None, **kw):
            return list(markets) if tag_slug is None else []

        api.get_rewards_markets.side_effect = fake_rewards
        api.get_rewards_for_market.return_value = []
        return api

    def _mkt(self, cid, age_hours):
        return {
            "condition_id": cid,
            "question": cid,
            "tokens": [{"token_id": cid + "-y"}],
            "rewards_config": [{"rate_per_day": 50}],
            "rewards_min_size": 100,
            "end_date": (date.today() + timedelta(days=10)).strftime("%Y-%m-%d"),
            "created_at": _created_hours_ago(age_hours),
        }

    def _tmpl(self, **over):
        t = {
            "included_categories": [],
            "include_other": True,
            "min_reward_usd": 0,
            "size_tiers": [_tier(100)],
            "min_settlement_days": 0,
            "max_settlement_days": None,
            "skip_new_markets": True,
            "new_market_hours": 24,
            "skip_new_categories": [],
            "skip_new_other": True,
        }
        t.update(over)
        return t

    def _db(self):
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        return db

    def _pool_ids(self, api, templates):
        sc = MarketScanner(api, self._db(), "")
        return {m["condition_id"] for m in sc.discover_candidates(templates)}

    def test_new_market_excluded_from_pool(self):
        api = self._api([self._mkt("NEW", 5), self._mkt("OLD", 100)])
        assert self._pool_ids(api, [self._tmpl()]) == {"OLD"}

    def test_exactly_at_threshold_kept(self):
        # 门槛是「不足 N 小时才跳」，刚满 N 小时要留下
        api = self._api([self._mkt("AT", 24.01)])
        assert self._pool_ids(api, [self._tmpl()]) == {"AT"}

    def test_any_template_off_keeps_new_market(self):
        api = self._api([self._mkt("NEW", 5), self._mkt("OLD", 100)])
        tmpls = [self._tmpl(), self._tmpl(skip_new_markets=False)]
        assert self._pool_ids(api, tmpls) == {"NEW", "OLD"}

    def test_loosest_hours_used(self):
        # A 要 24h、B 要 72h -> 共享阶段只能按 24h 排，48h 龄的市场得留给 A
        api = self._api([self._mkt("M48", 48)])
        tmpls = [self._tmpl(new_market_hours=24), self._tmpl(new_market_hours=72)]
        assert self._pool_ids(api, tmpls) == {"M48"}

    def test_missing_created_at_kept(self):
        m = self._mkt("A", 1)
        del m["created_at"]
        assert self._pool_ids(self._api([m]), [self._tmpl()]) == {"A"}

    def test_switch_off_keeps_everything(self):
        api = self._api([self._mkt("NEW", 1)])
        assert self._pool_ids(api, [self._tmpl(skip_new_markets=False)]) == {"NEW"}

    def test_zero_hours_keeps_everything(self):
        api = self._api([self._mkt("NEW", 0.1)])
        assert self._pool_ids(api, [self._tmpl(new_market_hours=0)]) == {"NEW"}

    def _api_tagged(self, markets, slug):
        """让指定 slug 的品类查询也返回这些市场,使 tag_pool 给它们打上该标签。"""
        api = MagicMock()

        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None or tag_slug == slug:
                return list(markets)
            return []

        api.get_rewards_markets.side_effect = fake_rewards
        api.get_rewards_for_market.return_value = []
        return api

    def _cat_tmpl(self, **over):
        # 只做 politics、不收其他:市场带上 politics 标签才进得了候选池
        return self._tmpl(included_categories=["politics"], include_other=False, **over)

    def test_protected_category_new_market_excluded(self):
        api = self._api_tagged([self._mkt("NEW", 5), self._mkt("OLD", 100)], "politics")
        tmpl = self._cat_tmpl(skip_new_categories=["politics"], skip_new_other=False)
        assert self._pool_ids(api, [tmpl]) == {"OLD"}

    def test_unprotected_category_new_market_kept(self):
        api = self._api_tagged([self._mkt("NEW", 5)], "politics")
        tmpl = self._cat_tmpl(skip_new_categories=["crypto"], skip_new_other=False)
        assert self._pool_ids(api, [tmpl]) == {"NEW"}

    def test_one_template_not_protecting_keeps_new_market(self):
        # A 保护 politics、B 不保护 -> 共享阶段一个都不排,留给 prefilter 各自精筛
        api = self._api_tagged([self._mkt("NEW", 5)], "politics")
        tmpls = [
            self._cat_tmpl(skip_new_categories=["politics"], skip_new_other=False),
            self._cat_tmpl(skip_new_categories=["crypto"], skip_new_other=False),
        ]
        assert self._pool_ids(api, tmpls) == {"NEW"}
