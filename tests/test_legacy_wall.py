"""tests/test_legacy_wall.py — v1.0.15 厚墙定价纯函数(不触网)。"""

from engine.legacy_wall import determine_order_price


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
