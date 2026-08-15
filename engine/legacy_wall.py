"""engine/legacy_wall.py — v1.0.15 老策略「找厚墙、挂墙下一档」定价(纯函数,不触网)。

与现行 gap_single 并列的另一套挂单定价:gap_single 看相对系数与价差断层,这套看
买单簿上的绝对挂量,找到一堵够厚的墙就挂在它下面一档,让墙替自己挡住砸盘。
由模板的 placement_mode 选用哪套。
"""


def determine_order_price(
    bids: list[dict],
    max_spread: float,
    tick_size: float,
    reward_range_min: float,
    reward_range_max: float,
    wall_threshold: int = 2000,
    cumulative_threshold: int = 6000,
) -> float | None:
    """按 tick 粗细与 max_spread 分三条路选一个买入价;选不出返回 None。

    bids: 按价降序的买档 [{price, size}, ...]。
    max_spread: 奖励区间宽度(美分)。调用方按 v1.0.15 原样传 int 截断值。
    wall_threshold: 1 美分盘的厚墙阈值(严格大于才算墙)。
    cumulative_threshold: 0.1 美分盘的累计挂量阈值(严格大于才停)。
    """
    if not bids:
        return None

    is_fine_tick = tick_size < 0.01  # 0.1 美分盘

    if is_fine_tick:
        return _cumulative(
            bids, reward_range_min, reward_range_max, cumulative_threshold
        )
    if max_spread == 2:
        return _spread2_coarse(bids, reward_range_min, reward_range_max, wall_threshold)
    return _spread_ge3_coarse(
        bids,
        max_spread,
        tick_size,
        reward_range_min,
        reward_range_max,
        wall_threshold,
    )


def _cumulative(
    bids: list[dict],
    reward_range_min: float,
    reward_range_max: float,
    threshold: int,
) -> float | None:
    """细 tick:自上而下累加挂量,累计首次 > threshold 时挂下一档。"""
    cumulative = 0
    for i, bid in enumerate(bids):
        cumulative += int(float(bid["size"]))
        if cumulative > threshold:
            if i + 1 >= len(bids):
                return None  # 没有下一档
            target = float(bids[i + 1]["price"])
            if reward_range_min <= target <= reward_range_max:
                return target
            return None
    return None


def _spread2_coarse(
    bids: list[dict],
    reward_range_min: float,
    reward_range_max: float,
    wall_threshold: int,
) -> float | None:
    """max_spread=2 粗 tick:买一够厚才挂买二,否则整个市场不挂。"""
    if int(float(bids[0]["size"])) <= wall_threshold:
        return None
    if len(bids) < 2:
        return None
    target = float(bids[1]["price"])
    if reward_range_min <= target <= reward_range_max:
        return target
    return None


def _spread_ge3_coarse(
    bids: list[dict],
    max_spread: float,
    tick_size: float,
    reward_range_min: float,
    reward_range_max: float,
    wall_threshold: int,
) -> float | None:
    """max_spread>=3 粗 tick:自上而下找第一堵墙,挂它的下一档。

    找到第一堵墙就定死:它的下一档不合格(低于 min_price / 出奖励区间 / 不存在)就
    直接不挂,**不会继续往下找第二堵墙**。跳过的只是「不够厚」的档。
    """
    best_bid_price = float(bids[0]["price"])
    min_price = best_bid_price - max_spread * tick_size

    for i, bid in enumerate(bids):
        if float(bid["price"]) < min_price:
            break  # 超出可挂范围,停止扫描
        if int(float(bid["size"])) > wall_threshold:
            if i + 1 < len(bids):
                target = float(bids[i + 1]["price"])
                if (
                    target >= min_price
                    and reward_range_min <= target <= reward_range_max
                ):
                    return target
            return None  # 第一堵墙的下一档不合格 -> 不挂,不再找第二堵
    return None


from engine.laddering import has_cliff_below
from engine.order_sizing import compute_order_size

_RULE_LABEL = {"wall": "厚墙", "cumulative": "累计厚度"}


def explain_legacy_order(
    bids,
    max_spread,
    tick_size,
    reward_range_min,
    reward_range_max,
    min_size,
    order_size_mode,
    balance,
    custom_usd,
    wall_threshold=2000,
    cumulative_threshold=6000,
    cliff_probe_cents=0,
):
    """老策略的完整判断(纯函数,不下单)。

    既驱动 compute_market_legacy_orders,也供记账/预演展示完整依据。字段见
    plan 的 Interfaces 段;levels 逐档带累计值 cum,便于看清累计路径怎么触发。
    cliff_probe_cents>0 时,先按 has_cliff_below(与 gap_single/监控 Step 3 的
    cliff_cancel 共用同一个纯函数)否决奖励区间下方无支撑的悬崖市场;命中时
    d["cliff"]=True、不再进入定价。
    """
    d = {
        "action": "skip",
        "rule": None,
        "threshold": None,
        "hit_index": None,
        "hit_size": None,
        "cumulative": None,
        "price": None,
        "shares": None,
        "levels": [],
        "skip_reason": None,
        "cliff": False,
    }
    if not bids or min_size <= 0:
        d["skip_reason"] = "无买单簿或最低份数<=0"
        return d

    levels, running = [], 0
    for b in bids:
        size = float(b["size"])
        running += int(size)
        levels.append({"price": float(b["price"]), "size": size, "cum": running})
    d["levels"] = levels

    if has_cliff_below(bids, reward_range_min, cliff_probe_cents):
        d["cliff"] = True
        d["skip_reason"] = (
            f"奖励区间下方 {cliff_probe_cents:g}¢ 内无买档支撑(悬崖) → 不挂"
        )
        return d

    is_fine_tick = tick_size < 0.01
    d["rule"] = "cumulative" if is_fine_tick else "wall"
    d["threshold"] = cumulative_threshold if is_fine_tick else wall_threshold

    price = determine_order_price(
        bids,
        max_spread,
        tick_size,
        reward_range_min,
        reward_range_max,
        wall_threshold=wall_threshold,
        cumulative_threshold=cumulative_threshold,
    )
    # 命中档必须按「定价函数实际扫过的范围」来找,否则会把定价根本没看过的深档
    # 标成命中,给出一个从未发生的跳过理由(审计/预演都会被误导)。扫描边界与
    # determine_order_price 的三条路一一对应:
    # - 累计路径(is_fine_tick):全簿累加,无边界。
    # - max_spread==2(对应 _spread2_coarse):只看买一,根本不考虑更深的档。
    # - max_spread>=3(对应 _spread_ge3_coarse):价格 < min_price 即停止扫描。
    if is_fine_tick:
        scanned = levels
    elif max_spread == 2:
        scanned = levels[:1]
    else:
        min_price = levels[0]["price"] - max_spread * tick_size
        scanned = []
        for lv in levels:
            if lv["price"] < min_price:
                break
            scanned.append(lv)

    for i, lv in enumerate(scanned):
        # 厚墙分支必须按 int 截断后比较,与 _spread2_coarse/_spread_ge3_coarse
        # 的真实定价口径(int(float(bid["size"])) > wall_threshold)对齐 —— 否则
        # 挂量为小数(Polymarket 挂量确实有小数,如 2000.5)时,这里未截断的浮点比较
        # 会把定价函数根本没判定为墙的档报成命中,连带 hit_index/reason 全部指向
        # 一个从未挂过的价位。累计分支的 cum 本来就是 int 累加,已经对齐,不用动。
        hit = (
            lv["cum"] > cumulative_threshold
            if is_fine_tick
            else int(lv["size"]) > wall_threshold
        )
        if hit:
            d["hit_index"] = i
            d["hit_size"] = lv["size"]
            d["cumulative"] = lv["cum"]
            break

    if price is None:
        if d["hit_index"] is None:
            if is_fine_tick:
                # 累计路径该比的是累计总量,不是单档最厚(scanned 范围内最后一档
                # 的 cum 就是全部扫描范围的累计总量)。
                total = scanned[-1]["cum"]
                d["skip_reason"] = (
                    f"{_RULE_LABEL[d['rule']]}:无档达到阈值 {d['threshold']:g}"
                    f"(扫描范围内累计 {total:g}) → 不挂"
                )
            else:
                # 挂量按 int 显示,与判定口径(int(size) 比阈值)一致:否则盘口 236.24
                # 会与「累计236」并排出现,看的人相加对不上。
                d["skip_reason"] = (
                    f"{_RULE_LABEL[d['rule']]}:无档达到阈值 {d['threshold']:g}"
                    f"(扫描范围内最厚 {max(int(lv['size']) for lv in scanned):g}) → 不挂"
                )
        else:
            if is_fine_tick:
                d["skip_reason"] = (
                    f"{_RULE_LABEL[d['rule']]}:第{d['hit_index'] + 1}档累计"
                    f" {d['cumulative']:g} > 阈值 {d['threshold']:g},但下一档不可挂"
                    f"(无下一档/超出可挂范围/出奖励区间) → 不挂"
                )
            else:
                d["skip_reason"] = (
                    f"{_RULE_LABEL[d['rule']]}:第{d['hit_index'] + 1}档挂量"
                    f" {int(d['hit_size']):g} > 阈值 {d['threshold']:g},但下一档不可挂"
                    f"(无下一档/超出可挂范围/出奖励区间) → 不挂"
                )
        return d

    shares = compute_order_size(
        order_size_mode, price, balance, int(min_size), custom_usd
    )
    if shares is None:
        d["skip_reason"] = (
            f"{_RULE_LABEL[d['rule']]}:选中 @{price:.4f},但按份数模式"
            f" {order_size_mode} 算出的份数不足最低份数 {int(min_size)} → 不挂"
        )
        return d

    d["action"] = "place"
    d["price"] = price
    d["shares"] = int(shares)
    return d


def legacy_reason(d):
    """把老策略决策格式化成中文原因(挂单/跳过通用)。"""
    if d.get("action") != "place":
        return d.get("skip_reason") or "不挂"
    label = _RULE_LABEL.get(d["rule"], "规则?")
    if d["rule"] == "cumulative":
        hit = f"第{d['hit_index'] + 1}档累计 {d['cumulative']:g} > 阈值 {d['threshold']:g}"
    else:
        hit = (
            f"第{d['hit_index'] + 1}档挂量 {d['hit_size']:g} > 阈值 {d['threshold']:g}"
        )
    return f"{label}·{hit}·挂下一档 @{d['price']:.4f} × {d['shares']}份"


def legacy_price_basis(d, reward_range_min, reward_range_max):
    """价格依据/来源串:逐档挂量与累计 + 命中档 + 目标价 + 数据来源。"""
    src = (
        f"奖励区间[{reward_range_min:.4f},{reward_range_max:.4f}];"
        f"来源:CLOB get_orderbook"
    )
    levels = d.get("levels") or []
    if not levels:
        return f"无可评估买档;{src}"
    hit = d.get("hit_index")
    if d.get("action") == "place":
        # 挂单成功:只写命中的那堵墙与实际挂的那一档。全簿展开留给跳过分支——
        # 挂单是每轮高频动作,把几十档买单簿写进 actions 表会迅速撑爆历史
        # (实测 40 档要 920 字符,而 gap_single 挂单时只写 102)。
        hv = levels[hit] if hit is not None and hit < len(levels) else None
        wall = (
            f"命中第{hit + 1}档 {hv['price']:.4f}×{int(hv['size']):g}"
            f"(累计{hv['cum']:g})"
            if hv
            else "无命中档"
        )
        return ";".join(
            [
                wall,
                f"阈值 {d['threshold']:g}",
                f"挂下一档 @{d['price']:.4f} × {d['shares']}份",
                src,
            ]
        )
    # 跳过:逐档展开全簿,这是复盘不挂原因的唯一依据。挂量按 int 显示,与判定
    # 口径(int(size) 比阈值、累计按 int 累加)一致,免得逐档相加对不上累计。
    per = " · ".join(
        f"{lv['price']:.4f}×{int(lv['size']):g}(累计{lv['cum']:g})"
        + ("[命中]" if i == hit else "")
        for i, lv in enumerate(levels)
    )
    thr = f"阈值 {d['threshold']:g}" if d.get("threshold") is not None else "阈值 —"
    return ";".join([f"买单簿(价降序):{per}", thr, d.get("skip_reason") or "不挂", src])


def compute_market_legacy_orders(
    side_a,
    side_b,
    market_budget_usd,
    max_exposure_shares,
    balance,
    order_size_mode,
    order_size_custom_usd,
    wall_threshold=2000,
    cumulative_threshold=6000,
    cliff_probe_cents=0,
):
    """两边共享敞口的老策略计划(每边至多一单)。

    与 gap_single 的 compute_market_single_orders 同形状:对每边算价与份数,再按
    剩余预算/敞口封顶;封顶后不足 min_size 则放弃该边(不挂残档)。a 先于 b 扣预算。
    """
    out = {"a": [], "b": []}
    spent_usd = 0.0
    spent_shares = 0
    for key, side in (("a", side_a), ("b", side_b)):
        if side is None:
            continue
        d = explain_legacy_order(
            side["bids"],
            # int 截断是 v1.0.15 的原版行为(4.5 -> 4),刻意保留以求逐字一致。
            # 注意奖励区间不受影响:它由调用方用不截断的 float 算好后传进来。
            int(side["max_spread"]),
            side["tick_size"],
            side["reward_range_min"],
            side["reward_range_max"],
            side["min_size"],
            order_size_mode,
            balance,
            order_size_custom_usd,
            wall_threshold=wall_threshold,
            cumulative_threshold=cumulative_threshold,
            cliff_probe_cents=cliff_probe_cents,
        )
        if d["action"] != "place":
            continue
        price, placed = d["price"], d["shares"]
        remaining_usd = market_budget_usd - spent_usd
        cap_usd = int(remaining_usd / price) if price > 0 else 0
        cap_shares = max_exposure_shares - spent_shares
        placed = min(placed, cap_usd, cap_shares)
        if placed < side["min_size"]:
            continue
        out[key].append((price, placed))
        spent_usd += price * placed
        spent_shares += placed
    return out
