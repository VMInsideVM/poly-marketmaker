"""tests/test_exit_plan.py — 两段式离场决策纯函数(不触网)。

成本 < 买一(盈利) -> 挂成本价(marketable、立即按买一成交)。
成本 ≥ 买一(保本/套牢) -> 挂卖一被动等回本;亏损 ≥ theta_stop 兜底市价止损。
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


def test_profit_cost_below_bid_rests_at_cost():
    # 成本 0.30 < 买一 0.31 -> 挂成本价 0.30(非卖一 0.33);marketable、立即按买一成交。
    _p(0.30, 0.31, 0.33, "A", "rest", price=0.30)


def test_case_a_mode_ignored_rests_at_cost():
    # case_a_mode 已死:即便传 "market" 也仍挂成本价,不再市价。
    _p(0.30, 0.31, 0.33, "A", "rest", price=0.30, mode="market")


def test_profit_ceils_cost_to_tick_and_not_above_bid():
    # 成本带零头 -> ceil_to_tick 向上取整;且挂价 ≤ 买一(始终 marketable,不卖穿成本)。
    out = _p(0.305, 0.31, 0.33, "A", "rest", price=0.31)
    assert out["price"] <= 0.31


def test_boundary_cost_equals_bid_parks_at_ask():
    # 成本 == 买一(不再算盈利)-> 走"挂卖一"侧,亏损 0 < theta_stop -> B_park 挂卖一。
    _p(0.30, 0.30, 0.32, "B_park", "rest", price=0.32)


def test_underwater_within_stop_parks_at_ask():
    # 成本 0.40 > 买一 0.39,亏损 0.01 < theta_stop -> 挂卖一 0.41 被动等回本(无 sweep)。
    _p(0.40, 0.39, 0.41, "B_park", "rest", price=0.41)


def test_underwater_deeper_still_parks_at_ask():
    # 亏损 0.03 < theta_stop -> 仍 B_park 挂卖一,不市价割肉。
    _p(0.40, 0.37, 0.41, "B_park", "rest", price=0.41)


def test_underwater_loss_ge_theta_stop_market():
    # 兜底止损:亏损 0.05 ≥ theta_stop -> B0 市价清仓。
    _p(0.40, 0.35, 0.36, "B0", "market", theta_stop=0.05)


def test_no_bid_parks_at_ask():
    out = plan_exit(0.40, None, 0.41, 0.01, 0.02, 0.05, "ask", 100)
    assert out["action"] == "rest" and abs(out["price"] - 0.41) < 1e-9


def test_no_book_at_all_noop():
    out = plan_exit(0.40, None, None, 0.01, 0.02, 0.05, "ask", 100)
    assert out["action"] == "noop"


def test_profit_falls_back_to_cost_when_no_ask():
    out = plan_exit(0.30, 0.31, None, 0.01, 0.02, 0.05, "ask", 100)
    assert out["action"] == "rest" and abs(out["price"] - 0.30) < 1e-9
