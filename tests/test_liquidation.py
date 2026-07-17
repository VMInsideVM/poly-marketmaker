"""tests/test_liquidation.py — 低余额清仓卖出优先级(纯函数)。"""

from engine.liquidation import plan_liquidation


def _c(a, size, reward, loss):
    return {"asset_id": a, "size": size, "daily_reward": reward, "loss": loss}


def test_tier1_low_reward_first_by_loss():
    # 档1=daily_reward<30;档内按 loss 升序
    cands = [
        _c("A", 100, 10, 5.0),  # 档1 loss5
        _c("B", 100, 10, 1.0),  # 档1 loss1
        _c("C", 100, 50, 2.0),  # 高奖励 -> 档3
    ]
    assert plan_liquidation(cands, 30, 20) == ["B", "A", "C"]


def test_tier2_small_position():
    cands = [
        _c("A", 5, 50, 3.0),  # 高奖励但份额<20 -> 档2
        _c("B", 100, 50, 1.0),  # 高奖励大仓 -> 档3
    ]
    assert plan_liquidation(cands, 30, 20) == ["A", "B"]


def test_tier1_beats_tier2():
    cands = [
        _c("A", 5, 50, 9.0),  # 高奖励小仓 -> 档2
        _c("B", 100, 10, 9.0),  # 低奖励大仓 -> 档1(优先)
    ]
    assert plan_liquidation(cands, 30, 20) == ["B", "A"]


def test_tier3_by_loss_profit_first():
    # 档3 按 loss 升序:盈利(负 loss)排最前
    cands = [
        _c("A", 100, 50, 3.0),
        _c("B", 100, 50, -2.0),  # 盈利
        _c("C", 100, 50, 0.0),
    ]
    assert plan_liquidation(cands, 30, 20) == ["B", "C", "A"]


def test_cost_unknown_sorts_to_tier_end():
    # loss=None(成本未知)排所在档末尾
    cands = [
        _c("A", 100, 10, None),  # 档1 但 loss 未知 -> 档1末
        _c("B", 100, 10, 5.0),  # 档1 loss5
    ]
    assert plan_liquidation(cands, 30, 20) == ["B", "A"]


def test_empty():
    assert plan_liquidation([], 30, 20) == []
