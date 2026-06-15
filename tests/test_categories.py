"""tests/test_categories.py — 品类排除纯函数(不触网)。"""

from engine.categories import (
    excluded_intersection,
    queried_categories,
    partition_candidates,
)


def test_queried_categories_is_union():
    templates = [
        {"excluded_categories": ["sports", "esports"]},
        {"excluded_categories": ["weather"]},
    ]
    assert queried_categories(templates) == {"sports", "esports", "weather"}


def test_excluded_intersection_is_common():
    templates = [
        {"excluded_categories": ["sports", "esports", "weather"]},
        {"excluded_categories": ["sports", "weather"]},
    ]
    assert excluded_intersection(templates) == {"sports", "weather"}


def test_excluded_intersection_empty_when_no_templates():
    assert excluded_intersection([]) == set()


def test_partition_subtracts_intersection_and_tags():
    full = [{"condition_id": "A"}, {"condition_id": "B"}, {"condition_id": "C"}]
    category_ids = {"sports": {"A"}, "weather": {"B"}, "esports": {"A"}}
    pool = partition_candidates(full, category_ids, {"sports", "weather"})
    assert {m["condition_id"] for m in pool} == {"C"}


def test_partition_tags_attached():
    full = [{"condition_id": "A"}, {"condition_id": "C"}]
    category_ids = {"sports": {"A"}, "esports": {"A"}, "weather": set()}
    pool = partition_candidates(full, category_ids, set())
    by_id = {m["condition_id"]: m for m in pool}
    assert set(by_id["A"]["tags"]) == {"sports", "esports"}
    assert by_id["C"]["tags"] == []


def test_partition_empty_intersection_keeps_all():
    full = [{"condition_id": "A"}, {"condition_id": "B"}]
    pool = partition_candidates(full, {"sports": {"A"}}, set())
    assert len(pool) == 2
