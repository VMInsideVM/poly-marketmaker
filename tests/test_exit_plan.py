"""tests/test_exit_plan.py — 两段式离场决策纯函数(不触网)。

成本 ≤ 买一(盈利)   -> 挂卖一(best_ask)做 maker 捕捉价差;无卖一回退成本价。
成本 > 买一(保本/套牢) -> 挂成本价 ceil_to_tick(cost) 被动等回本,绝不低于成本;
                        亏损 ≥ theta_stop 兜底市价止损。
theta_loss / case_a_mode 已不再使用(签名保留以免改动调用方)。
"""

from engine.take_profit import plan_exit, market_fill_price, effective_theta_stop


def _p(
    cost,
    best_bid,
    best_ask,
    tier,
    action,
    price=None,
    theta_loss=0.02,
    theta_stop=0.05,
    mode="ask",
    size=100,
    tick=0.01,
):
    out = plan_exit(cost, best_bid, best_ask, tick, theta_loss, theta_stop, mode, size)
    assert out["tier"] == tier and out["action"] == action
    if price is not None:
        assert abs(out["price"] - price) < 1e-9
    return out


def test_profit_cost_below_bid_rests_at_ask():
    # 成本 0.30 ≤ 买一 0.31(盈利)-> 挂卖一 0.33 做 maker 捕价差(不再按成本/买一甩)。
    _p(0.30, 0.31, 0.33, "A", "rest", price=0.33)


def test_profit_boundary_cost_equals_bid_rests_at_ask():
    # 成本 == 买一 仍算盈利(≤)-> 挂卖一。
    _p(0.30, 0.30, 0.32, "A", "rest", price=0.32)


def test_profit_no_ask_falls_back_to_cost():
    # 盈利但无卖一 -> 回退挂成本价(成本≤买一,仍按≥成本成交)。
    _p(0.30, 0.31, None, "A", "rest", price=0.30)


def test_profit_crossed_book_floors_at_cost():
    # 异常/幻影盘口:买一 0.16(≥成本,触发盈利分支)但卖一 0.13 < 成本 0.14。
    # 盈利分支必须对成本取下限 -> 挂 0.14,绝不挂在成本下方 0.13(这正是 0.13 卖的疑似根因 B)。
    out = _p(0.14, 0.16, 0.13, "A", "rest", price=0.14)
    assert out["price"] >= 0.14


def test_case_a_mode_ignored():
    # case_a_mode 已死:即便传 "market" 盈利侧仍挂卖一。
    _p(0.30, 0.31, 0.33, "A", "rest", price=0.33, mode="market")


def test_underwater_within_stop_rests_at_cost():
    # 成本 0.40 > 买一 0.39,亏损 0.01 < theta_stop -> 挂成本价 0.40 等回本(不跟卖一)。
    _p(0.40, 0.39, 0.41, "B_park", "rest", price=0.40)


def test_underwater_ask_below_cost_rests_at_cost():
    # "买15卖13.5" 真根因:卖一 0.135 已低于成本 0.15 -> 挂成本价 0.15,绝不挂在成本下方。
    out = _p(0.15, 0.13, 0.135, "B_park", "rest", price=0.15, tick=0.005)
    assert out["price"] >= 0.15


def test_underwater_ask_below_cost_rests_at_cost_subcent():
    # "买19.3卖18.7":卖一 0.187 < 成本 0.193 -> 挂成本价 0.193。
    out = _p(0.193, 0.185, 0.187, "B_park", "rest", price=0.193, tick=0.001)
    assert out["price"] >= 0.193


def test_underwater_ceils_cost_to_tick():
    # 成本带零头 0.305 > 买一 0.30 -> 挂 ceil_to_tick(0.305)=0.31。
    _p(0.305, 0.30, 0.34, "B_park", "rest", price=0.31)


def test_underwater_loss_ge_theta_stop_market():
    # 兜底止损:亏损 0.05 ≥ theta_stop -> B0 市价清仓。
    _p(0.40, 0.35, 0.36, "B0", "market", theta_stop=0.05)


def test_no_bid_rests_at_cost():
    # 无买盘无法判断盈亏 -> 挂成本价等回本(绝不低于成本),不跟卖一。
    out = plan_exit(0.40, None, 0.41, 0.01, 0.02, 0.05, "ask", 100)
    assert (
        out["action"] == "rest"
        and out["tier"] == "B_park"
        and abs(out["price"] - 0.40) < 1e-9
    )


def test_no_book_at_all_noop():
    out = plan_exit(0.40, None, None, 0.01, 0.02, 0.05, "ask", 100)
    assert out["action"] == "noop"


# --- market_fill_price: B0 清仓的真实成交价(绝不用 Data API 现价)---


def test_market_fill_uses_response_amounts():
    # 卖出 60 份收到 2.88 USDC -> 均价 0.048(精确取自响应成交额)。
    resp = {"makingAmount": 60.0, "takingAmount": 2.88}
    assert abs(market_fill_price(resp, 0.06, 0.13) - 0.048) < 1e-9


def test_market_fill_rejects_insane_ratio_falls_back_to_bid():
    # 字段语义反了(比值>1)-> 不采信,退回买一,绝不用现价。
    resp = {"makingAmount": 2.88, "takingAmount": 60.0}
    assert market_fill_price(resp, 0.06, 0.13) == 0.06


def test_market_fill_no_amounts_falls_back_to_bid():
    # 响应没有成交额(常见)-> 退回买一(FAK 起吃价),不是现价。
    assert market_fill_price({"success": True}, 0.06, 0.13) == 0.06


def test_market_fill_never_uses_cur_when_bid_present():
    # 关键:有买一就绝不退到现价——现价会把亏损显示成盈利。
    assert market_fill_price({}, 0.06, 0.13) == 0.06


def test_market_fill_no_bid_falls_back_to_cur():
    # 连买一都没有(理论上 B0 不会走到)-> 才退回现价。
    assert market_fill_price({}, None, 0.13) == 0.13


# --- take_profit_mode="market" + 止损可关(effective_theta_stop off)---


def test_effective_theta_stop_off_returns_none():
    assert effective_theta_stop(0.30, "off", 20, 5) is None


def _pm(
    cost,
    best_bid,
    best_ask,
    tier,
    action,
    price=None,
    theta_stop=0.05,
    size=100,
    tick=0.01,
):
    out = plan_exit(
        cost,
        best_bid,
        best_ask,
        tick,
        0.02,
        theta_stop,
        "ask",
        size,
        take_profit_mode="market",
    )
    assert out["tier"] == tier and out["action"] == action
    if price is not None:
        assert abs(out["price"] - price) < 1e-9
    return out


def test_market_mode_profit_cost_below_bid_market_sells():
    # 成本 0.28 < 买一 0.30(浮盈)-> 市价清仓。
    _pm(0.28, 0.30, 0.33, "A_market", "market")


def test_market_mode_boundary_cost_equals_bid_rests_at_cost():
    # 成本 == 买一 归保本侧 -> 挂成本价。
    _pm(0.30, 0.30, 0.33, "B_park", "rest", price=0.30)


def test_market_mode_underwater_stop_off_rests_at_cost():
    # 套牢 + 止损关(theta_stop None)-> 挂成本价,绝不 B0。
    out = plan_exit(
        0.40, 0.30, 0.42, 0.01, 0.02, None, "ask", 100, take_profit_mode="market"
    )
    assert out["tier"] == "B_park" and out["action"] == "rest"
    assert abs(out["price"] - 0.40) < 1e-9


def test_market_mode_underwater_stop_on_fires_b0():
    # 套牢 + 止损开:亏损 0.10 >= theta_stop 0.05 -> B0 市价。
    _pm(0.40, 0.30, 0.42, "B0", "market", theta_stop=0.05)


def test_market_mode_no_book_noops():
    out = plan_exit(
        0.30, None, None, 0.01, 0.02, None, "ask", 100, take_profit_mode="market"
    )
    assert out["tier"] == "none" and out["action"] == "noop"


def test_maker_mode_stop_off_no_b0():
    # maker 默认 + 止损关:套牢挂成本价、不 B0(theta_stop None 被安全跳过)。
    out = plan_exit(0.40, 0.30, 0.42, 0.01, 0.02, None, "ask", 100)
    assert out["tier"] == "B_park" and out["action"] == "rest"
    assert abs(out["price"] - 0.40) < 1e-9


# --- effective_theta_stop: 可配置强平阈值(比例 / 固定金额)---


def test_effective_stop_percent_of_cost():
    # 按比例 20%:0.40 × 20% = 0.08。
    assert abs(effective_theta_stop(0.40, "percent", 20, 5) - 0.08) < 1e-9


def test_effective_stop_percent_scales_with_cost():
    # 成本越低、止损越紧:0.10 × 20% = 0.02。
    assert abs(effective_theta_stop(0.10, "percent", 20, 5) - 0.02) < 1e-9


def test_effective_stop_fixed_cents():
    # 按固定金额:5¢ = 0.05,与成本无关。
    assert abs(effective_theta_stop(0.40, "fixed", 20, 5) - 0.05) < 1e-9
    assert abs(effective_theta_stop(0.10, "fixed", 20, 5) - 0.05) < 1e-9


def test_effective_stop_garbage_falls_back_to_fixed():
    # 比例参数异常 -> 回退固定美分,不让阈值变成 0/负。
    assert abs(effective_theta_stop(0.40, "percent", None, 5) - 0.05) < 1e-9
