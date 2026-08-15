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
    cliff=0,
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
        cliff_probe_cents=cliff,
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


def test_explain_spread2_does_not_claim_hit_beyond_bid1():
    # max_spread=2 只看买一:买一 1000 不够厚 -> 不挂。第2档虽有 3000,
    # 但这条规则根本不看它,不能把它报成「命中」。
    d = _explain(_make_bids([(0.30, 1000), (0.29, 3000)]), rmin=0.20, rmax=0.35)
    assert d["action"] == "skip"
    assert d["hit_index"] is None
    assert "3000" not in d["skip_reason"]


def test_explain_ge3_does_not_claim_hit_below_min_price():
    # max_spread=5、tick=0.01 -> min_price=0.25;0.20 那档定价函数扫都没扫到,
    # 不能报成命中。
    d = _explain(
        _make_bids([(0.30, 100), (0.29, 100), (0.20, 5000)]),
        max_spread=5,
        rmin=0.10,
        rmax=0.35,
    )
    assert d["action"] == "skip"
    assert d["hit_index"] is None
    assert "5000" not in d["skip_reason"]


def test_explain_hit_uses_int_truncated_size_not_raw_float():
    # 挂量含小数(Polymarket 实盘挂量确实有小数,如 236.24):命中判定必须按 int
    # 截断后比较,与真实定价口径(_spread_ge3_coarse 的 int(float(size)) >
    # wall_threshold)对齐,否则会把定价函数根本没判为墙的档报成命中,连带
    # hit_index/reason 都指向一个从未挂过的价位。
    # 买档 0.30/2000.5(int=2000,不是墙)、0.29/100、0.28/3000(真正的墙)、0.27/100;
    # 真实定价挂在 0.28 的下一档 0.27。
    bids = _make_bids([(0.30, 2000.5), (0.29, 100), (0.28, 3000), (0.27, 100)])
    d = _explain(bids, max_spread=3, rmin=0.20, rmax=0.35)
    assert d["action"] == "place"
    assert d["price"] == 0.27
    assert d["hit_index"] == 2
    # 命中档的下一档必须就是真正挂出去的价,定价与解释不能分叉。
    assert d["price"] == d["levels"][d["hit_index"] + 1]["price"]


def test_explain_cumulative_hit_skip_reason_uses_cumulative_not_size():
    # 累计路径命中但下一档不可挂(这里是没有下一档):skip_reason 必须报累计值
    # 6500,而不是单档挂量 3500 —「3500 > 阈值 6000」是假的不等式,真值是
    # 「第2档累计 6500 > 阈值 6000」。
    bids = _make_bids([(0.300, 3000), (0.299, 3500)])
    d = _explain(bids, max_spread=2, tick=0.001, rmin=0.290, rmax=0.310, cum=6000)
    assert d["action"] == "skip"
    assert d["hit_index"] == 1
    assert d["cumulative"] == 6500
    assert "6500" in d["skip_reason"]
    assert "3500 >" not in d["skip_reason"]


def test_explain_cumulative_no_hit_skip_reason_uses_total_not_max_size():
    # 累计路径无命中:skip_reason 该报累计总量(1900),不是单档最厚(1000)。
    bids = _make_bids([(0.300, 1000), (0.299, 900)])
    d = _explain(bids, max_spread=2, tick=0.001, rmin=0.290, rmax=0.310, cum=6000)
    assert d["action"] == "skip"
    assert d["hit_index"] is None
    assert "1900" in d["skip_reason"]
    assert "1000" not in d["skip_reason"]


def test_explain_cliff_below_reward_range_vetoes_before_pricing():
    # 买档跳空到远低于探测带的位置(0.05):奖励区间下沿(0.28)往下 2¢ 内
    # ([0.26,0.28))没有买档支撑 -> 悬崖否决,不再进入定价(即便真实定价本来
    # 会挂在 0.29)。
    bids = _make_bids([(0.30, 3000), (0.29, 500), (0.05, 9999)])
    baseline = _explain(bids)  # cliff 默认 0,不否决
    assert baseline["action"] == "place"

    d = _explain(bids, cliff=2)
    assert d["action"] == "skip"
    assert d["cliff"] is True
    assert "悬崖" in d["skip_reason"]


def test_explain_cliff_probe_zero_never_vetoes():
    # cliff_probe_cents=0(v1.0.15 原始行为的默认值)-> 不否决,即便下方是真空。
    bids = _make_bids([(0.30, 3000), (0.29, 500), (0.05, 9999)])
    d = _explain(bids, cliff=0)
    assert d["action"] == "place"
    assert d["cliff"] is False


def test_compute_market_legacy_orders_respects_cliff_probe():
    # compute_market_legacy_orders 的 cliff_probe_cents 必须透传给 explain_legacy_order,
    # 悬崖市场即便预算/份数都够也不挂。
    side = _side(_make_bids([(0.30, 3000), (0.29, 500), (0.05, 9999)]))
    out_no_cliff = compute_market_legacy_orders(
        side, None, 1000.0, 500, 1000.0, "min", 0.0
    )
    assert out_no_cliff["a"] == [(0.29, 20)]
    out_cliff = compute_market_legacy_orders(
        side, None, 1000.0, 500, 1000.0, "min", 0.0, cliff_probe_cents=2
    )
    assert out_cliff["a"] == []


# --- 2026-08-15 调查修复 ------------------------------------------------------


def test_price_basis_on_place_does_not_dump_whole_book():
    # 挂单成功时只写命中档与选中档;全簿展开留给跳过分支(对齐 gap_single 的做法)。
    # 否则每次挂单都往 actions 表写几百上千字符,高频写入撑爆历史。
    bids = _make_bids(
        [(0.30, 3000)] + [(round(0.30 - i * 0.005, 3), 100) for i in range(1, 40)]
    )
    d = _explain(bids, rmin=0.28, rmax=0.32)
    assert d["action"] == "place"
    b = legacy_price_basis(d, 0.28, 0.32)
    assert "买单簿(价降序)" not in b
    assert len(b) < 250


def test_price_basis_on_skip_still_expands_book():
    # 跳过时仍要逐档展开,那是复盘不挂原因的唯一依据。
    d = _explain(_make_bids([(0.30, 100), (0.29, 100)]), rmin=0.28, rmax=0.32)
    assert d["action"] == "skip"
    assert "买单簿(价降序)" in legacy_price_basis(d, 0.28, 0.32)


def test_text_sizes_use_int_matching_judgement():
    # 判定拿 int(size) 比阈值、累计也按 int 累加,文案里的挂量必须同口径显示,
    # 否则 236.24 与「累计236」并排出现,用户相加对不上。
    d = _explain(_make_bids([(0.21, 236.24), (0.19, 120.0)]), rmin=0.10, rmax=0.30)
    assert d["action"] == "skip"
    assert "236.24" not in d["skip_reason"]
    assert "236" in d["skip_reason"]
    b = legacy_price_basis(d, 0.10, 0.30)
    assert "236.24" not in b
    assert "0.2100×236(累计236)" in b
