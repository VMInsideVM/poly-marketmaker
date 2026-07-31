"""tests/test_categories.py — 品类白名单纯函数(不触网)。"""

from engine.categories import (
    included_union,
    any_include_other,
    tag_pool,
    market_in_categories,
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


def test_market_in_categories_hit():
    assert market_in_categories(["politics"], {"politics", "ai"}, False) is True


def test_market_in_categories_any_tag_hits():
    # 多标签市场:任一 tag 命中即算落在集合内(新市场保护的口径)
    assert (
        market_in_categories(["politics", "geopolitics"], {"geopolitics"}, False)
        is True
    )


def test_market_in_categories_miss():
    assert market_in_categories(["sports"], {"politics"}, False) is False


def test_market_in_categories_untagged_follows_flag():
    # 空 tags = 无 curated 标签(即「其他/未分类」),由 include_untagged 单独管
    assert market_in_categories([], {"politics"}, True) is True
    assert market_in_categories([], {"politics"}, False) is False


def test_market_in_categories_tagged_but_unlisted_is_not_untagged():
    # 有 curated 标签但不在名单里:即便 include_untagged 也不算(它不是「其他」)
    assert market_in_categories(["sports"], {"politics"}, True) is False


def test_market_in_categories_empty_slugs():
    # 空名单 + 不收未分类 -> 恒 False。这是「模板缺键按不保护处理」的依据。
    assert market_in_categories(["politics"], [], False) is False
    assert market_in_categories([], [], False) is False


def test_market_in_categories_none_tags():
    # tags 为 None(市场记录还没打过标签)不能抛
    assert market_in_categories(None, {"politics"}, False) is False
    assert market_in_categories(None, {"politics"}, True) is True


def test_count_by_category():
    full_ids = {"A", "B", "C", "D"}
    category_ids = {"politics": {"A"}, "economy": {"A", "B"}, "ai": set()}
    counts, other = count_by_category(
        full_ids, category_ids, ["politics", "economy", "ai"]
    )
    assert counts == {"politics": 1, "economy": 2, "ai": 0}
    assert other == 2  # C、D 未命中任何 curated
