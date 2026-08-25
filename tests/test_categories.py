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


# --- 排除品类(veto_categories):命中即一票否决 -----------------------------
# 起因:朋友的钱包模板勾了 politics、没勾 elections,却在
# "Will Sergio Moro win the Governor of Paraná election?" 挂了单 —— 该市场官方
# 标签是 ['politics','elections'],白名单是「命中任一即要」的 OR 语义,交集非空
# 就过了。他候选池里带 elections 的 54 个市场中 44 个同时带 politics(81%),
# 靠调白名单挡不住,所以另设排除集。


def test_market_wanted_excluded_vetoes_overlapping_market():
    # 正是实盘那一条:勾了 politics、排除 elections,市场两个标签都有 -> 不做
    assert (
        market_wanted(["politics", "elections"], {"politics"}, True, {"elections"})
        is False
    )


def test_market_wanted_excluded_beats_included():
    # 同一 slug 既在白名单又在排除集(DB 里存进了矛盾配置):排除优先,绝不下单
    assert market_wanted(["politics"], {"politics"}, True, {"politics"}) is False


def test_market_wanted_excluded_does_not_touch_other_markets():
    # 排除集只否决命中它的市场,不带该标签的照旧
    assert market_wanted(["politics"], {"politics"}, True, {"elections"}) is True


def test_market_wanted_excluded_vetoes_even_untagged_path():
    # 「其他」市场(空 tags)不带任何 curated 标签,排除集命中不了它,仍由
    # include_other 决定 —— 排除集不能顺手把「其他」也一并关掉
    assert market_wanted([], {"politics"}, True, {"elections"}) is True
    assert market_wanted([], {"politics"}, False, {"elections"}) is False


def test_market_wanted_empty_excluded_is_zero_regression():
    # 不配排除集(默认 []/None/缺参)时逐字等于原行为
    for exc in ([], None, ()):
        assert market_wanted(["politics", "elections"], {"politics"}, True, exc) is True
        assert market_wanted(["sports"], {"politics"}, True, exc) is False
    assert market_wanted(["politics", "elections"], {"politics"}, True) is True


def test_market_in_categories_signature_unaffected_by_excluded():
    # 排除集只能长在 market_wanted 上。market_in_categories 被「新市场保护」共用
    # (scanner.py 的 skip_new_categories 判定),不做市和保护新市场是两回事,
    # 不能被排除集牵连。
    assert market_in_categories(["politics", "elections"], {"politics"}, True) is True
