"""engine/laddering.py — 网关式单档挂单纯函数引擎(不触网)。

v4 用户策略:每侧在奖励区间内按相邻价差分级(宽/中/密断层),选一个系数超门槛的
买档挂一单;整市场敞口两边共享。含金额数值查表与买单撤改收敛。
"""


def amount_value(price, table):
    """金额数值查表:返回第一个 price <= upper 的 value(按 upper 升序);价超最大 upper -> None。

    table: [{"upper": float, "value": float}](顺序无所谓,内部按 upper 升序取)。
    低端下界由价格区间旗 min_price_cents 兜。表空/无匹配 -> None(该档不挂)。
    """
    if not table:
        return None
    rows = []
    for r in table:
        try:
            rows.append((float(r["upper"]), float(r["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x[0])
    for upper, value in rows:
        if price <= upper:
            return value
    return None


def explain_gap_single_order(
    bids,
    reward_range_min,
    reward_range_max,
    min_size,
    amount_value_table,
    gap_wide_cents,
    gap_mid_cents,
    gap_high_coeff_sum_min,
    rule1_min_coeff,
    rule2_min_coeff,
    rule3_min_coeff,
):
    """网关式单档挂单的完整判断(纯函数,不下单)。

    单个 token 的买单簿 -> 决策 dict,既驱动 plan_gap_single_order,也供记账/预演
    展示完整依据(每个市场怎么按断层单档规则判、判的原因、价格依据)。字段:
      action: "place"|"skip"
      rule: 1|2|3|None(None=区间内无买档/无簿)
      max_gap: 最大相邻价差(美分);<2 档时 0
      min_coeff: 该级选档门槛(rule1/2/3 之一);rule=None 时 None
      high_sum: 规则1 高位系数和;非规则1 为 None
      gate_passed: 规则1 闸门是否通过;规则2/3 恒 True;rule=None 为 False
      levels: 区间内买档(价降序),每项 {price,size,coeff,high_side,chosen}
      chosen_index: 选中档在 levels 的下标;跳过为 None
      price/shares: 选中档价 / int(min_size);跳过为 None
      skip_reason: 跳过原因(中文);挂单为 None
    归级口径:价差 10/5 归中一级(规则1 严格 >宽、规则3 严格 <中);高位系数和 ==
    门槛放行;选档严格 > 门槛。价差按分四舍五入去浮点尘。
    """
    d = {
        "action": "skip",
        "rule": None,
        "max_gap": 0.0,
        "min_coeff": None,
        "high_sum": None,
        "gate_passed": False,
        "levels": [],
        "chosen_index": None,
        "price": None,
        "shares": None,
        "skip_reason": None,
    }
    if min_size <= 0 or not bids:
        d["skip_reason"] = "无买单簿或最低份数<=0"
        return d
    in_range = sorted(
        (
            {"price": float(b["price"]), "size": float(b["size"])}
            for b in bids
            if reward_range_min <= float(b["price"]) <= reward_range_max
        ),
        key=lambda lv: lv["price"],
        reverse=True,
    )
    if not in_range:
        d["skip_reason"] = "奖励区间内无买档"
        return d
    for lv in in_range:
        av = amount_value(lv["price"], amount_value_table)
        lv["coeff"] = (lv["size"] / (min_size * av)) if (av and av > 0) else 0.0
    # 最大相邻价差 + 劈分点:高位 = in_range[0 .. split_idx](价差上方一侧)。
    max_gap = 0.0
    split_idx = len(in_range) - 1
    for i in range(len(in_range) - 1):
        # 按分四舍五入去浮点尘:否则 0.28-0.18 会算成 10.000000000000002,把恰 10¢ 的
        # 价差误判成 >10¢ 宽断层;并列价差也会被尘埃打破而选错劈分点。
        gap = round((in_range[i]["price"] - in_range[i + 1]["price"]) * 100.0, 6)
        if gap > max_gap:
            max_gap = gap
            split_idx = i
    for idx, lv in enumerate(in_range):
        lv["high_side"] = idx <= split_idx
        lv["chosen"] = False
    d["max_gap"] = max_gap
    d["levels"] = in_range

    # 按最大价差归级,取该级的选档门槛;仅规则1(宽断层)加「高位风险系数和」市场闸门。
    if max_gap > gap_wide_cents:
        rule, min_coeff = 1, rule1_min_coeff
        high_sum = sum(lv["coeff"] for lv in in_range[: split_idx + 1])
        d["rule"], d["min_coeff"], d["high_sum"] = 1, min_coeff, high_sum
        if high_sum < gap_high_coeff_sum_min:
            d["skip_reason"] = (
                f"规则1(宽断层,最大断层{max_gap:g}¢):高位系数和 {high_sum:g}"
                f" < 门槛 {gap_high_coeff_sum_min:g} → 整市场不挂"
            )
            return d
        d["gate_passed"] = True
    elif max_gap >= gap_mid_cents:
        rule, min_coeff = 2, rule2_min_coeff
        d["rule"], d["min_coeff"], d["gate_passed"] = 2, min_coeff, True
    else:
        rule, min_coeff = 3, rule3_min_coeff
        d["rule"], d["min_coeff"], d["gate_passed"] = 3, min_coeff, True

    # 顺延:自上而下第一个 coeff > 该级门槛。
    for idx, lv in enumerate(in_range):
        if lv["coeff"] > min_coeff:
            lv["chosen"] = True
            d["action"] = "place"
            d["chosen_index"] = idx
            d["price"] = lv["price"]
            d["shares"] = int(min_size)
            return d
    d["skip_reason"] = (
        f"规则{rule}(最大断层{max_gap:g}¢):无档系数 > 门槛 {min_coeff:g} → 不挂"
    )
    return d


def plan_gap_single_order(
    bids,
    reward_range_min,
    reward_range_max,
    min_size,
    amount_value_table,
    gap_wide_cents,
    gap_mid_cents,
    gap_high_coeff_sum_min,
    rule1_min_coeff,
    rule2_min_coeff,
    rule3_min_coeff,
):
    """网关式单档挂单(v4 用户策略)。explain_gap_single_order 的薄壳:
    返回 (price, shares) 或 None(不挂)。完整判断依据见 explain_gap_single_order。"""
    d = explain_gap_single_order(
        bids,
        reward_range_min,
        reward_range_max,
        min_size,
        amount_value_table,
        gap_wide_cents,
        gap_mid_cents,
        gap_high_coeff_sum_min,
        rule1_min_coeff,
        rule2_min_coeff,
        rule3_min_coeff,
    )
    return (d["price"], d["shares"]) if d["action"] == "place" else None


_GAP_RULE_LABEL = {1: "规则1(宽断层)", 2: "规则2(中断层)", 3: "规则3(密盘)"}


def gap_single_reason(d):
    """把 explain_gap_single_order 决策格式化成中文原因(挂单/跳过通用)。

    跳过 -> 直接用决策里的 skip_reason;挂单 -> 规则级·最大断层·(规则1)高位系数和·
    选中第几档@价 系数>门槛。供 place_buy 记账与预演展示。"""
    if d.get("action") != "place" or d.get("chosen_index") is None:
        return d.get("skip_reason") or "不挂"
    parts = [f"{_GAP_RULE_LABEL.get(d['rule'], '规则?')}·最大断层{d['max_gap']:g}¢"]
    if d["rule"] == 1 and d.get("high_sum") is not None:
        parts.append(f"高位系数和{d['high_sum']:g}(过闸)")
    lv = d["levels"][d["chosen_index"]]
    parts.append(
        f"选中第{d['chosen_index'] + 1}档 @{lv['price']:.4f}"
        f" 系数{lv['coeff']:g} > 门槛{d['min_coeff']:g}"
    )
    return "·".join(parts)


def gap_single_price_basis(d, reward_range_min, reward_range_max):
    """挂单的价格依据/来源串:选中档价 + 系数构成 + 断层分级 + 数据来源。"""
    src = (
        f"奖励区间[{reward_range_min:.4f},{reward_range_max:.4f}];"
        f"来源:CLOB get_orderbook"
    )
    if d.get("action") != "place" or d.get("chosen_index") is None:
        return src
    lv = d["levels"][d["chosen_index"]]
    return (
        f"档价 {lv['price']:.4f};系数 {lv['coeff']:g}=挂量{lv['size']:g}÷(最低份数×金额数值);"
        f"最大断层 {d['max_gap']:g}¢→{_GAP_RULE_LABEL.get(d['rule'], '')};"
        f"选档门槛 {d['min_coeff']:g};" + src
    )


def compute_market_single_orders(
    side_a,
    side_b,
    market_budget_usd,
    max_exposure_shares,
    amount_value_table,
    gap_wide_cents,
    gap_mid_cents,
    gap_high_coeff_sum_min,
    rule1_min_coeff,
    rule2_min_coeff,
    rule3_min_coeff,
):
    """两边共享敞口的网关式单档计划(每边至多一单)。

    对每边调 plan_gap_single_order,再按剩余预算/敞口封顶;封顶后不足 min_size 则放弃
    该边(不挂残档)。a 先于 b 扣预算。
    返回 {"a":[(price,shares)]|[], "b":[...]}。
    """
    out = {"a": [], "b": []}
    spent_usd = 0.0
    spent_shares = 0
    for key, side in (("a", side_a), ("b", side_b)):
        if side is None:
            continue
        plan = plan_gap_single_order(
            side["bids"],
            side["reward_range_min"],
            side["reward_range_max"],
            side["min_size"],
            amount_value_table,
            gap_wide_cents,
            gap_mid_cents,
            gap_high_coeff_sum_min,
            rule1_min_coeff,
            rule2_min_coeff,
            rule3_min_coeff,
        )
        if plan is None:
            continue
        price, shares = plan
        remaining_usd = market_budget_usd - spent_usd
        cap_usd = int(remaining_usd / price) if price > 0 else 0
        cap_shares = max_exposure_shares - spent_shares
        shares = min(shares, cap_usd, cap_shares)
        if shares < side["min_size"]:
            continue
        out[key].append((price, shares))
        spent_usd += price * shares
        spent_shares += shares
    return out


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


def preview_gap_single_market(
    side_a,
    side_b,
    amount_value_table,
    gap_wide_cents,
    gap_mid_cents,
    gap_high_coeff_sum_min,
    rule1_min_coeff,
    rule2_min_coeff,
    rule3_min_coeff,
):
    """两边的网关式单档只读预演:对每边调 explain_gap_single_order,组装展示用 side。

    展示「每个市场怎么按断层单档规则判、判成什么、选中/跳过原因」。不做预算封顶
    (单档=最低份数,是否够预算另由下单时判);held 侧暂停等运行态也不在此体现。
    side_x:{"outcome","token_id","min_size","reward_range_min","reward_range_max",
            "best_bid","best_ask","spread_cents","bids":[{price,size}...]} 或 None。
    返回 {"a":<side|None>,"b":<side|None>};side 含 rule/max_gap/high_sum/gate_passed/
    action/chosen_*/skip_reason/levels(逐档 price/size/coeff/high_side/chosen)。
    """
    out = {}
    for key, side in (("a", side_a), ("b", side_b)):
        if side is None:
            out[key] = None
            continue
        d = explain_gap_single_order(
            side["bids"],
            side["reward_range_min"],
            side["reward_range_max"],
            side["min_size"],
            amount_value_table,
            gap_wide_cents,
            gap_mid_cents,
            gap_high_coeff_sum_min,
            rule1_min_coeff,
            rule2_min_coeff,
            rule3_min_coeff,
        )
        out[key] = {
            "outcome": side.get("outcome", ""),
            "token_id": side.get("token_id", ""),
            "best_bid": side.get("best_bid"),
            "best_ask": side.get("best_ask"),
            "spread_cents": side.get("spread_cents"),
            "reward_range": [side["reward_range_min"], side["reward_range_max"]],
            "rule": d["rule"],
            "rule_label": _GAP_RULE_LABEL.get(d["rule"], "无区间档"),
            "max_gap": d["max_gap"],
            "min_coeff": d["min_coeff"],
            "high_sum": d["high_sum"],
            "gate_passed": d["gate_passed"],
            "action": d["action"],
            "chosen_index": d["chosen_index"],
            "chosen_price": d["price"],
            "skip_reason": d["skip_reason"],
            "levels": d["levels"],
        }
    return out
