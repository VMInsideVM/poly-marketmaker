# engine/positions.py
"""Pure helpers derived from Polymarket Data API /positions (no network/IO)."""


def held_condition_ids(positions: list[dict]) -> set[str]:
    """返回当前持有仓位(size>0)的 market(condition_id)集合。

    positions 为 Data API /positions 的返回(每项含 conditionId / size)。YES/NO
    任一方向有仓都算该 market 已持仓;缺 conditionId 或 size<=0 的项忽略。size 做
    None/字符串安全转换。
    """
    out: set[str] = set()
    for p in positions:
        cid = p.get("conditionId", "")
        if cid and float(p.get("size", 0) or 0) > 0:
            out.add(cid)
    return out
