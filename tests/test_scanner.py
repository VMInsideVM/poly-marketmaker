"""tests/test_scanner.py — Tests updated to match Polymarket API response format."""

import time
import pytest
from datetime import datetime, timedelta, date
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

    def test_rewards_min_size_exact_match_only(self):
        scanner = self._scanner()
        pool = [
            self._candidate("A", [], daily_reward=20, min_size=20),
            self._candidate("B", [], daily_reward=50, min_size=50),
        ]
        tmpl = self._template(rewards_min_size_min=20, rewards_min_size_max=20)
        out = scanner.filter_for_template(pool, tmpl, "0xW")
        assert {e["rewards_min_size"] for e in out} == {20}

    def test_per_share_disabled_lets_low_through(self):
        scanner = self._scanner()
        # 单份奖励 20/100 = 0.20 < 0.30,但关掉开关 -> 应放行
        pool = [self._candidate("A", [], daily_reward=20)]
        tmpl = self._template(per_share_reward_enabled=False)
        out = scanner.filter_for_template(pool, tmpl, "0xW")
        assert any(e["market_id"] == "A" for e in out)

    def test_per_share_enabled_by_default_still_excludes(self):
        scanner = self._scanner()
        pool = [self._candidate("A", [], daily_reward=20)]
        # _template 不设该键 -> 默认启用 -> 仍剔除
        assert scanner.filter_for_template(pool, self._template(), "0xW") == []

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


class TestParallelMap:
    def test_preserves_order(self):
        from engine.scanner import _parallel_map

        assert _parallel_map(lambda x: x * 2, [1, 2, 3, 4]) == [2, 4, 6, 8]

    def test_empty_items(self):
        from engine.scanner import _parallel_map

        assert _parallel_map(lambda x: x, []) == []

    def test_propagates_current_proxy_into_workers(self):
        # 关键:新线程默认 current_proxy=None(会直连、泄露真实 IP);助手必须把调用
        # 线程的代理带进每个 worker,否则并行化就破了 IP 隔离铁律。
        from engine.scanner import _parallel_map
        from api.proxy import current_proxy, use_proxy

        with use_proxy("http://user:pass@host:9999"):
            seen = _parallel_map(lambda x: current_proxy.get(), list(range(8)))
        assert seen == ["http://user:pass@host:9999"] * 8

    def test_no_proxy_stays_none(self):
        from engine.scanner import _parallel_map
        from api.proxy import current_proxy

        assert _parallel_map(lambda x: current_proxy.get(), [1, 2]) == [None, None]
