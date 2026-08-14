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
