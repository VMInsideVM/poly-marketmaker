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


def apply_double_sided_floor(ladders, min_price_double_cents):
    """§8:若任一已定档价 < 阈值,则要求两边都有>0档;否则整市场清空。"""
    threshold = min_price_double_cents / 100.0
    has_sub = any(price < threshold for side in ladders.values() for (price, _) in side)
    if not has_sub:
        return ladders
    if ladders.get("a") and ladders.get("b"):
        return ladders
    return {"a": [], "b": []}


def reconcile_buy_orders(ladder, resting_buys):
    """撤改收敛(v4 §6):把某 token 的现挂买单收敛到目标 ladder。

    ladder: [(price, shares), ...] 该 token 的目标多档。
    resting_buys: [{"id","price","original_size"/"size"}, ...] 该 token 当前在挂买单。
    返回 (cancel_ids, to_place):
      - 现挂单价不在目标、或同价但量不符(容差 max(1,1%)) -> 撤(进 cancel_ids)。
      - 价量都符 -> 保持(不撤不挂)。
      - 目标档里没被任何现挂单保持到的价 -> 挂(进 to_place)。
    """
    target = {round(float(p), 4): s for (p, s) in ladder}
    keep = set()
    cancel_ids = []
    for o in resting_buys:
        op = round(float(o.get("price", 0) or 0), 4)
        osize = float(o.get("original_size", o.get("size", 0)) or 0)
        tgt = target.get(op)
        if tgt is not None and abs(osize - tgt) <= max(1.0, 0.01 * tgt):
            keep.add(op)
        else:
            oid = o.get("id")
            if oid is not None:
                cancel_ids.append(oid)
    to_place = [(p, s) for (p, s) in ladder if round(float(p), 4) not in keep]
    return cancel_ids, to_place
