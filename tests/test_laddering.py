"""tests/test_laddering.py — 多档挂单纯函数(不触网)。"""

from engine.laddering import build_ladder


def _b(price, size):
    return {"price": str(price), "size": str(size)}


def test_ladder_picks_valid_levels_highest_first():
    bids = [_b(0.35, 300), _b(0.30, 150), _b(0.25, 50), _b(0.22, 200)]
    # 0.25 厚度 50/100=0.5 <1 -> 跳过;其余厚度>=1 且在区间
    ladder = build_ladder(bids, 0.20, 0.40, 100, 6)
    assert [r["price"] for r in ladder] == [0.35, 0.30, 0.22]


def test_ladder_cumulative_thickness_sums_all_levels_above():
    bids = [_b(0.35, 300), _b(0.30, 150), _b(0.25, 50), _b(0.22, 200)]
    ladder = build_ladder(bids, 0.20, 0.40, 100, 6)
    by_price = {r["price"]: r["cumulative_thickness"] for r in ladder}
    assert by_price[0.35] == 3.0
    assert by_price[0.30] == 3.0 + 1.5
    assert by_price[0.22] == 4.5 + 0.5 + 2.0


def test_ladder_skips_out_of_reward_range():
    bids = [_b(0.45, 300), _b(0.30, 300)]
    ladder = build_ladder(bids, 0.20, 0.40, 100, 6)
    assert [r["price"] for r in ladder] == [0.30]


def test_ladder_caps_at_k():
    bids = [_b(0.39 - i * 0.01, 300) for i in range(10)]
    ladder = build_ladder(bids, 0.0, 1.0, 100, 6)
    assert len(ladder) == 6


def test_ladder_empty_when_no_bids_or_min_size_zero():
    assert build_ladder([], 0.0, 1.0, 100, 6) == []
    assert build_ladder([_b(0.3, 300)], 0.0, 1.0, 0, 6) == []


from engine.laddering import resolve_tier_share


def _rule(*intervals):
    return list(intervals)


def test_share_min_size():
    rule = _rule({"upper": None, "action": {"type": "min_size"}})
    assert resolve_tier_share(2.0, rule, 0.30, 100, 1000) == 100


def test_share_fixed_shares():
    rule = _rule({"upper": None, "action": {"type": "fixed_shares", "shares": 50}})
    assert resolve_tier_share(2.0, rule, 0.30, 100, 1000) == 50


def test_share_fixed_amount_floor_and_min_bump():
    rule = _rule({"upper": None, "action": {"type": "fixed_amount", "usd": 30}})
    assert resolve_tier_share(2.0, rule, 0.30, 100, 1000) == 100
    rule2 = _rule({"upper": None, "action": {"type": "fixed_amount", "usd": 9}})
    assert resolve_tier_share(2.0, rule2, 0.30, 100, 1000) == 100


def test_share_wallet_total_uses_remaining_budget():
    rule = _rule({"upper": None, "action": {"type": "wallet_total"}})
    assert resolve_tier_share(2.0, rule, 0.30, 100, 60) == 200


def test_share_skip():
    rule = _rule({"upper": None, "action": {"type": "skip"}})
    assert resolve_tier_share(2.0, rule, 0.30, 100, 1000) == 0


def test_interval_selection_half_open_ascending():
    rule = _rule(
        {"upper": 5.0, "action": {"type": "fixed_shares", "shares": 50}},
        {"upper": None, "action": {"type": "min_size"}},
    )
    assert resolve_tier_share(4.9, rule, 0.30, 100, 1000) == 50
    assert resolve_tier_share(5.0, rule, 0.30, 100, 1000) == 100
    assert resolve_tier_share(9.0, rule, 0.30, 100, 1000) == 100
