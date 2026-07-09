"""tests/test_gap_single.py — 网关式单档挂单纯函数(不触网)。"""

from engine.laddering import plan_gap_single_order

AV = [
    {"upper": 0.20, "value": 1},
    {"upper": 0.25, "value": 1.5},
    {"upper": 0.31, "value": 2},
]


def _b(price, size):
    return {"price": str(price), "size": str(size)}


def _plan(
    bids,
    rmin=0.10,
    rmax=0.31,
    min_size=20,
    av=None,
    wide=10,
    mid=5,
    gate=20,
    x1=0,
    x2=0,
    x3=0,
    cliff=0,
):
    return plan_gap_single_order(
        bids,
        rmin,
        rmax,
        min_size,
        AV if av is None else av,
        wide,
        mid,
        gate,
        x1,
        x2,
        x3,
        cliff_probe_cents=cliff,
    )


def test_normal_market_picks_highest_in_range():
    # 相邻价差 1¢ < 5 -> 规则3;买一 0.28 系数 50/(20*2)=1.25 > 0 -> 挂 0.28 一单 20 股。
    out = _plan([_b(0.28, 50), _b(0.27, 40)])
    assert out == (0.28, 20)


def test_out_of_range_levels_ignored():
    # 0.05 低于 rmin 0.10,剔除;仍挂 0.28。
    out = _plan([_b(0.28, 50), _b(0.05, 9999)])
    assert out == (0.28, 20)


def test_wide_gap_gate_fail_skips_market():
    # 0.27->0.15 价差 12¢ > 10 -> 规则1;高位{0.28,0.27}系数和 1.25+0.75=2 < 20 -> None。
    out = _plan([_b(0.28, 50), _b(0.27, 30), _b(0.15, 400)])
    assert out is None


def test_wide_gap_gate_pass_places():
    # 同上但 0.27 厚 -> 高位系数和 1.25+20=21.25 >= 20 -> 顺延挂买一 0.28。
    out = _plan([_b(0.28, 50), _b(0.27, 800), _b(0.15, 400)])
    assert out == (0.28, 20)


def test_mid_gap_no_gate_places():
    # 价差 7¢ 在 [5,10] -> 规则2,无闸门,挂买一 0.28。
    out = _plan([_b(0.28, 50), _b(0.21, 400)])
    assert out == (0.28, 20)


def test_fallthrough_to_lower_when_top_below_x():
    # 密盘(价差1¢)走规则3;x3=2:买一 0.28 系数 1.25 不>2 -> 顺延买二 0.27 系数 200/40=5 >2 -> 挂 0.27。
    out = _plan([_b(0.28, 50), _b(0.27, 200)], x3=2)
    assert out == (0.27, 20)


def test_none_qualify_returns_none():
    out = _plan([_b(0.28, 50), _b(0.27, 40)], x3=10)
    assert out is None


def test_rule1_selection_uses_only_rule1_coeff():
    # 宽断层 12¢ 过闸(高位系数和 1.25+20=21.25>=20)。x1=2 跳过买一 0.28(系数1.25),
    # 顺延买二 0.27(系数 800/(20*2)=20)。x2/x3=100 证明规则1只看 rule1_min_coeff。
    bids = [_b(0.28, 50), _b(0.27, 800), _b(0.15, 400)]
    assert _plan(bids, x1=2, x2=100, x3=100) == (0.27, 20)


def test_rule2_selection_uses_only_rule2_coeff():
    # 中断层 7¢ -> 规则2。x2=2 跳过买一 0.28(1.25),顺延 0.21(系数 400/(20*1.5)=13.33)。
    # x1/x3=100 证明规则2只看 rule2_min_coeff。
    bids = [_b(0.28, 50), _b(0.21, 400)]
    assert _plan(bids, x1=100, x2=2, x3=100) == (0.21, 20)


def test_rule3_selection_uses_only_rule3_coeff():
    # 密盘 1¢ -> 规则3。x3=2 跳过买一 0.28(1.25),顺延 0.27(系数 200/(20*2)=5)。
    # x1/x2=100 证明规则3只看 rule3_min_coeff。
    bids = [_b(0.28, 50), _b(0.27, 200)]
    assert _plan(bids, x1=100, x2=100, x3=2) == (0.27, 20)


def test_empty_or_zero_min_size_returns_none():
    assert _plan([]) is None
    assert _plan([_b(0.28, 50)], min_size=0) is None


def test_no_in_range_returns_none():
    # 全部买档在奖励区间外。
    assert _plan([_b(0.05, 500), _b(0.04, 500)]) is None


def test_gap_exactly_10_is_rule2_not_gated():
    # 价差恰 10¢ 不 >10 -> 规则2,不触发高位闸门;高位系数虽小仍挂买一。
    out = _plan([_b(0.28, 10), _b(0.18, 10)])
    assert out == (0.28, 20)


def test_amount_value_missing_gives_zero_coeff():
    # 价 0.35 超金额表(顶 0.31)-> 系数 0,x=0 时 0 不>0 -> None。
    out = _plan([_b(0.35, 100)], rmax=0.40)
    assert out is None


def test_tie_max_gap_splits_at_topmost():
    # 两处价差都 10¢、wide=8 都算宽断层;取最靠上那处劈分 -> 高位仅{0.30}系数 2.5 <20 -> None。
    # (若误在第二处劈分,高位含 0.20 厚档系数和会 >=20 而误挂。)
    out = _plan([_b(0.30, 100), _b(0.20, 800), _b(0.10, 100)], wide=8)
    assert out is None


from engine.laddering import explain_gap_single_order


def _explain(
    bids,
    rmin=0.10,
    rmax=0.31,
    min_size=20,
    av=None,
    wide=10,
    mid=5,
    gate=20,
    x1=0,
    x2=0,
    x3=0,
):
    return explain_gap_single_order(
        bids,
        rmin,
        rmax,
        min_size,
        AV if av is None else av,
        wide,
        mid,
        gate,
        x1,
        x2,
        x3,
    )


def test_explain_normal_market_full_decision():
    d = _explain([_b(0.28, 50), _b(0.27, 40)])
    assert d["action"] == "place"
    assert d["rule"] == 3
    assert d["max_gap"] == 1.0
    assert d["min_coeff"] == 0
    assert d["high_sum"] is None
    assert d["gate_passed"] is True
    assert d["chosen_index"] == 0
    assert d["price"] == 0.28
    assert d["shares"] == 20
    assert d["skip_reason"] is None
    assert d["levels"][0]["coeff"] == 1.25  # 50/(20*2)
    assert d["levels"][0]["chosen"] is True
    assert d["levels"][1]["chosen"] is False


def test_explain_wide_gap_gate_fail():
    d = _explain([_b(0.28, 50), _b(0.27, 30), _b(0.15, 400)])
    assert d["action"] == "skip"
    assert d["rule"] == 1
    assert d["max_gap"] == 12.0
    assert d["high_sum"] == 2.0  # 1.25 + 0.75
    assert d["gate_passed"] is False
    assert d["chosen_index"] is None
    assert d["price"] is None
    # 高位 = 断层上方两档;低位不算入高位
    assert [lv["high_side"] for lv in d["levels"]] == [True, True, False]
    assert "规则1" in d["skip_reason"]
    assert "20" in d["skip_reason"]


def test_explain_wide_gap_gate_pass():
    d = _explain([_b(0.28, 50), _b(0.27, 800), _b(0.15, 400)])
    assert d["action"] == "place"
    assert d["rule"] == 1
    assert d["high_sum"] == 21.25  # 1.25 + 20
    assert d["gate_passed"] is True
    assert d["chosen_index"] == 0
    assert d["price"] == 0.28


def test_explain_mid_gap_no_gate():
    d = _explain([_b(0.28, 50), _b(0.21, 400)])
    assert d["rule"] == 2
    assert d["max_gap"] == 7.0
    assert d["high_sum"] is None
    assert d["gate_passed"] is True
    assert d["chosen_index"] == 0


def test_explain_fallthrough_marks_chosen():
    d = _explain([_b(0.28, 50), _b(0.27, 200)], x3=2)
    assert d["chosen_index"] == 1
    assert d["price"] == 0.27
    assert d["levels"][0]["chosen"] is False
    assert d["levels"][1]["chosen"] is True


def test_explain_none_qualify_skip_reason_names_threshold():
    d = _explain([_b(0.28, 50), _b(0.27, 40)], x3=10)
    assert d["action"] == "skip"
    assert d["rule"] == 3
    assert d["chosen_index"] is None
    assert "规则3" in d["skip_reason"]
    assert "10" in d["skip_reason"]


def test_explain_no_in_range_rule_none():
    d = _explain([_b(0.05, 500), _b(0.04, 500)])
    assert d["action"] == "skip"
    assert d["rule"] is None
    assert d["levels"] == []
    assert "奖励区间" in d["skip_reason"]


def test_explain_rule1_carries_gate_min():
    # 规则1 决策带 gate_min(=高位系数和门槛),供价格依据自解释门槛数值。
    d = _explain([_b(0.28, 30), _b(0.27, 20), _b(0.15, 400)], gate=25)
    assert d["rule"] == 1
    assert d["gate_min"] == 25


def test_explain_non_rule1_gate_min_none():
    d = _explain([_b(0.28, 50), _b(0.21, 400)])  # 断层7¢ -> 规则2
    assert d["rule"] == 2
    assert d["gate_min"] is None
    d3 = _explain([_b(0.28, 50), _b(0.27, 40)])  # 断层1¢ -> 规则3
    assert d3["rule"] == 3
    assert d3["gate_min"] is None
    dn = _explain([_b(0.05, 500)])  # 区间外 -> rule None
    assert dn["gate_min"] is None


def test_plan_matches_explain_wrapper():
    # plan 是 explain 的薄壳:决策一致。
    bids = [_b(0.28, 50), _b(0.27, 800), _b(0.15, 400)]
    d = _explain(bids)
    assert _plan(bids) == (d["price"], d["shares"])
    assert _plan([_b(0.05, 500)]) is None


from engine.laddering import gap_single_reason, gap_single_price_basis


def test_reason_placed_rule2():
    d = _explain([_b(0.28, 50), _b(0.21, 400)])
    r = gap_single_reason(d)
    assert "规则2(中断层)" in r
    assert "最大断层7¢" in r
    assert "选中第1档" in r
    assert "@0.2800" in r
    assert "门槛" in r


def test_reason_placed_rule1_shows_high_sum():
    d = _explain([_b(0.28, 50), _b(0.27, 800), _b(0.15, 400)])
    r = gap_single_reason(d)
    assert "规则1(宽断层)" in r
    assert "高位系数和21.25" in r
    assert "过闸" in r


def test_reason_skip_uses_skip_reason():
    d = _explain([_b(0.28, 50), _b(0.27, 30), _b(0.15, 400)])
    assert gap_single_reason(d) == d["skip_reason"]


def test_reason_fallthrough_shows_second_tier():
    d = _explain([_b(0.28, 50), _b(0.27, 200)], x3=2)
    r = gap_single_reason(d)
    assert "选中第2档" in r
    assert "@0.2700" in r


def test_price_basis_placed_has_coeff_and_source():
    d = _explain([_b(0.28, 50), _b(0.21, 400)])
    b = gap_single_price_basis(d, 0.10, 0.31)
    assert "档价 0.2800" in b
    assert "系数" in b
    assert "最大断层" in b
    assert "奖励区间[0.1000,0.3100]" in b
    assert "get_orderbook" in b


def test_price_basis_skip_no_in_range_is_minimal():
    # 区间内无买档(全在区间外):无档可枚举,退化简洁版。
    d = _explain([_b(0.05, 500), _b(0.04, 500)])
    b = gap_single_price_basis(d, 0.10, 0.31)
    assert "无可评估买档" in b
    assert "get_orderbook" in b
    assert "档价" not in b
    assert "→系数" not in b


def test_price_basis_skip_rule1_high_sum_shows_evidence():
    # 规则1 高位系数和不足:逐档证据 + 断层 + 高位系数和 + 门槛。
    d = _explain([_b(0.28, 30), _b(0.27, 20), _b(0.15, 400)])  # gate 默认 20
    assert d["action"] == "skip" and d["rule"] == 1
    b = gap_single_price_basis(d, 0.10, 0.31)
    assert "0.2800×30→系数" in b
    assert "[高位]" in b
    assert "最大断层" in b
    assert "高位系数和" in b
    assert "门槛20" in b
    assert "整市场不挂" in b
    assert "get_orderbook" in b


def test_price_basis_skip_no_coeff_shows_evidence():
    # 顺延无档过门槛(规则3):逐档系数 + 门槛。
    d = _explain([_b(0.28, 50), _b(0.27, 40)], x3=100)
    assert d["action"] == "skip" and d["rule"] == 3
    b = gap_single_price_basis(d, 0.10, 0.31)
    assert "0.2800×50→系数" in b
    assert "各档系数均 ≤ 选档门槛100" in b
    assert "get_orderbook" in b


def test_price_basis_skip_rule1_passed_no_coeff_shows_evidence():
    # 规则1 过闸(高位系数和≥门槛)但顺延无档>门槛:逐档 + 过闸 + 选档门槛。
    d = _explain([_b(0.28, 600), _b(0.27, 600), _b(0.15, 100)], x1=25)
    assert d["action"] == "skip" and d["rule"] == 1 and d["gate_passed"]
    b = gap_single_price_basis(d, 0.10, 0.31)
    assert "0.2800×600→系数" in b
    assert "过闸" in b
    assert "各档系数均 ≤ 选档门槛25" in b
    assert "get_orderbook" in b


from engine.laddering import compute_market_single_orders


def _side(bids, rmin=0.10, rmax=0.31, min_size=20):
    return {
        "bids": bids,
        "reward_range_min": rmin,
        "reward_range_max": rmax,
        "min_size": min_size,
    }


def test_single_orders_one_side_places_one():
    side = _side([_b(0.28, 50), _b(0.27, 40)])
    out = compute_market_single_orders(side, None, 1000.0, 500, AV, 10, 5, 20, 0, 0, 0)
    assert out == {"a": [(0.28, 20)], "b": []}


def test_single_orders_budget_too_small_skips():
    # 预算 3 USD:0.28×20=5.6 > 3 -> 封顶 int(3/0.28)=10 < min_size 20 -> 放弃。
    side = _side([_b(0.28, 50)])
    out = compute_market_single_orders(side, None, 3.0, 500, AV, 10, 5, 20, 0, 0, 0)
    assert out == {"a": [], "b": []}


def test_single_orders_both_sides_share_budget():
    a = _side([_b(0.28, 50)])
    b = _side([_b(0.22, 400)])
    out = compute_market_single_orders(a, b, 1000.0, 500, AV, 10, 5, 20, 0, 0, 0)
    assert out["a"] == [(0.28, 20)]
    assert out["b"] == [(0.22, 20)]


def test_single_orders_none_side_skipped():
    out = compute_market_single_orders(None, None, 1000.0, 500, AV, 10, 5, 20, 0, 0, 0)
    assert out == {"a": [], "b": []}


from engine.laddering import preview_gap_single_market


def _pside(bids, outcome="Yes", rmin=0.10, rmax=0.31, min_size=20):
    return {
        "outcome": outcome,
        "token_id": outcome + "-t",
        "min_size": min_size,
        "reward_range_min": rmin,
        "reward_range_max": rmax,
        "best_bid": 0.28,
        "best_ask": 0.30,
        "spread_cents": 2,
        "bids": bids,
    }


def test_preview_places_side_carries_full_judgment():
    a = _pside([_b(0.28, 50), _b(0.21, 400)])
    out = preview_gap_single_market(a, None, AV, 10, 5, 20, 0, 0, 0)
    assert out["b"] is None
    sa = out["a"]
    assert sa["outcome"] == "Yes"
    assert sa["rule"] == 2
    assert sa["rule_label"] == "规则2(中断层)"
    assert sa["action"] == "place"
    assert sa["max_gap"] == 7.0
    assert sa["chosen_price"] == 0.28
    assert sa["reward_range"] == [0.10, 0.31]
    # 逐档带系数 + 高位/选中标记,供前端表格展示
    assert sa["levels"][0]["coeff"] == 1.25
    assert sa["levels"][0]["chosen"] is True


def test_preview_skip_side_shows_reason():
    a = _pside([_b(0.28, 50), _b(0.27, 30), _b(0.15, 400)])  # 规则1 闸门不过
    out = preview_gap_single_market(a, None, AV, 10, 5, 20, 0, 0, 0)
    sa = out["a"]
    assert sa["action"] == "skip"
    assert sa["rule"] == 1
    assert sa["gate_passed"] is False
    assert sa["high_sum"] == 2.0
    assert "规则1" in sa["skip_reason"]


def test_preview_both_sides():
    a = _pside([_b(0.28, 50), _b(0.21, 400)], outcome="Yes")
    b = _pside([_b(0.27, 200), _b(0.26, 200)], outcome="No")
    out = preview_gap_single_market(a, b, AV, 10, 5, 20, 0, 0, 0)
    assert out["a"]["outcome"] == "Yes"
    assert out["b"]["outcome"] == "No"
    assert out["b"]["action"] == "place"


from engine.laddering import (
    explain_gap_single_order,
    compute_market_single_orders,
    preview_gap_single_market,
    gap_single_price_basis,
)


def test_cliff_below_zone_skips():
    # in_range 有 0.18/0.17(≥rmin 0.10);[0.08,0.10) 内无档(下一档直接 0.02)→ 悬崖
    out = _plan([_b(0.18, 50), _b(0.17, 40), _b(0.02, 9999)], cliff=2)
    assert out is None


def test_cliff_support_within_band_places():
    # 同上但 0.09 落在 [0.08,0.10) 内 → 有支撑 → 照常挂 0.18
    out = _plan([_b(0.18, 50), _b(0.17, 40), _b(0.09, 30), _b(0.02, 9999)], cliff=2)
    assert out == (0.18, 20)


def test_cliff_boundary_9c_support_not_dusted():
    # floor 0.10, N=1: 9¢ 支撑恰在 [0.09,0.10) 带内,不能被浮点尘误排 → 照常挂
    out = _plan(
        [_b(0.18, 50), _b(0.17, 40), _b(0.09, 30), _b(0.02, 9999)], rmin=0.10, cliff=1
    )
    assert out == (0.18, 20)


def test_cliff_disabled_places_despite_void():
    # 下方真空但 cliff=0 → 照常挂(零回归)
    out = _plan([_b(0.18, 50), _b(0.17, 40), _b(0.02, 9999)], cliff=0)
    assert out == (0.18, 20)


def test_empty_zone_reason_wins_over_cliff():
    # 区间内无买档 → 保留旧原因"奖励区间内无买档",不被悬崖判断抢先/覆盖
    d = explain_gap_single_order(
        [_b(0.02, 9999)],
        0.10,
        0.31,
        20,
        AV,
        10,
        5,
        20,
        0,
        0,
        0,
        cliff_probe_cents=2,
    )
    assert d["action"] == "skip"
    assert d["skip_reason"] == "奖励区间内无买档"
    assert d["cliff"] is False


def test_explain_cliff_sets_flag_and_reason():
    d = explain_gap_single_order(
        [_b(0.18, 50), _b(0.17, 40), _b(0.02, 9999)],
        0.10,
        0.31,
        20,
        AV,
        10,
        5,
        20,
        0,
        0,
        0,
        cliff_probe_cents=2,
    )
    assert d["action"] == "skip"
    assert d["cliff"] is True
    assert "悬崖" in d["skip_reason"]
    assert d["rule"] is None


def test_price_basis_renders_cliff_not_empty_book():
    d = explain_gap_single_order(
        [_b(0.18, 50), _b(0.17, 40), _b(0.02, 9999)],
        0.10,
        0.31,
        20,
        AV,
        10,
        5,
        20,
        0,
        0,
        0,
        cliff_probe_cents=2,
    )
    pb = gap_single_price_basis(d, 0.10, 0.31)
    assert "悬崖" in pb
    assert "无可评估买档" not in pb  # 不能误报成空区间


def test_compute_threads_cliff_probe():
    side = {
        "bids": [_b(0.18, 50), _b(0.17, 40), _b(0.02, 9999)],
        "reward_range_min": 0.10,
        "reward_range_max": 0.31,
        "min_size": 20,
    }
    out = compute_market_single_orders(
        side,
        None,
        1000.0,
        500,
        AV,
        10,
        5,
        20,
        0,
        0,
        0,
        cliff_probe_cents=2,
    )
    assert out["a"] == []


def test_preview_threads_cliff_probe():
    side = {
        "outcome": "Yes",
        "token_id": "t",
        "min_size": 20,
        "reward_range_min": 0.10,
        "reward_range_max": 0.31,
        "best_bid": 0.18,
        "best_ask": 0.19,
        "spread_cents": 1,
        "bids": [_b(0.18, 50), _b(0.17, 40), _b(0.02, 9999)],
    }
    out = preview_gap_single_market(
        side,
        None,
        AV,
        10,
        5,
        20,
        0,
        0,
        0,
        cliff_probe_cents=2,
    )
    assert out["a"]["action"] == "skip"
    assert out["a"]["cliff"] is True
