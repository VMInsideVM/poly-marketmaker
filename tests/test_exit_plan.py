"""tests/test_exit_plan.py — 两段式离场决策纯函数(不触网)。

成本 ≤ 买一(盈利)   -> 挂卖一(best_ask)做 maker 捕捉价差;无卖一回退成本价。
成本 > 买一(保本/套牢) -> 挂成本价 ceil_to_tick(cost) 被动等回本,绝不低于成本;
                        亏损 ≥ theta_stop 兜底市价止损。
theta_loss / case_a_mode 已不再使用(签名保留以免改动调用方)。
"""

from engine.take_profit import plan_exit


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
