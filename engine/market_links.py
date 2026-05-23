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
