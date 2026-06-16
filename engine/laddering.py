"""engine/laddering.py — 多档挂单纯函数引擎(不触网)。

v4 §5:一侧最多 K 档,从买一往下取「在奖励区间内且厚度>=1」的有效价位。
每档份额由「累加厚度->份额规则表」决定;整市场敞口两边共享。
"""


def build_ladder(bids, reward_range_min, reward_range_max, min_size, tiers_k):
    """构建单边档价梯。

    Args:
        bids: 按价降序的 [{price, size}](字符串或数值均可)。
        reward_range_min/max: 奖励区间(含端点)。
        min_size: 最低份数(>0)。
        tiers_k: 最多取几档。

    Returns:
        [{"price": float, "cumulative_thickness": float}, ...],档1=最高合格价位。
        累加厚度 = 从买一往下到该档(含)所有 bid 价位的 size/min_size 之和。
        合格档 = 价位在奖励区间内且该价位厚度(size/min_size)>=1。
    """
    if min_size <= 0 or not bids:
        return []
    tiers = []
    running_ct = 0.0
    for level in bids:
        price = float(level["price"])
        size = float(level["size"])
        thickness = size / min_size
        running_ct += thickness
        if reward_range_min <= price <= reward_range_max and thickness >= 1:
            tiers.append({"price": price, "cumulative_thickness": running_ct})
            if len(tiers) >= tiers_k:
                break
    return tiers


def _interval_action(tier_rule, ct):
    """半开升序区间 [前一上界, upper):返回包含 ct 的区间动作。"""
    for interval in tier_rule:
        upper = interval.get("upper")
        if upper is None or ct < upper:
            return interval.get("action", {"type": "skip"})
    return {"type": "skip"}


def resolve_tier_share(
    cumulative_thickness, tier_rule, price, min_size, remaining_budget_usd
):
    """按该档累加厚度命中的区间动作,算份额(int>=0,0=不挂)。"""
    action = _interval_action(tier_rule, cumulative_thickness)
    t = action.get("type")
    if t == "min_size":
        return int(min_size)
    if t == "fixed_shares":
        return int(action.get("shares", 0))
    if t == "fixed_amount":
        if price <= 0:
            return 0
        s = int(float(action.get("usd", 0)) / price)
        return s if s >= min_size else int(min_size)
    if t == "wallet_total":
        return int(remaining_budget_usd / price) if price > 0 else 0
    return 0


def compute_market_ladders(
    side_a, side_b, tier_rules, market_budget_usd, max_exposure_shares
):
    """两边共享敞口算多档计划。

    side_a/side_b: {"bids","reward_range_min","reward_range_max","min_size"} 或 None。
    side_a 先于 side_b 在同一档内扣预算(档序升序、同档先 a 后 b)。
    返回 {"a":[(price,shares),...], "b":[...]}(仅含份额>0)。
    """
    tiers_k = len(tier_rules)
    rungs = {}
    for key, side in (("a", side_a), ("b", side_b)):
        if side is None:
            rungs[key] = []
        else:
            rungs[key] = build_ladder(
                side["bids"],
                side["reward_range_min"],
                side["reward_range_max"],
                side["min_size"],
                tiers_k,
            )
    out = {"a": [], "b": []}
    spent_usd = 0.0
    spent_shares = 0
    for j in range(tiers_k):
        for key, side in (("a", side_a), ("b", side_b)):
            if side is None or j >= len(rungs[key]):
                continue
            rung = rungs[key][j]
            price = rung["price"]
            ct = rung["cumulative_thickness"]
            remaining_usd = market_budget_usd - spent_usd
            shares = resolve_tier_share(
                ct, tier_rules[j], price, side["min_size"], remaining_usd
            )
            if shares <= 0:
                continue
            cap_by_usd = int(remaining_usd / price) if price > 0 else 0
            cap_by_shares = max_exposure_shares - spent_shares
            shares = min(shares, cap_by_usd, cap_by_shares)
            if shares <= 0:
                continue
            out[key].append((price, shares))
            spent_usd += price * shares
            spent_shares += shares
    return out
