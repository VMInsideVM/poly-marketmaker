"""tests/test_positions.py — pure position-derived helpers."""

from engine.positions import held_side_info


def test_held_side_info_held_assets_only_positive_size():
    pos = [
        {"conditionId": "c1", "asset": "yes", "size": 100.0, "curPrice": 0.4},
        {"conditionId": "c1", "asset": "no", "size": 0, "curPrice": 0.6},
    ]
    held_assets, value, shares = held_side_info(pos)
    assert held_assets == {"yes"}


def test_held_side_info_value_and_shares_aggregate_by_market():
    pos = [
        {"conditionId": "c1", "asset": "yes", "size": 100.0, "curPrice": 0.40},
        {"conditionId": "c1", "asset": "no", "size": 50.0, "curPrice": 0.60},
    ]
    held_assets, value, shares = held_side_info(pos)
    assert held_assets == {"yes", "no"}
    assert value["c1"] == 100.0 * 0.40 + 50.0 * 0.60  # 70.0
    assert shares["c1"] == 150.0


def test_held_side_info_skips_nonpositive_and_missing_fields():
    pos = [
        {
            "conditionId": "c1",
            "asset": "yes",
            "size": 0,
            "curPrice": 0.4,
        },  # size<=0 skip
        {"asset": "x", "size": 100.0, "curPrice": 0.5},  # 无 conditionId
        {"conditionId": "c2", "size": 100.0},  # 无 asset/curPrice
        {
            "conditionId": "c3",
            "asset": "z",
            "size": None,
            "curPrice": 0.5,
        },  # None size skip
    ]
    held_assets, value, shares = held_side_info(pos)
    assert "x" in held_assets  # size>0、有 asset -> 进集合(无 conditionId 不影响)
    assert "yes" not in held_assets  # size 0 -> 跳过
    assert "z" not in held_assets  # None size -> 跳过
    assert value.get("c2") == 0.0  # 无 curPrice -> 市值计 0
    assert shares.get("c2") == 100.0
    assert "c1" not in value  # size 0 整项跳过,无聚合


def test_held_side_info_empty():
    assert held_side_info([]) == (set(), {}, {})
