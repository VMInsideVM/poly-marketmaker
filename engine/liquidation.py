"""engine/liquidation.py — 低余额清仓的卖出优先级(纯函数,无 IO)。

余额过低时按优先级逐笔市价卖持仓腾现金:档1=低奖励市场、档2=小仓、档3=其余,每档内
按亏损从小到大(先卖最不心疼的)。「停手目标」由编排器按估算余额逐笔控制,本函数只定顺序。
"""

import math


def plan_liquidation(candidates, low_reward_threshold, small_shares_threshold):
    """返回按优先级排序的 asset_id 列表:档1(低奖励市场)→档2(小仓)→档3(其余),
    每档内按 loss 升序(loss=None 视为 +∞ 排档末)。

    candidates: [{asset_id, size, daily_reward(或 None), loss(或 None)}]。
    """
    tier1, tier2, tier3 = [], [], []
    for c in candidates:
        reward = c.get("daily_reward")
        size = float(c.get("size", 0) or 0)
        if reward is not None and reward < low_reward_threshold:
            tier1.append(c)
        elif size < small_shares_threshold:
            tier2.append(c)
        else:
            tier3.append(c)

    def _key(c):
        loss = c.get("loss")
        return math.inf if loss is None else loss

    ordered = []
    for tier in (tier1, tier2, tier3):
        ordered += [c["asset_id"] for c in sorted(tier, key=_key)]
    return ordered
