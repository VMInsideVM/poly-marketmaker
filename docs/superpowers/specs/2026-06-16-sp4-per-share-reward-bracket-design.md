# SP4：单份奖励阈值 + 取档（per-share reward threshold + bracket）设计 / spec

> 日期：2026-06-16
> 状态：待用户评审
> v4 做市策略接入的第四个子项目。父背景见记忆 [[v4-strategy-integration-roadmap]]。

## 零、背景与定位

SP1-SP3 已完成配置解耦、多档挂单、三段式离场。本子项目按 v4 §2/§3 给市场筛选加最后一个维度：**按最低份数取档 + 单份奖励阈值**。这是个小而明确的筛选增量，落在每钱包精筛 `filter_for_template` 里。

**已确认决策**：
1. 单份奖励阈值按 v4 §9 **5 档各自可调**（默认都 0.30）。
2. `最低份额 ≤ 250` 作为**固定闸**（v4 §3，不做成模板参数）；超 250 无取档 → 不做该市场。
3. 现有 `min_reward_usd`（每日 LP 总奖励下限）闸**保留**，与单份奖励闸并存（v4 §3 两条都有）。
4. `取档` 仅服务本节市场筛选（v4 §2：不参与挂单/离场）。

**不在 SP4**：不退役任何东西；不碰挂单/离场；阈值编辑 UI 留 SP6。

## 一、取档（向上取档，更保守）

纯函数 `reward_bracket(min_size) -> int | None`（`engine/scanner.py` 模块级，与 `_parse_end_date` 同处，纯函数单测）：

| 实际最低份数 s | 取档 |
| --- | --- |
| 0 < s ≤ 20 | 20 |
| 20 < s ≤ 50 | 50 |
| 50 < s ≤ 100 | 100 |
| 100 < s ≤ 200 | 200 |
| 200 < s ≤ 250 | 250 |
| s > 250 或 s ≤ 0 | None |

实现：
```python
def reward_bracket(min_size):
    """向上取档(更保守):返回 20/50/100/200/250;超 250 或 <=0 返回 None。"""
    if min_size <= 0:
        return None
    for b in (20, 50, 100, 200, 250):
        if min_size <= b:
            return b
    return None
```

## 二、单份奖励 + 阈值过滤

**单份奖励** = 每日 LP 奖励 ÷ 最低份数 = `market_reward / rewards_min_size`（`market_reward` 是 `fetch_candidates` 补的精确每市场奖励，回退 `total_rate`）。

在 `filter_for_template` 的**按市场** gating 段（现有 `min_reward_usd` 总额闸 + 冷却闸之后、token 循环之前）加两道闸：

```python
min_size = int(market.get("rewards_min_size", 0))
# v4 §3:最低份额 0 < ≤ 250;超档无 bracket -> 不做
if not (0 < min_size <= 250):
    continue
# v4 §3:单份奖励 >= 该取档阈值 -> 通过(向上取档)
bracket = reward_bracket(min_size)
per_share = market_reward / min_size
if per_share < float(thresholds.get(str(bracket), 0.30)):
    continue
```

其中 `thresholds = template.get("per_share_reward_thresholds", {})`（键为字符串，JSON dict）。`bracket` 在 `0 < min_size ≤ 250` 闸之后必为 20/50/100/200/250 之一（非 None），故 `str(bracket)` 安全。

> `min_size` 当前在 token 循环前已读取一次（用于 eligible 条目）；本闸放在该读取处即可，不重复读。

## 三、模板键

新增策略级键 `per_share_reward_thresholds`（`TEMPLATE_DEFAULTS`，JSON dict，键为字符串）：

```json
{"20": 0.30, "50": 0.30, "100": 0.30, "200": 0.30, "250": 0.30}
```

五档默认都 0.30；用户可各档单独调（如高份额档放宽、低份额档收紧，v4 §9）。读取时缺某档键 → 回退 0.30（`thresholds.get(str(bracket), 0.30)`）。

## 四、测试

- **`reward_bracket` 纯函数**：`20→20`、`21→50`、`50→50`、`100→100`、`200→200`、`250→250`、`251→None`、`0→None`、`-5→None`。
- **`filter_for_template`（test_scanner.py）**：
  - 单份奖励 < 该档阈值 → 剔除；≥ → 通过（用 `market_reward`/`rewards_min_size` 构造）。
  - `rewards_min_size > 250` → 剔除；`= 0` → 剔除。
  - 分档阈值生效：同一池，把某档阈值调高使该档市场被剔，另一档默认让其市场通过（证明按档分设而非单值）。
  - 现有 `min_reward_usd` 总额闸仍生效（不被单份奖励闸取代）。

## 五、验收 checkpoint

1. 单份奖励 ≥ 取档阈值的市场通过、低于的剔除。
2. 最低份数 > 250 或 ≤ 0 的市场不进 eligible。
3. 五档阈值分别可调：调高某档剔除该档市场、其它档不受影响。
4. `min_reward_usd` 总额闸与单份奖励闸并存。
5. `pytest` 全绿。

## 六、范围之外

SP5 三档节奏 + 观察名单 + 成交后单侧暂停 + 撤改收敛 · SP6 模板 UI（含 5 档阈值编辑、退役死字段收口）。
