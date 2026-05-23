"""engine/market_links.py — 解析市场展示名 + Polymarket 链接。

纯函数(无 I/O),便于单测。condition_id -> 元信息 的映射来自
Database.get_market_meta()。
"""


def market_url(meta_entry: dict) -> str:
    """从 market_meta 条目构造 Polymarket 链接。

    market_slug 优先(/market/...),否则 event_slug(/event/...)。
    条目缺失或无 slug 时返回空串。
    """
    if not meta_entry:
        return ""
    ms = (meta_entry.get("market_slug") or "").strip()
    es = (meta_entry.get("event_slug") or "").strip()
    if ms:
        return f"https://polymarket.com/market/{ms}"
    if es:
        return f"https://polymarket.com/event/{es}"
    return ""


def enrich_with_market_meta(rows: list, meta: dict, id_key: str) -> list:
    """按 row[id_key]==condition_id 给每行补 market_name + market_url。

    - market_url:meta 命中则构造链接,否则空串。
    - market_name:仅当行里还没有非空 market_name 时才用 meta 的名字填充
      (避免覆盖持仓已有的 Data API title)。
    就地修改 rows 并返回。
    """
    meta = meta or {}
    for r in rows:
        cid = r.get(id_key, "") or ""
        entry = meta.get(cid)
        r["market_url"] = market_url(entry)
        if not r.get("market_name"):
            r["market_name"] = entry.get("name", "") if entry else ""
    return rows


def ensure_market_meta(condition_ids, db, fetch, max_lookup: int = 50):
    """确保 market_meta 含给定 condition_ids 的条目,缺的用 fetch(Gamma)解析并落库。

    - 只解析不在 market_meta 里的 id(本地命中/已负缓存的不再查)。
    - fetch 返回的命中项 upsert 真名+slug;Gamma 应答但不含的 id upsert 空行
      做负缓存(避免每次轮询重打 Gamma)。
    - fetch 抛异常(Gamma 临时不可用)则不写负缓存,留待下次轮询重试。
    - 单次最多解析 max_lookup 个,其余下次轮询再补。
    返回(可能已更新的)condition_id -> 元信息 映射。永不抛出。
    """
    meta = db.get_market_meta()
    missing = [c for c in dict.fromkeys(condition_ids) if c and c not in meta]
    if not missing:
        return meta
    missing = missing[:max_lookup]
    try:
        resolved = fetch(missing)
    except Exception:
        return meta  # 临时失败:不负缓存,下次重试
    for c in missing:
        r = resolved.get(c)
        if r:
            db.upsert_market_meta(
                c, r.get("name", ""), r.get("market_slug", ""), r.get("event_slug", "")
            )
        else:
            db.upsert_market_meta(c, "", "", "")  # 负缓存:Gamma 无此市场
    return db.get_market_meta()
