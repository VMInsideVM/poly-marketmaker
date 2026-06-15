"""engine/categories.py — 品类排除纯函数(不触网)。

采集器按品类黑名单在采集阶段排除市场:
- queried_categories: 所有模板排除集的并集 = 需向服务端查询的品类(打标签 + 相减)。
- excluded_intersection: 所有模板共同排除的品类 = 可在采集阶段安全删除的品类
  (某模板独有的排除项留到每钱包精筛 narrow,避免误删别的模板需要的市场)。
- partition_candidates: 全量市场减去交集品类命中者,并给每条候选打 tags。
"""


def queried_categories(templates: list[dict]) -> set:
    out = set()
    for t in templates:
        out.update(t.get("excluded_categories", []) or [])
    return out


def excluded_intersection(templates: list[dict]) -> set:
    sets = [set(t.get("excluded_categories", []) or []) for t in templates]
    if not sets:
        return set()
    out = sets[0]
    for s in sets[1:]:
        out = out & s
    return out


def partition_candidates(
    full_markets: list[dict],
    category_ids: dict,
    intersection_slugs: set,
) -> list[dict]:
    """全量市场 - 交集品类命中者,并给每条候选打 tags。

    Args:
        full_markets: 全量奖励市场(每条含 condition_id)。
        category_ids: {品类 slug: set(condition_id)},采集器逐品类查询所得。
        intersection_slugs: 采集阶段要删的品类(= 所有模板共同排除集)。

    Returns:
        候选池:移除属于任一交集品类的市场,并为每条加 tags =
        它命中的(被查询过的)品类 slug 列表。
    """
    removed = set()
    for slug in intersection_slugs:
        removed |= category_ids.get(slug, set())

    pool = []
    for m in full_markets:
        cid = m.get("condition_id", "")
        if cid in removed:
            continue
        tags = [slug for slug, ids in category_ids.items() if cid in ids]
        entry = dict(m)
        entry["tags"] = tags
        pool.append(entry)
    return pool
