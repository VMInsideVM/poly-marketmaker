"""tests/test_order_sizing.py — 老策略挂单份数(纯函数,不触网)。"""

from engine.order_sizing import compute_order_size


def test_min_mode_returns_min_size_regardless_of_balance_and_price():
    assert compute_order_size("min", 0.50, 1000.0, 10, 0.0) == 10
    assert compute_order_size("min", 0.99, 5.0, 7, 0.0) == 7


def test_balance_mode_floors_balance_over_price():
    assert compute_order_size("balance", 0.50, 1000.0, 10, 0.0) == 2000


def test_balance_mode_floors_inexact_division():
    # 除不尽向下取整(取整向上会在成交时超出余额)。
    assert compute_order_size("balance", 0.30, 1000.0, 10, 0.0) == 3333


def test_balance_mode_skips_when_below_min_size():
    # floor(3.0/0.50)=6 < min_size 10 -> None
    assert compute_order_size("balance", 0.50, 3.0, 10, 0.0) is None


def test_custom_mode_floors_cap_over_price():
    # cap 50 美元,余额充足 -> floor(50/0.50)=100
    assert compute_order_size("custom", 0.50, 1000.0, 10, 50.0) == 100


def test_custom_mode_capped_by_balance_when_cap_exceeds_balance():
    # cap 50 但余额只有 20 -> 用 20:floor(20/0.50)=40
    assert compute_order_size("custom", 0.50, 20.0, 10, 50.0) == 40


def test_custom_mode_skips_when_below_min_size():
    # cap 4 美元 -> floor(4/0.50)=8 < min_size 10 -> None
    assert compute_order_size("custom", 0.50, 1000.0, 10, 4.0) is None


def test_custom_mode_zero_cap_skips():
    # cap 0 -> 预算 0 -> size 0 < min_size -> None(不挂)
    assert compute_order_size("custom", 0.50, 1000.0, 10, 0.0) is None


def test_non_positive_price_returns_none():
    assert compute_order_size("balance", 0.0, 1000.0, 10, 0.0) is None
    assert compute_order_size("custom", -0.10, 1000.0, 10, 50.0) is None
    assert compute_order_size("min", 0.0, 1000.0, 10, 0.0) is None


def test_unknown_mode_falls_back_to_min():
    assert compute_order_size("weird", 0.50, 1000.0, 10, 50.0) == 10
