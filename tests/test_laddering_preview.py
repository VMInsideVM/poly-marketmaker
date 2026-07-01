"""tests/test_laddering_preview.py — preview_market_ladders verbose 预演。"""

from engine.laddering import preview_market_ladders


def _side(outcome="YES"):
    # min_size=100;中点 0.55、奖励范围 [0.51,0.59]
    return {
        "outcome": outcome,
        "token_id": "t_" + outcome,
        "min_size": 100,
        "reward_range_min": 0.51,
        "reward_range_max": 0.59,
        "best_bid": 0.54,
        "best_ask": 0.56,
        "spread_cents": 2.0,
        "bids": [
            {"price": 0.54, "size": 150},  # 厚 1.5 合格 -> 档0
            {"price": 0.53, "size": 80},  # 厚 0.8 <1 跳过
            {"price": 0.52, "size": 300},  # 厚 3.0 合格 -> 档1
            {"price": 0.51, "size": 120},  # 厚 1.2 合格 -> 档2
            {"price": 0.50, "size": 200},  # 价 < 0.51 超范围
        ],
    }


# 三档规则:每档都 min_size(=100 份)
TIER_RULES = [
    [{"upper": None, "action": {"type": "min_size"}}],
    [{"upper": None, "action": {"type": "min_size"}}],
    [{"upper": None, "action": {"type": "min_size"}}],
]


def test_levels_thickness_and_qualification():
    out = preview_market_ladders(_side(), None, TIER_RULES, 1000.0, 10000)
    levels = out["a"]["levels"]
    assert [round(l["thickness"], 2) for l in levels] == [1.5, 0.8, 3.0, 1.2, 2.0]
    assert [round(l["cumulative_thickness"], 1) for l in levels] == [
        1.5,
        2.3,
        5.3,
        6.5,
        8.5,
    ]
    # 合格档(in_range 且 厚度>=1):0.54/0.52/0.51
    assert [l["tier_index"] for l in levels] == [0, None, 1, 2, None]
    assert levels[1]["skip_reason"] == "厚度<1"
    assert levels[4]["skip_reason"] == "超出奖励范围"


def test_shares_allocated_to_qualifying_tiers():
    out = preview_market_ladders(_side(), None, TIER_RULES, 1000.0, 10000)
    placed = [l for l in out["a"]["levels"] if l["shares"] > 0]
    assert [l["price"] for l in placed] == [0.54, 0.52, 0.51]
    assert all(l["shares"] == 100 for l in placed)  # min_size 规则
    assert out["a"]["total_tiers"] == 3
    assert out["a"]["total_shares"] == 300


def test_share_cap_exhaustion_marks_skip():
    # 份额敞口上限只够第 1 档(100 份);其余合格档应标"预算/敞口用尽"
    # 用 max_shares 触发耗尽,避免依赖 USD 封顶的浮点运算
    out = preview_market_ladders(_side(), None, TIER_RULES, 100000.0, 100)
    levels = out["a"]["levels"]
    assert levels[0]["shares"] == 100
    assert levels[2]["shares"] == 0 and levels[2]["skip_reason"] == "预算/敞口用尽"


def test_usd_budget_cap_drops_sub_min_rung():
    # 镜像 compute_market_ladders:预算紧时,按 USD 封顶后不足 min_size 的档放弃。
    # 预算 60:第1档 0.54*100=54;余约 6,第2档 0.52 -> int(6/0.52)=11 < min_size(100)
    # -> 该档放弃并标「不足最低份数」(旧版会照挂 11 份残档,赚不到奖励)。
    out = preview_market_ladders(_side(), None, TIER_RULES, 60.0, 10000)
    levels = out["a"]["levels"]
    assert levels[0]["shares"] == 100
    assert levels[2]["shares"] == 0
    assert levels[2]["skip_reason"] == "不足最低份数"


def test_none_side_returns_none():
    out = preview_market_ladders(None, _side("NO"), TIER_RULES, 1000.0, 10000)
    assert out["a"] is None
    assert out["b"]["outcome"] == "NO"


_AVT = [
    {"upper": 0.20, "value": 1},
    {"upper": 0.25, "value": 1.5},
    {"upper": 0.30, "value": 2},
]


def test_preview_risk_mode_marks_price_over_table():
    side = {
        "outcome": "YES",
        "token_id": "t",
        "min_size": 100,
        "best_bid": 0.4,
        "best_ask": 0.45,
        "spread_cents": 5,
        "reward_range_min": 0.0,
        "reward_range_max": 1.0,
        "bids": [{"price": "0.40", "size": "500"}, {"price": "0.15", "size": "200"}],
    }
    rule = [{"upper": None, "action": {"type": "min_size"}}]
    out = preview_market_ladders(
        side,
        None,
        [rule, rule],
        100000.0,
        100000,
        tier_match_var="risk_coefficient",
        amount_value_table=_AVT,
    )
    levels = {L["price"]: L for L in out["a"]["levels"]}
    assert levels[0.40]["skip_reason"] == "价超金额表"
    assert levels[0.15]["match_value"] == 2.0  # 200/100 / 1
