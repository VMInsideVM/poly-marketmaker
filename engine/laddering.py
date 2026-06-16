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
