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
    bids, rmin=0.10, rmax=0.31, min_size=20, av=None, wide=10, mid=5, gate=20, x=0
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
        x,
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
    # x=2:买一 0.28 系数 1.25 不>2 -> 顺延买二 0.27 系数 200/40=5 >2 -> 挂 0.27。
    out = _plan([_b(0.28, 50), _b(0.27, 200)], x=2)
    assert out == (0.27, 20)


def test_none_qualify_returns_none():
    out = _plan([_b(0.28, 50), _b(0.27, 40)], x=10)
    assert out is None


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
