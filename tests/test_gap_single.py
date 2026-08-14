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
    shares=None,
    gate2=0,
    gate3=0,
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
        shares=shares,
        rule2_high_coeff_sum_min=gate2,
        rule3_high_coeff_sum_min=gate3,
    )


def test_normal_market_picks_highest_in_range():
    # 相邻价差 1¢ < 5 -> 规则3;买一 0.28 系数 50/(20*2)=1.25 > 0 -> 挂 0.28 一单 20 股。
    out = _plan([_b(0.28, 50), _b(0.27, 40)])
    assert out == (0.28, 20)


def test_out_of_range_levels_join_gap_but_not_selection():
    # 0.05 低于 rmin 0.10:不进选档,但参与断层。0.28->0.05 断层 23¢ > 10 -> 规则1,
    # 高位仅{0.28}系数 50/(20*2)=1.25 < 20 -> 整市场不挂。
    out = _plan([_b(0.28, 50), _b(0.05, 9999)])
    assert out is None


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
    gate2=0,
    gate3=0,
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
        rule2_high_coeff_sum_min=gate2,
        rule3_high_coeff_sum_min=gate3,
    )


def test_explain_normal_market_full_decision():
    d = _explain([_b(0.28, 50), _b(0.27, 40)])
    assert d["action"] == "place"
    assert d["rule"] == 3
    assert d["max_gap"] == 1.0
    assert d["min_coeff"] == 0
    assert d["high_sum"] == 1.25  # 断层就在这两档之间,高位仅{0.28} 50/(20*2)
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
    assert d["high_sum"] == 1.25  # 高位仅{0.28};默认门槛0 -> 不拦
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


def test_explain_gate_min_present_on_every_rule():
    # 三级都带 gate_min/high_sum(旧版只有规则1 有,规则2/3 是 None)。
    d = _explain([_b(0.28, 50), _b(0.21, 400)], gate2=7)  # 断层7¢ -> 规则2
    assert d["rule"] == 2
    assert d["gate_min"] == 7
    assert d["high_sum"] is not None
    d3 = _explain([_b(0.28, 50), _b(0.27, 40)], gate3=2)  # 断层1¢ -> 规则3
    assert d3["rule"] == 3
    assert d3["gate_min"] == 2
    assert d3["high_sum"] is not None
    dn = _explain([_b(0.05, 500)])  # 区间内无买档 -> rule None
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


def test_has_cliff_below_detects_void():
    # 下沿 0.10 往下 2¢ 带 [0.08,0.10) 内无档(下一档直接 0.02)-> 悬崖
    from engine.laddering import has_cliff_below

    assert has_cliff_below([_b(0.18, 50), _b(0.02, 9999)], 0.10, 2) is True


def test_has_cliff_below_support_in_band():
    from engine.laddering import has_cliff_below

    assert has_cliff_below([_b(0.18, 50), _b(0.09, 30)], 0.10, 2) is False


def test_has_cliff_below_boundary_not_dusted():
    # floor 0.10、N=1:9¢ 恰落在 [0.09,0.10) 带内,不能被浮点尘排掉判成悬崖。
    from engine.laddering import has_cliff_below

    assert has_cliff_below([_b(0.09, 30)], 0.10, 1) is False


def test_has_cliff_below_disabled_or_no_book():
    # N<=0 = 关闭;空买单簿 = 无从判断,都不报悬崖(不撤单)。
    from engine.laddering import has_cliff_below

    assert has_cliff_below([_b(0.02, 9999)], 0.10, 0) is False
    assert has_cliff_below([], 0.10, 2) is False


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
    # 下方真空但 cliff=0 → 悬崖不否决,照常挂。
    # gate=0 是为了隔离变量:全簿口径下 0.17->0.02 是 15¢ 断层 -> 规则1,高位{0.18,0.17}
    # 系数和 4.5 会被默认闸门 20 拦下,那样就测不出悬崖开关本身了。
    out = _plan([_b(0.18, 50), _b(0.17, 40), _b(0.02, 9999)], cliff=0, gate=0)
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


def test_shares_param_overrides_min_size():
    # 档位模块配 40 份 -> 挂 40 份(不再等于 min_size 20)。
    out = _plan([_b(0.28, 50), _b(0.27, 40)], shares=40)
    assert out == (0.28, 40)


def test_shares_default_none_keeps_min_size():
    out = _plan([_b(0.28, 50), _b(0.27, 40)])
    assert out == (0.28, 20)


def test_coeff_still_uses_min_size_not_shares():
    # 系数按市场最低份额算:50/(20×2)=1.25 > 门槛 1.2 -> 挂。
    # 若误用 shares=40 会算成 0.625 < 1.2 被拒,本测试即失败。
    out = _plan([_b(0.28, 50), _b(0.27, 40)], x3=1.2, shares=40)
    assert out == (0.28, 40)


def test_compute_budget_caps_shares_but_keeps_above_min_size():
    # 配置 40 份、预算只够 30 份(30 ≥ min_size 20)-> 照挂 30(资格线以上不放弃)。
    # 7.6/0.25 = 30.4 -> 封顶 30。
    out = compute_market_single_orders(
        _side([_b(0.25, 50)]), None, 7.6, 500, AV, 10, 5, 20, 0, 0, 0, shares=40
    )
    assert out["a"] == [(0.25, 30)]


def test_compute_budget_below_min_size_drops_side():
    # 2.6/0.25 = 10.4 -> 封顶 10 < min_size 20 -> 放弃该侧。
    out = compute_market_single_orders(
        _side([_b(0.25, 50)]), None, 2.6, 500, AV, 10, 5, 20, 0, 0, 0, shares=40
    )
    assert out["a"] == []


def test_preview_reports_chosen_shares():
    side = dict(
        _side([_b(0.28, 50)]),
        outcome="YES",
        token_id="t",
        best_bid=0.28,
        best_ask=0.31,
        spread_cents=3.0,
    )
    out = preview_gap_single_market(side, None, AV, 10, 5, 20, 0, 0, 0, 0, shares=40)
    assert out["a"]["action"] == "place" and out["a"]["chosen_shares"] == 40


def test_preview_skip_has_none_chosen_shares():
    side = dict(
        _side([]),  # 无买档 -> skip
        outcome="YES",
        token_id="t",
        best_bid=None,
        best_ask=None,
        spread_cents=None,
    )
    out = preview_gap_single_market(side, None, AV, 10, 5, 20, 0, 0, 0, 0, shares=40)
    assert out["a"]["action"] == "skip" and out["a"]["chosen_shares"] is None


def test_compute_default_shares_uses_each_sides_min_size():
    # 参数完整性:shares=None 时 b 侧须用自己的 min_size(30),
    # 不得被 a 侧的循环局部量(20)污染——污染时 b 侧会被误判 <min_size 整边丢弃。
    side_a = dict(_side([_b(0.28, 200)]), min_size=20)
    side_b = dict(_side([_b(0.28, 200)]), min_size=30)
    out = compute_market_single_orders(
        side_a, side_b, 100.0, 500, AV, 10, 5, 20, 0, 0, 0
    )
    assert out["a"] == [(0.28, 20)]
    assert out["b"] == [(0.28, 30)]


def test_compute_shares_param_survives_side_a_budget_cap():
    # a 侧被预算封顶成 31 份后,b 侧仍须用配置的 40 份(而非被污染的 31)。
    # b 侧用低价+放宽的奖励区间,使 a 封顶后剩余预算仍够 b 挂满 40。
    side_a = {
        "bids": [_b(0.30, 500)],
        "reward_range_min": 0.10,
        "reward_range_max": 0.31,
        "min_size": 20,
    }
    side_b = {
        "bids": [_b(0.005, 500)],
        "reward_range_min": 0.001,
        "reward_range_max": 0.31,
        "min_size": 20,
    }
    out = compute_market_single_orders(
        side_a, side_b, 9.59, 500, AV, 10, 5, 20, 0, 0, 0, shares=40
    )
    assert out["a"] == [(0.30, 31)]
    assert out["b"] == [(0.005, 40)]


# --- 断层口径 = 整个买单簿(2026-08-14) ---------------------------------------
# 断层分级与高位系数和在「全部买档」上算(含奖励区间外的档),选档仍只在区间内顺延。
# 起因:区间内只剩一档的市场 max_gap 恒为 0,永远落到最松的规则3,区间下方的深坑
# 完全不参与判定(实盘 0xe457df06… 地震市场按规则3 挂进了 21¢)。


def test_gap_spans_whole_book_single_in_range_level():
    # 实盘形状:区间内只有 0.21 一档,下方 0.19->0.08 是 11¢ 深坑。
    # 旧口径:区间内不足 2 档 -> max_gap=0 -> 规则3 -> 挂。
    # 新口径:全簿断层 11¢ > 10 -> 规则1;高位{0.21,0.19}系数和
    #        236.24/(20*1.5)=7.87 + 120/(20*1)=6.0 = 13.87 < 20 -> 整市场不挂。
    bids = [_b(0.21, 236.24), _b(0.19, 120), _b(0.08, 9.14), _b(0.07, 16.35)]
    assert _plan(bids, rmin=0.195, rmax=0.275) is None


def test_high_side_includes_out_of_range_levels():
    # 最大断层 0.18->0.02 落在区间下沿(0.20)以下,高位={0.28,0.22,0.18},其中 0.18 在区间外。
    # 系数 1.25 + 2.0 + 15.0 = 18.25;闸门 18 -> 过闸挂 0.28。
    # 若高位漏掉区间外的 0.18(系数 15),和只有 3.25 < 18 会误判不挂。
    bids = [_b(0.28, 50), _b(0.22, 60), _b(0.18, 300), _b(0.02, 10)]
    assert _plan(bids, rmin=0.20, rmax=0.31, gate=18) == (0.28, 20)


def test_high_side_out_of_range_gate_still_can_fail():
    # 同一副买单簿,闸门抬到 20:18.25 < 20 -> 整市场不挂(证明上一条是真过闸,不是没算闸门)。
    bids = [_b(0.28, 50), _b(0.22, 60), _b(0.18, 300), _b(0.02, 10)]
    assert _plan(bids, rmin=0.20, rmax=0.31, gate=20) is None


def test_out_of_range_thick_level_never_selected():
    # 闸门放到 0 必过闸;区间外 0.05 挂着 9999 的超厚档,选档只在区间内 -> 仍挂 0.28。
    out = _plan([_b(0.28, 50), _b(0.05, 9999)], gate=0)
    assert out == (0.28, 20)


def test_whole_book_single_level_still_rule3():
    # 全簿只有一档:无相邻价差可算 -> max_gap=0 -> 规则3(维持现状,不因口径改动而变)。
    d = _explain([_b(0.28, 50)])
    assert d["rule"] == 3
    assert d["max_gap"] == 0.0
    assert d["action"] == "place"


def test_in_range_dense_book_unaffected_by_out_of_range_gap():
    # 区间内 0.28/0.27 密盘,但区间外 0.05 制造 22¢ 断层 -> 新口径判规则1。
    # 高位仅{0.28}系数 1.25;闸门 1 -> 过闸,选档 x1=0 -> 挂 0.28。
    d = _explain([_b(0.28, 50), _b(0.27, 40), _b(0.05, 10)], gate=1)
    assert d["rule"] == 1
    assert d["max_gap"] == 22.0
    assert d["action"] == "place"
    assert d["price"] == 0.28


def test_levels_carry_whole_book_with_in_range_flag():
    # levels 展开全簿(价降序)并标 in_range,供预演/价格依据看清高位系数和的构成。
    d = _explain([_b(0.28, 50), _b(0.05, 9999)], gate=0)
    assert [lv["price"] for lv in d["levels"]] == [0.28, 0.05]
    assert [lv["in_range"] for lv in d["levels"]] == [True, False]
    assert [lv["high_side"] for lv in d["levels"]] == [True, False]


def test_chosen_index_points_into_whole_book_levels():
    # chosen_index 是 levels(全簿)的下标:区间外的 0.30 排在前面时不能把下标错位到它身上。
    d = _explain(
        [_b(0.30, 10), _b(0.28, 50), _b(0.27, 40)], rmin=0.10, rmax=0.29, gate=0
    )
    assert d["action"] == "place"
    assert d["price"] == 0.28
    assert d["levels"][d["chosen_index"]]["price"] == 0.28


# --- 规则2/3 各自的高位系数和闸门(2026-08-14) -------------------------------


def test_rule2_gate_blocks_when_high_sum_below():
    # 断层 7¢ -> 规则2。高位={0.28} 系数 50/(20*2)=1.25 < 门槛 5 -> 整市场不挂。
    assert _plan([_b(0.28, 50), _b(0.21, 400)], gate2=5) is None


def test_rule2_gate_passes_at_exactly_threshold():
    # 高位系数和恰等门槛 -> 放行(与规则1 同约定:< 才拦)。
    assert _plan([_b(0.28, 50), _b(0.21, 400)], gate2=1.25) == (0.28, 20)


def test_rule2_gate_default_zero_never_blocks():
    # 默认 0:高位再薄也放行,升级零回归。
    assert _plan([_b(0.28, 1), _b(0.21, 400)]) == (0.28, 20)


def test_rule3_gate_blocks_when_high_sum_below():
    # 断层 1¢ -> 规则3。高位={0.28} 系数 1.25 < 门槛 3 -> 不挂。
    assert _plan([_b(0.28, 50), _b(0.27, 40)], gate3=3) is None


def test_rule3_gate_passes_at_exactly_threshold():
    assert _plan([_b(0.28, 50), _b(0.27, 40)], gate3=1.25) == (0.28, 20)


def test_rule3_gate_default_zero_never_blocks():
    assert _plan([_b(0.28, 1), _b(0.27, 40)]) == (0.28, 20)


def test_gates_do_not_cross_between_rules():
    # 规则2 的市场(断层7¢)只认 gate2:gate/gate3 抬到 999 也不该拦它。
    assert _plan([_b(0.28, 50), _b(0.21, 400)], gate=999, gate3=999) == (0.28, 20)
    # 规则3 的市场(断层1¢)只认 gate3。
    assert _plan([_b(0.28, 50), _b(0.27, 40)], gate=999, gate2=999) == (0.28, 20)
    # 规则1 的市场(断层12¢)只认 gate。
    bids1 = [_b(0.28, 50), _b(0.27, 800), _b(0.15, 400)]
    assert _plan(bids1, gate2=999, gate3=999) == (0.28, 20)


def test_rule1_gate_behavior_unchanged():
    # 回归:规则1 的闸门与默认 20 一字不动。
    assert _plan([_b(0.28, 50), _b(0.27, 30), _b(0.15, 400)]) is None
    assert _plan([_b(0.28, 50), _b(0.27, 800), _b(0.15, 400)]) == (0.28, 20)


def test_rule2_gate_skip_reason_names_the_rule():
    d = _explain([_b(0.28, 50), _b(0.21, 400)], gate2=5)
    assert d["action"] == "skip"
    assert "规则2" in d["skip_reason"]
    assert "高位系数和" in d["skip_reason"]
    assert "整市场不挂" in d["skip_reason"]


def test_single_level_book_gate_uses_that_level():
    # 边界(spec 明确):全簿只有一档 -> max_gap=0 -> 规则3,高位就是那唯一一档。
    # 系数 50/(20*2)=1.25:门槛 2 拦下,门槛 1 放行。
    assert _plan([_b(0.28, 50)], gate3=2) is None
    assert _plan([_b(0.28, 50)], gate3=1) == (0.28, 20)
