"""engine/categories.py — 品类白名单纯函数(不触网)。

采集器对整份 curated 名单给市场打标签(tag_pool),再按白名单判定(market_wanted):
- included_union: 所有模板 included_categories 的并集(发现阶段预筛用的"想要"集)。
- any_include_other: 是否有模板收「其他/未分类」(决定预筛是否保留无 curated 标签者)。
- market_wanted: 命中 included,或(空 tags 且 include_other)。空 tags 恒指"无 curated
  标签"——故打标签必须覆盖整份 catalog,否则未勾选品类会被误判成「其他」。
"""


def included_union(templates: list[dict]) -> set:
    out = set()
    for t in templates:
        out.update(t.get("included_categories", []) or [])
    return out


def any_include_other(templates: list[dict]) -> bool:
    return any(bool(t.get("include_other", False)) for t in templates)


def tag_pool(full_markets: list[dict], category_ids: dict, catalog_slugs) -> list[dict]:
    """给每条市场加 tags = 命中的 catalog slug(有序);不删除任何市场。

    Args:
        full_markets: 全量奖励市场(每条含 condition_id)。
        category_ids: {catalog slug: set(condition_id)},逐 slug 查询所得。
        catalog_slugs: 打标签的 curated slug 全集(决定 tags 与「其他」判定)。
    """
    ordered = list(catalog_slugs)
    pool = []
    for m in full_markets:
        cid = m.get("condition_id", "")
        tags = [s for s in ordered if cid in category_ids.get(s, set())]
        entry = dict(m)
        entry["tags"] = tags
        pool.append(entry)
    return pool


def market_wanted(tags, included, include_other: bool) -> bool:
    tags = tags or []
    if set(included) & set(tags):
        return True
    return bool(include_other) and not tags


def count_by_category(full_ids, category_ids: dict, catalog_slugs):
    """返回 ({slug: 命中数}, 其他数)。计数在整全量集上做,与是否被勾选无关。"""
    counts = {s: len(category_ids.get(s, set())) for s in catalog_slugs}
    covered = set()
    for s in catalog_slugs:
        covered |= category_ids.get(s, set())
    other = len(set(full_ids) - covered)
    return counts, other
