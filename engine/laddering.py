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
    """厚度下界区间「厚度 > upper」:返回第一个满足 ct > upper 的区间动作。

    `upper` 现在是「累计厚度须超过的阈值」(下界,非上界;键名沿用 upper 以兼容已存模板)。
    取第一个命中,故同档内多个区间须按阈值**从大到小**排列,否则小阈值区间会遮住大的;
    `upper=None` 为「其余」兜底(总匹配),都不满足时返回 skip。
    """
    for interval in tier_rule:
        upper = interval.get("upper")
        if upper is None or ct > upper:
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
            # 预算/敞口封顶后若不足奖励最低份数,放弃该档(不挂残档):挂出的单
            # 达不到 rewards_min_size 既赚不到奖励、又白占资金/敞口。后续更便宜的
            # 档仍可能用同一剩余预算凑满 min_size,故 continue 而非 break。
            if shares < side["min_size"]:
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
        # 按「剩余可成交量 = 原始 - 已成交」判定量是否达标,而非原始挂单量:
        # 部分成交后 original_size 不变(仍=原始),只有 size_matched 反映已吃掉的量。
        # 若仍用 original_size,被吃到不足最低份数的残单会被误判为「量符」而保留(现象2)。
        osize = float(o.get("original_size", o.get("size", 0)) or 0)
        remaining = osize - float(o.get("size_matched", 0) or 0)
        tgt = target.get(op)
        # 每个目标价只保留一笔:价量符且该价尚未保留过 -> 留;否则(价漂/量不符/
        # 同价重复)-> 撤。漏掉 op not in keep 会让同价的重复单都被 keep、量翻倍。
        if (
            tgt is not None
            and op not in keep
            and abs(remaining - tgt) <= max(1.0, 0.01 * tgt)
        ):
            keep.add(op)
        else:
            oid = o.get("id")
            if oid is not None:
                cancel_ids.append(oid)
    to_place = [(p, s) for (p, s) in ladder if round(float(p), 4) not in keep]
    return cancel_ids, to_place


def _verbose_levels(side, tiers_k):
    """每个 bid 价位 -> 标注 dict;合格档(in_range 且厚度>=1)按序给 tier_index(<tiers_k)。"""
    min_size = side["min_size"]
    rmin, rmax = side["reward_range_min"], side["reward_range_max"]
    levels, running, tier_no = [], 0.0, 0
    for lvl in side["bids"]:
        price, size = float(lvl["price"]), float(lvl["size"])
        thickness = size / min_size if min_size > 0 else 0.0
        running += thickness
        in_range = rmin <= price <= rmax
        qualifies = in_range and thickness >= 1
        tier_index, skip_reason = None, None
        if not qualifies:
            skip_reason = "超出奖励范围" if not in_range else "厚度<1"
        elif tier_no < tiers_k:
            tier_index, tier_no = tier_no, tier_no + 1
        else:
            skip_reason = "超过最大档数"
        levels.append(
            {
                "price": price,
                "size": size,
                "thickness": thickness,
                "cumulative_thickness": running,
                "in_range": in_range,
                "qualifies": qualifies,
                "tier_index": tier_index,
                "shares": 0,
                "amount": 0.0,
                "skip_reason": skip_reason,
            }
        )
    return levels


def preview_market_ladders(side_a, side_b, tier_rules, budget_usd, max_shares):
    """两边共享敞口的逐档预演(只读、不下单)。

    分配口径与 compute_market_ladders 一致(档序升序、同档先 a 后 b、resolve_tier_share
    同款规则、按 USD/份额封顶);保留全部 bid 价位并标注 skip_reason。不应用 §8 双边地板。
    side_x:{"outcome","token_id","min_size","reward_range_min","reward_range_max",
            "best_bid","best_ask","spread_cents","bids":[{price,size}...]} 或 None。
    返回 {"a":<side|None>,"b":<side|None>};
    side = {"outcome","token_id","best_bid","best_ask","spread_cents","reward_range":[min,max],
            "levels":[...],"total_tiers","total_shares","total_amount","double_sided_warn"}。
    """
    tiers_k = len(tier_rules)
    out, lv = {}, {}
    for key, side in (("a", side_a), ("b", side_b)):
        if side is None:
            out[key], lv[key] = None, []
            continue
        lv[key] = _verbose_levels(side, tiers_k)
        out[key] = {
            "outcome": side.get("outcome", ""),
            "token_id": side.get("token_id", ""),
            "best_bid": side.get("best_bid"),
            "best_ask": side.get("best_ask"),
            "spread_cents": side.get("spread_cents"),
            "reward_range": [side["reward_range_min"], side["reward_range_max"]],
            "levels": lv[key],
            "total_tiers": 0,
            "total_shares": 0,
            "total_amount": 0.0,
            "double_sided_warn": False,
        }
    by_tier = {"a": {}, "b": {}}
    for key in ("a", "b"):
        for L in lv[key]:
            if L["tier_index"] is not None:
                by_tier[key][L["tier_index"]] = L
    spent_usd, spent_shares = 0.0, 0
    for j in range(tiers_k):
        for key, side in (("a", side_a), ("b", side_b)):
            if side is None:
                continue
            L = by_tier[key].get(j)
            if L is None:
                continue
            price, ct = L["price"], L["cumulative_thickness"]
            remaining_usd = budget_usd - spent_usd
            shares = resolve_tier_share(
                ct, tier_rules[j], price, side["min_size"], remaining_usd
            )
            if shares <= 0:
                L["skip_reason"] = "规则判定不挂"
                continue
            cap_usd = int(remaining_usd / price) if price > 0 else 0
            cap_shares = max_shares - spent_shares
            shares = min(shares, cap_usd, cap_shares)
            if shares <= 0:
                L["skip_reason"] = "预算/敞口用尽"
                continue
            # 与 compute_market_ladders 一致:封顶后不足最低份数的档放弃。
            if shares < side["min_size"]:
                L["skip_reason"] = "不足最低份数"
                continue
            L["shares"], L["amount"] = shares, price * shares
            spent_usd += price * shares
            spent_shares += shares
    for key in ("a", "b"):
        if out[key] is None:
            continue
        placed = [L for L in lv[key] if L["shares"] > 0]
        out[key]["total_tiers"] = len(placed)
        out[key]["total_shares"] = sum(L["shares"] for L in placed)
        out[key]["total_amount"] = sum(L["amount"] for L in placed)
    return out
