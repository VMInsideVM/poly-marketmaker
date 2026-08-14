"""tests/test_legacy_wall.py — v1.0.15 厚墙定价纯函数(不触网)。"""

from engine.legacy_wall import (
    compute_market_legacy_orders,
    determine_order_price,
    explain_legacy_order,
    legacy_price_basis,
    legacy_reason,
)


def _make_bids(pairs):
    return [{"price": str(p), "size": str(s)} for p, s in pairs]


class TestMaxSpread2_TickSize1Cent:
    """max_spread=2, tick_size=0.01:买一厚才挂买二。"""

    def test_bid1_gt_2000_place_at_bid2(self):
        bids = _make_bids([(0.30, 3000), (0.29, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.01,
            reward_range_min=0.28,
            reward_range_max=0.32,
        )
        assert result == 0.29

    def test_bid1_le_2000_skip(self):
        bids = _make_bids([(0.30, 2000), (0.29, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.01,
            reward_range_min=0.28,
            reward_range_max=0.32,
        )
        assert result is None

    def test_result_outside_reward_range_returns_none(self):
        bids = _make_bids([(0.30, 3000), (0.20, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.01,
            reward_range_min=0.28,
            reward_range_max=0.32,
        )
        assert result is None


class TestMaxSpread2_TickSize01Cent:
    """max_spread=2, tick_size=0.001:走累计路径。"""

    def test_cumulative_gt_6000_next_position(self):
        bids = _make_bids(
            [
                (0.300, 2000),
                (0.299, 2000),
                (0.298, 2500),
                (0.297, 500),
            ]
        )
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.001,
            reward_range_min=0.290,
            reward_range_max=0.310,
        )
        assert result == 0.297

    def test_cumulative_never_exceeds_returns_none(self):
        bids = _make_bids([(0.300, 1000), (0.299, 1000)])
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.001,
            reward_range_min=0.290,
            reward_range_max=0.310,
        )
        assert result is None


class TestMaxSpreadGE3_TickSize1Cent:
    """max_spread>=3, tick_size=0.01:自上而下找第一堵墙。"""

    def test_bid1_gt_2000_place_bid2(self):
        bids = _make_bids([(0.30, 3000), (0.29, 500), (0.28, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=3,
            tick_size=0.01,
            reward_range_min=0.27,
            reward_range_max=0.32,
        )
        assert result == 0.29

    def test_bid1_le_2000_bid2_gt_2000_place_bid3(self):
        bids = _make_bids([(0.30, 1000), (0.29, 3000), (0.28, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=3,
            tick_size=0.01,
            reward_range_min=0.27,
            reward_range_max=0.32,
        )
        assert result == 0.28

    def test_bid1_bid2_lt_2000_bid3_gt_2000_place_bid4(self):
        bids = _make_bids(
            [
                (0.30, 500),
                (0.29, 500),
                (0.28, 3000),
                (0.27, 100),
            ]
        )
        result = determine_order_price(
            bids=bids,
            max_spread=3,
            tick_size=0.01,
            reward_range_min=0.26,
            reward_range_max=0.32,
        )
        assert result == 0.27

    def test_fallback_keeps_searching(self):
        # 所有档都 <=2000:跳过薄档一路往下扫,扫到超出 max_spread 范围仍没墙 -> 不挂。
        bids = _make_bids(
            [
                (0.30, 500),
                (0.29, 500),
                (0.28, 500),
                (0.27, 500),
            ]
        )
        result = determine_order_price(
            bids=bids,
            max_spread=3,
            tick_size=0.01,
            reward_range_min=0.26,
            reward_range_max=0.32,
        )
        assert result is None


class TestMaxSpreadGE3_TickSize01Cent:
    """max_spread>=3, tick_size=0.001:细 tick 一律走累计路径。"""

    def test_cumulative_gt_6000(self):
        bids = _make_bids(
            [
                (0.300, 3000),
                (0.299, 3500),
                (0.298, 500),
            ]
        )
        result = determine_order_price(
            bids=bids,
            max_spread=3,
            tick_size=0.001,
            reward_range_min=0.290,
            reward_range_max=0.310,
        )
        assert result == 0.298


def test_empty_bids_returns_none():
    assert (
        determine_order_price(
            bids=[],
            max_spread=2,
            tick_size=0.01,
            reward_range_min=0.10,
            reward_range_max=0.90,
        )
        is None
    )


def test_wall_threshold_is_configurable():
    # 买一 1500:默认阈值 2000 拦下;把阈值降到 1000 就挂买二。
    bids = _make_bids([(0.30, 1500), (0.29, 500)])
    common = dict(
        bids=bids,
        max_spread=2,
        tick_size=0.01,
        reward_range_min=0.28,
        reward_range_max=0.32,
    )
    assert determine_order_price(**common) is None
    assert determine_order_price(**common, wall_threshold=1000) == 0.29


def test_cumulative_threshold_is_configurable():
    # 累计 2000:默认阈值 6000 拦下;降到 1500 就挂下一档。
    bids = _make_bids([(0.300, 2000), (0.299, 500)])
    common = dict(
        bids=bids,
        max_spread=2,
        tick_size=0.001,
        reward_range_min=0.290,
        reward_range_max=0.310,
    )
    assert determine_order_price(**common) is None
    assert determine_order_price(**common, cumulative_threshold=1500) == 0.299


def test_ge3_first_wall_wins_no_second_search():
    # 0.30 是墙(3000),它的下一档 0.29 出了奖励区间[0.28,0.32]吗?没有。
    # 换个构造:墙在 0.30,下一档 0.26 低于 min_price(0.30-3*0.01=0.27) -> 不挂,
    # 且不会继续往下找 0.26 这堵更厚的墙。
    bids = _make_bids([(0.30, 3000), (0.26, 9999), (0.25, 9999)])
    result = determine_order_price(
        bids=bids,
        max_spread=3,
        tick_size=0.01,
        reward_range_min=0.20,
        reward_range_max=0.32,
    )
    assert result is None


def test_ge3_wall_at_last_level_has_no_next():
    # 墙是最后一档,没有下一档可挂 -> 不挂。
    bids = _make_bids([(0.30, 500), (0.29, 3000)])
    result = determine_order_price(
        bids=bids,
        max_spread=3,
        tick_size=0.01,
        reward_range_min=0.26,
        reward_range_max=0.32,
    )
    assert result is None


def test_ge3_does_not_search_second_wall_when_first_next_out_of_reward_range():
    # 钉住「找到第一堵墙就定死」:第一堵墙 0.30(3000) 的下一档 0.29 在价格带内
    # (>= min_price 0.27)但**出了奖励区间上沿 0.285** -> 不挂。
    # 若实现漏掉那句 return None 而继续往下找,会找到 0.28(5000) 这堵更厚的墙,
    # 它的下一档 0.27 价格带与奖励区间都合格 -> 会错误地返回 0.27。
    bids = _make_bids([(0.30, 3000), (0.29, 100), (0.28, 5000), (0.27, 100)])
    result = determine_order_price(
        bids=bids,
        max_spread=3,
        tick_size=0.01,
        reward_range_min=0.10,
        reward_range_max=0.285,
    )
    assert result is None


def _explain(
    bids,
    max_spread=2,
    tick=0.01,
    rmin=0.28,
    rmax=0.32,
    min_size=20,
    mode="min",
    balance=1000.0,
    custom=0.0,
    wall=2000,
    cum=6000,
):
    return explain_legacy_order(
        bids,
        max_spread,
        tick,
        rmin,
        rmax,
        min_size,
        mode,
        balance,
        custom,
        wall_threshold=wall,
        cumulative_threshold=cum,
    )


def test_explain_wall_hit_carries_evidence():
    # 买一 3000 > 2000 -> 命中墙,挂买二 0.29,份数 min 模式 = min_size。
    d = _explain(_make_bids([(0.30, 3000), (0.29, 500)]))
    assert d["action"] == "place"
    assert d["rule"] == "wall"
    assert d["hit_index"] == 0
    assert d["hit_size"] == 3000
    assert d["threshold"] == 2000
    assert d["price"] == 0.29
    assert d["shares"] == 20


def test_explain_skip_reason_names_threshold():
    d = _explain(_make_bids([(0.30, 1500), (0.29, 500)]))
    assert d["action"] == "skip"
    assert "1500" in d["skip_reason"]
    assert "2000" in d["skip_reason"]


def test_explain_cumulative_carries_running_total():
    d = _explain(
        _make_bids([(0.300, 2000), (0.299, 2000), (0.298, 2500), (0.297, 500)]),
        max_spread=2,
        tick=0.001,
        rmin=0.290,
        rmax=0.310,
        cum=6000,
    )
    assert d["rule"] == "cumulative"
    assert d["hit_index"] == 2
    assert d["cumulative"] == 6500
    assert d["price"] == 0.297


def test_explain_levels_carry_running_cum():
    d = _explain(_make_bids([(0.30, 3000), (0.29, 500)]))
    assert [lv["cum"] for lv in d["levels"]] == [3000, 3500]


def test_explain_size_mode_balance_overrides_min_size():
    # balance 模式:floor(1000/0.29)=3448 份。
    d = _explain(_make_bids([(0.30, 3000), (0.29, 500)]), mode="balance")
    assert d["shares"] == 3448


def test_explain_size_mode_skip_when_below_min_size():
    # custom 上限 4 美元 -> floor(4/0.29)=13 < min_size 20 -> 跳过。
    d = _explain(_make_bids([(0.30, 3000), (0.29, 500)]), mode="custom", custom=4.0)
    assert d["action"] == "skip"
    assert "份数" in d["skip_reason"]


def test_reason_placed_mentions_rule_and_threshold():
    d = _explain(_make_bids([(0.30, 3000), (0.29, 500)]))
    r = legacy_reason(d)
    assert "厚墙" in r
    assert "3000" in r
    assert "2000" in r


def test_reason_skip_uses_skip_reason():
    d = _explain(_make_bids([(0.30, 1500), (0.29, 500)]))
    assert legacy_reason(d) == d["skip_reason"]


def test_price_basis_has_levels_and_source():
    d = _explain(_make_bids([(0.30, 3000), (0.29, 500)]))
    b = legacy_price_basis(d, 0.28, 0.32)
    assert "0.3000" in b
    assert "get_orderbook" in b
    assert "奖励区间" in b


def _side(bids, rmin=0.28, rmax=0.32, min_size=20, tick="0.01", max_spread=2):
    return {
        "bids": bids,
        "reward_range_min": rmin,
        "reward_range_max": rmax,
        "min_size": min_size,
        "tick_size": float(tick),
        "max_spread": max_spread,
        "token_id": "t",
        "outcome": "Yes",
    }


def test_compute_one_side_places_one():
    side = _side(_make_bids([(0.30, 3000), (0.29, 500)]))
    out = compute_market_legacy_orders(side, None, 1000.0, 500, 1000.0, "min", 0.0)
    assert out["a"] == [(0.29, 20)]
    assert out["b"] == []


def test_compute_budget_caps_shares():
    # balance 模式想挂 3448 份,但市场预算只有 10 美元 -> floor(10/0.29)=34 份。
    side = _side(_make_bids([(0.30, 3000), (0.29, 500)]))
    out = compute_market_legacy_orders(side, None, 10.0, 500, 1000.0, "balance", 0.0)
    assert out["a"] == [(0.29, 34)]


def test_compute_drops_side_when_cap_below_min_size():
    # 预算只够 3 份 < min_size 20 -> 放弃该边。
    side = _side(_make_bids([(0.30, 3000), (0.29, 500)]))
    out = compute_market_legacy_orders(side, None, 1.0, 500, 1000.0, "min", 0.0)
    assert out["a"] == []


def test_compute_both_sides_share_budget():
    a = _side(_make_bids([(0.30, 3000), (0.29, 500)]))
    b = _side(_make_bids([(0.30, 3000), (0.29, 500)]))
    # 预算只够 a 边挂满 20 份(0.29*20=5.8),b 边剩 0.2 美元 -> 放弃。
    out = compute_market_legacy_orders(a, b, 6.0, 500, 1000.0, "min", 0.0)
    assert out["a"] == [(0.29, 20)]
    assert out["b"] == []
