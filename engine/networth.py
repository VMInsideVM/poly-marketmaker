"""engine/networth.py — 账户净值快照纯函数(不触网)。"""


def should_snapshot(last_date, today):
    """跨天判断:上次快照日期(YYYY-MM-DD,None=本进程内还没记过)与今天不同 -> 该记。"""
    return last_date != today


def positions_value(positions):
    """Data API /positions 的持仓市值合计。

    currentValue 优先,缺失/非法回退 size×curPrice;size<=0 或非法的项忽略。
    仅供净值展示,不进任何交易决策(与「curPrice 禁用作成本」铁律不冲突——
    那条铁律限定的是离场成本口径)。
    """
    total = 0.0
    for p in positions or []:
        try:
            size = float(p.get("size", 0) or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        cv = p.get("currentValue")
        if cv is not None:
            try:
                total += float(cv)
                continue
            except (TypeError, ValueError):
                pass
        try:
            total += size * float(p.get("curPrice", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total
