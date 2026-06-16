"""tests/test_exit_plan.py — 三段式离场决策纯函数(不触网)。"""

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


def test_case_a_rest_at_ask():
    _p(0.30, 0.31, 0.33, "A", "rest", price=0.33)


def test_case_a_market_mode():
    out = plan_exit(0.30, 0.31, 0.33, 0.01, 0.02, 0.05, "market", 100)
    assert out["tier"] == "A" and out["action"] == "market"


def test_case_a_boundary_cost_equals_bid():
    _p(0.30, 0.30, 0.32, "A", "rest", price=0.32)


def test_b0_force_clear_when_loss_ge_theta_stop():
    _p(0.40, 0.35, 0.36, "B0", "market", theta_stop=0.05)


def test_b_sweep_when_bid_ge_floor():
    _p(0.40, 0.39, 0.41, "B_sweep", "sweep", price=0.38)


def test_b_park_when_bid_below_floor():
    _p(0.40, 0.37, 0.41, "B_park", "rest", price=0.41)


def test_no_bid_parks_at_ask():
    out = plan_exit(0.40, None, 0.41, 0.01, 0.02, 0.05, "ask", 100)
    assert out["action"] == "rest" and abs(out["price"] - 0.41) < 1e-9


def test_no_book_at_all_noop():
    out = plan_exit(0.40, None, None, 0.01, 0.02, 0.05, "ask", 100)
    assert out["action"] == "noop"


def test_rest_falls_back_to_cost_when_no_ask():
    out = plan_exit(0.30, 0.31, None, 0.01, 0.02, 0.05, "ask", 100)
    assert out["action"] == "rest" and abs(out["price"] - 0.30) < 1e-9
