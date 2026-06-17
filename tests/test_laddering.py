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


from engine.laddering import compute_market_ladders


def _side(bids, min_size=100, rmin=0.0, rmax=1.0):
    return {
        "bids": bids,
        "reward_range_min": rmin,
        "reward_range_max": rmax,
        "min_size": min_size,
    }


def test_market_ladders_min_size_both_sides():
    rules = [[{"upper": None, "action": {"type": "min_size"}}] for _ in range(6)]
    a = _side([_b(0.30, 300), _b(0.29, 300)])
    b = _side([_b(0.30, 300)])
    out = compute_market_ladders(a, b, rules, 1000.0, 10000)
    assert out["a"] == [(0.30, 100), (0.29, 100)]
    assert out["b"] == [(0.30, 100)]


def test_market_ladders_usd_budget_caps_across_sides():
    rules = [[{"upper": None, "action": {"type": "min_size"}}] for _ in range(6)]
    a = _side([_b(0.50, 300)])
    b = _side([_b(0.50, 300)])
    out = compute_market_ladders(a, b, rules, 80.0, 10000)
    assert out["a"] == [(0.50, 100)]
    assert out["b"] == [(0.50, 60)]


def test_market_ladders_shares_budget_caps():
    rules = [
        [{"upper": None, "action": {"type": "fixed_shares", "shares": 400}}]
        for _ in range(6)
    ]
    a = _side([_b(0.10, 1000)])
    b = _side([_b(0.10, 1000)])
    out = compute_market_ladders(a, b, rules, 100000.0, 500)
    assert out["a"] == [(0.10, 400)]
    assert out["b"] == [(0.10, 100)]


def test_market_ladders_wallet_total_decrements_across_tiers():
    rules = [[{"upper": None, "action": {"type": "wallet_total"}}] for _ in range(6)]
    a = _side([_b(0.50, 1000), _b(0.40, 1000)])
    b = None
    out = compute_market_ladders(a, b, rules, 100.0, 100000)
    assert out["a"] == [(0.50, 200)]
    assert out["b"] == []


def test_market_ladders_cross_side_order_a_before_b_same_tier():
    rules = [[{"upper": None, "action": {"type": "wallet_total"}}] for _ in range(6)]
    a = _side([_b(0.50, 1000)])
    b = _side([_b(0.50, 1000)])
    out = compute_market_ladders(a, b, rules, 100.0, 100000)
    assert out["a"] == [(0.50, 200)]
    assert out["b"] == []


from engine.laddering import apply_double_sided_floor


def test_double_sided_no_sub_threshold_unchanged():
    ladders = {"a": [(0.30, 100)], "b": []}
    assert apply_double_sided_floor(ladders, 10) == {"a": [(0.30, 100)], "b": []}


def test_double_sided_sub_threshold_both_sides_kept():
    ladders = {"a": [(0.08, 100)], "b": [(0.30, 100)]}
    assert apply_double_sided_floor(ladders, 10) == {
        "a": [(0.08, 100)],
        "b": [(0.30, 100)],
    }


def test_double_sided_sub_threshold_one_side_cleared():
    ladders = {"a": [(0.08, 100)], "b": []}
    assert apply_double_sided_floor(ladders, 10) == {"a": [], "b": []}


from engine.laddering import reconcile_buy_orders


def _ob_order(oid, price, size):
    return {"id": oid, "price": str(price), "original_size": str(size)}


def test_reconcile_empty_resting_places_all():
    cancel_ids, to_place = reconcile_buy_orders([(0.30, 100), (0.29, 100)], [])
    assert cancel_ids == []
    assert to_place == [(0.30, 100), (0.29, 100)]


def test_reconcile_keeps_matching_price_and_size():
    resting = [_ob_order("o1", 0.30, 100)]
    cancel_ids, to_place = reconcile_buy_orders([(0.30, 100), (0.29, 100)], resting)
    assert cancel_ids == []
    assert to_place == [(0.29, 100)]


def test_reconcile_cancels_price_drift():
    resting = [_ob_order("o1", 0.30, 100)]
    cancel_ids, to_place = reconcile_buy_orders([(0.29, 100)], resting)
    assert cancel_ids == ["o1"]
    assert to_place == [(0.29, 100)]


def test_reconcile_cancels_size_mismatch():
    resting = [_ob_order("o1", 0.30, 100)]
    cancel_ids, to_place = reconcile_buy_orders([(0.30, 200)], resting)
    assert cancel_ids == ["o1"]
    assert to_place == [(0.30, 200)]


def test_reconcile_size_tolerance_keeps():
    resting = [_ob_order("o1", 0.30, 100)]
    cancel_ids, to_place = reconcile_buy_orders([(0.30, 100.5)], resting)
    assert cancel_ids == []
    assert to_place == []


def test_reconcile_cancels_duplicate_at_same_price():
    # 同价两笔(上轮撤单失败遗留的重复单):只保留一笔、多余撤掉、不再重复挂,
    # 否则两笔都被 keep、该 token 持仓量翻倍(F6)。
    resting = [_ob_order("o1", 0.30, 100), _ob_order("o2", 0.30, 100)]
    cancel_ids, to_place = reconcile_buy_orders([(0.30, 100)], resting)
    assert cancel_ids == ["o2"]
    assert to_place == []


def test_reconcile_empty_target_cancels_all():
    resting = [_ob_order("o1", 0.30, 100), _ob_order("o2", 0.29, 100)]
    cancel_ids, to_place = reconcile_buy_orders([], resting)
    assert set(cancel_ids) == {"o1", "o2"}
    assert to_place == []
