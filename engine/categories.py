"""engine/categories.py — 品类白名单纯函数(不触网)。

采集器对整份 curated 名单给市场打标签(tag_pool),再按白名单判定(market_wanted):
- included_union: 所有模板 included_categories 的并集(发现阶段预筛用的"想要"集)。
- any_include_other: 是否有模板收「其他/未分类」(决定预筛是否保留无 curated 标签者)。
- veto_union: 所有模板 veto_categories 的并集(发现阶段还要多查哪些 slug)。
- market_in_categories: 市场是否落在某个品类集合内(命中任一 slug,或空 tags 且收未分类)。
  做市白名单 market_wanted 与新市场保护名单共用它。空 tags 恒指"无 curated
  标签"——故打标签必须覆盖整份 catalog,否则未勾选品类会被误判成「其他」。

键名用 veto_categories 而非 excluded_categories:后者是 v3.0.0 之前的黑名单键
(语义是「不做这些、其余都做」,与白名单二选一),用户库里仍有残留值(实测某台机器
的默认模板还存着 ["sports","esports","weather"])。同名不同义会让旧值被当成新语义
读出来,升级后凭空多出几个一票否决品类。tests/test_database.py 那条
`assert "excluded_categories" not in TEMPLATE_DEFAULTS` 守的正是这件事。
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


def market_in_categories(tags, slugs, include_untagged: bool) -> bool:
    """市场是否落在给定品类集合内。

    命中任一 slug -> True;空 tags(= 无 curated 标签,即「其他/未分类」)且
    include_untagged -> True;其余 False。

    做市白名单(market_wanted)与新市场保护名单共用这一份口径,两处判定不会漂
    (同 has_cliff_below 被下单与 Step3 共用)。
    """
    tags = tags or []
    if set(slugs) & set(tags):
        return True
    return bool(include_untagged) and not tags


def veto_union(templates: list[dict]) -> set:
    """所有模板 veto_categories 的并集(发现阶段决定「还要多查哪些 slug」)。

    排除集的 slug **必须一并去查**:tag_pool 只按查过的 slug 打标签,不查
    elections 就打不出 elections 标签,market_wanted 的排除分支永远不成立,
    配了等于没配。
    """
    out = set()
    for t in templates:
        out.update(t.get("veto_categories", []) or [])
    return out


def market_wanted(tags, included, include_other: bool, veto=()) -> bool:
    """做市白名单:先过排除集(命中即一票否决),再走 included/其他 的常规判定。

    白名单是 OR 语义(命中任一勾选品类即要),而 Polymarket 的品类互相重叠——实盘
    某选举市场官方标签是 ['politics','elections'],模板勾了 politics 没勾
    elections,交集非空就过了闸(候选池里带 elections 的 54 个市场有 44 个同时带
    politics)。靠收窄白名单挡不住这种重叠,故另设排除集一票否决。

    排除集**只长在做市白名单上**,不进 market_in_categories:后者被「新市场保护」
    共用,不做市和保护新市场是两回事,不能被牵连。
    空 tags(「其他/未分类」)不带任何 curated 标签,排除集碰不到它,仍归
    include_other 管。
    """
    if veto and (set(veto) & set(tags or [])):
        return False
    return market_in_categories(tags, included, include_other)


def count_by_category(full_ids, category_ids: dict, catalog_slugs):
    """返回 ({slug: 命中数}, 其他数)。计数在整全量集上做,与是否被勾选无关。"""
    counts = {s: len(category_ids.get(s, set())) for s in catalog_slugs}
    covered = set()
    for s in catalog_slugs:
        covered |= category_ids.get(s, set())
    other = len(set(full_ids) - covered)
    return counts, other
