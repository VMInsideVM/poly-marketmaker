"""tests/test_categories.py — 品类白名单纯函数(不触网)。"""

from engine.categories import (
    included_union,
    any_include_other,
    tag_pool,
    market_wanted,
    count_by_category,
)


def test_included_union_is_union():
    templates = [
        {"included_categories": ["politics", "economy"]},
        {"included_categories": ["economy", "ai"]},
    ]
    assert included_union(templates) == {"politics", "economy", "ai"}


def test_any_include_other():
    assert (
        any_include_other([{"include_other": False}, {"include_other": True}]) is True
    )
    assert any_include_other([{"include_other": False}]) is False
    assert any_include_other([]) is False


def test_tag_pool_attaches_curated_tags():
    full = [{"condition_id": "A"}, {"condition_id": "B"}, {"condition_id": "C"}]
    category_ids = {"politics": {"A"}, "economy": {"A", "B"}, "ai": set()}
    pool = tag_pool(full, category_ids, ["politics", "economy", "ai"])
    by = {m["condition_id"]: m for m in pool}
    assert set(by["A"]["tags"]) == {"politics", "economy"}
    assert by["B"]["tags"] == ["economy"]
    assert by["C"]["tags"] == []  # 无 curated 标签 -> 其他


def test_market_wanted_whitelist_hit():
    assert market_wanted(["politics"], {"politics", "ai"}, False) is True


def test_market_wanted_whitelist_miss():
    assert market_wanted(["sports"], {"politics"}, False) is False


def test_market_wanted_other_bucket():
    # 空 tags + include_other -> 收
    assert market_wanted([], {"politics"}, True) is True
    # 空 tags + 不收其他 -> 不收
    assert market_wanted([], {"politics"}, False) is False


def test_market_wanted_categorized_but_unselected_is_not_other():
    # 有 curated 标签但没被勾选:即便 include_other 也不收(它不是"其他")
    assert market_wanted(["sports"], {"politics"}, True) is False


def test_count_by_category():
    full_ids = {"A", "B", "C", "D"}
    category_ids = {"politics": {"A"}, "economy": {"A", "B"}, "ai": set()}
    counts, other = count_by_category(
        full_ids, category_ids, ["politics", "economy", "ai"]
    )
    assert counts == {"politics": 1, "economy": 2, "ai": 0}
    assert other == 2  # C、D 未命中任何 curated
