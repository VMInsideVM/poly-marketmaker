"""tests/test_positions.py — pure position-derived helpers."""

from engine.positions import held_condition_ids


def test_includes_condition_with_positive_size():
    assert held_condition_ids([{"conditionId": "c1", "size": 100.0}]) == {"c1"}


def test_excludes_zero_and_negative_size():
    pos = [{"conditionId": "c1", "size": 0}, {"conditionId": "c2", "size": -5}]
    assert held_condition_ids(pos) == set()


def test_excludes_missing_or_empty_condition_id():
    assert held_condition_ids([{"size": 100.0}]) == set()
    assert held_condition_ids([{"conditionId": "", "size": 100.0}]) == set()


def test_empty_list_empty_set():
    assert held_condition_ids([]) == set()


def test_yes_and_no_of_same_condition_collapse_to_one():
    pos = [
        {"conditionId": "c1", "size": 100.0, "asset": "yes"},
        {"conditionId": "c1", "size": 50.0, "asset": "no"},
    ]
    assert held_condition_ids(pos) == {"c1"}


def test_none_and_string_size_are_handled():
    pos = [
        {"conditionId": "c1", "size": None},  # None -> treated as 0 -> excluded
        {"conditionId": "c2", "size": "30"},  # numeric string -> 30 -> included
    ]
    assert held_condition_ids(pos) == {"c2"}
