# 老策略（v1.0.15 厚墙定价）可切换挂单模式 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 v1.0.15 的「找厚墙、挂墙下一档」定价与 min/custom/balance 份数模式移植回来，作为一个与现行 gap_single 并列、可按模板切换的挂单模式。

**Architecture:** 新增 `engine/legacy_wall.py`（定价 + 决策解释 + 两边共享预算的组装）与恢复 `engine/order_sizing.py`（份数），两者都是纯函数。`manager.place_orders` 按模板的 `placement_mode` 二选一走哪套定价，外层的选品、预算封顶、撤改收敛、离场、代理一律复用现有实现。

**Tech Stack:** Python 3.12 + pytest；前端是 Jinja 模板里的原生 JS。

## Global Constraints

- 设计依据：`docs/superpowers/specs/2026-08-14-legacy-wall-placement-mode-design.md`。
- 五个新模板键：`placement_mode`（默认 `"gap_single"`）、`legacy_wall_threshold`（默认 `2000`）、`legacy_cumulative_threshold`（默认 `6000`）、`order_size_mode`（默认 `"min"`）、`order_size_custom_usd`（默认 `0`）。
- 默认 `placement_mode="gap_single"`，升级后不改配置行为必须与升级前**完全一致**。
- 定价的三条路、阈值比较一律**严格大于**（`> 阈值`），等于阈值不算数。
- **C 路（`max_spread >= 3`）找到第一堵墙就定死**：墙的下一档不合格（低于 `min_price` / 出奖励区间 / 没有下一档）就直接不挂，**不继续找第二堵墙**。
- `determine_order_price` 的 `max_spread` 实参按 v1.0.15 原样传 `int(rewards_max_spread)`（`4.5` 截成 `4`）。但**奖励区间**仍用不截断的 float 走 `reward_price_range(midpoint, max_spread_cents)`，不要把 `max_spread × tick_size` 那套带回来。
- 新参数一律加在函数签名**末尾**并带默认值（本仓库多处按位置传参）。
- 中文注释与 UI 字符串保持简体中文。
- **含中文的前端模板（`web/templates/*.html`）由主 agent（控制者）亲自编辑**，subagent 不要碰：历史上 subagent 会把中文写成形近别字并给文件加 BOM。
- **保存 .py 文件会触发自动格式化 hook 重排整个文件**：提交前 `git diff --stat` 检查并还原无关 reflow。
- 工作区有两个与本项目无关的未提交文件（`docs/superpowers/{plans,specs}/2026-07-31-*.md`），任何任务都不要 stage 它们。
- 每个任务结束时全量 `python -m pytest tests/ -q` 必须绿（当前基线 **1021 passed**）。

---

### Task 1: 厚墙定价纯函数

**Files:**
- Create: `engine/legacy_wall.py`
- Test: `tests/test_legacy_wall.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces: `determine_order_price(bids, max_spread, tick_size, reward_range_min, reward_range_max, wall_threshold=2000, cumulative_threshold=6000) -> float | None`

- [ ] **Step 1: 写移植测试（先红）**

新建 `tests/test_legacy_wall.py`。这 13 个用例是从 v1.0.15 的 `tests/test_strategy.py` 原样移植的，断言值一个都不要改：

```python
"""tests/test_legacy_wall.py — v1.0.15 厚墙定价纯函数(不触网)。"""

from engine.legacy_wall import determine_order_price


def _make_bids(pairs):
    return [{"price": str(p), "size": str(s)} for p, s in pairs]


class TestMaxSpread2_TickSize1Cent:
    """max_spread=2, tick_size=0.01:买一厚才挂买二。"""

    def test_bid1_gt_2000_place_at_bid2(self):
        bids = _make_bids([(0.30, 3000), (0.29, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.01,
            reward_range_min=0.28,
            reward_range_max=0.32,
        )
        assert result == 0.29

    def test_bid1_le_2000_skip(self):
        bids = _make_bids([(0.30, 2000), (0.29, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.01,
            reward_range_min=0.28,
            reward_range_max=0.32,
        )
        assert result is None

    def test_result_outside_reward_range_returns_none(self):
        bids = _make_bids([(0.30, 3000), (0.20, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.01,
            reward_range_min=0.28,
            reward_range_max=0.32,
        )
        assert result is None


class TestMaxSpread2_TickSize01Cent:
    """max_spread=2, tick_size=0.001:走累计路径。"""

    def test_cumulative_gt_6000_next_position(self):
        bids = _make_bids(
            [
                (0.300, 2000),
                (0.299, 2000),
                (0.298, 2500),
                (0.297, 500),
            ]
        )
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.001,
            reward_range_min=0.290,
            reward_range_max=0.310,
        )
        assert result == 0.297

    def test_cumulative_never_exceeds_returns_none(self):
        bids = _make_bids([(0.300, 1000), (0.299, 1000)])
        result = determine_order_price(
            bids=bids,
            max_spread=2,
            tick_size=0.001,
            reward_range_min=0.290,
            reward_range_max=0.310,
        )
        assert result is None


class TestMaxSpreadGE3_TickSize1Cent:
    """max_spread>=3, tick_size=0.01:自上而下找第一堵墙。"""

    def test_bid1_gt_2000_place_bid2(self):
        bids = _make_bids([(0.30, 3000), (0.29, 500), (0.28, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=3,
            tick_size=0.01,
            reward_range_min=0.27,
            reward_range_max=0.32,
        )
        assert result == 0.29

    def test_bid1_le_2000_bid2_gt_2000_place_bid3(self):
        bids = _make_bids([(0.30, 1000), (0.29, 3000), (0.28, 500)])
        result = determine_order_price(
            bids=bids,
            max_spread=3,
            tick_size=0.01,
            reward_range_min=0.27,
            reward_range_max=0.32,
        )
        assert result == 0.28

    def test_bid1_bid2_lt_2000_bid3_gt_2000_place_bid4(self):
        bids = _make_bids(
            [
                (0.30, 500),
                (0.29, 500),
                (0.28, 3000),
                (0.27, 100),
            ]
        )
        result = determine_order_price(
            bids=bids,
            max_spread=3,
            tick_size=0.01,
            reward_range_min=0.26,
            reward_range_max=0.32,
        )
        assert result == 0.27

    def test_fallback_keeps_searching(self):
        # 所有档都 <=2000:跳过薄档一路往下扫,扫到超出 max_spread 范围仍没墙 -> 不挂。
        bids = _make_bids(
            [
                (0.30, 500),
                (0.29, 500),
                (0.28, 500),
                (0.27, 500),
            ]
        )
        result = determine_order_price(
            bids=bids,
            max_spread=3,
            tick_size=0.01,
            reward_range_min=0.26,
            reward_range_max=0.32,
        )
        assert result is None


class TestMaxSpreadGE3_TickSize01Cent:
    """max_spread>=3, tick_size=0.001:细 tick 一律走累计路径。"""

    def test_cumulative_gt_6000(self):
        bids = _make_bids(
            [
                (0.300, 3000),
                (0.299, 3500),
                (0.298, 500),
            ]
        )
        result = determine_order_price(
            bids=bids,
            max_spread=3,
            tick_size=0.001,
            reward_range_min=0.290,
            reward_range_max=0.310,
        )
        assert result == 0.298


def test_empty_bids_returns_none():
    assert (
        determine_order_price(
            bids=[],
            max_spread=2,
            tick_size=0.01,
            reward_range_min=0.10,
            reward_range_max=0.90,
        )
        is None
    )
```

再加 4 条本次新增的用例（阈值可配 + C 路语义）：

```python
def test_wall_threshold_is_configurable():
    # 买一 1500:默认阈值 2000 拦下;把阈值降到 1000 就挂买二。
    bids = _make_bids([(0.30, 1500), (0.29, 500)])
    common = dict(
        bids=bids,
        max_spread=2,
        tick_size=0.01,
        reward_range_min=0.28,
        reward_range_max=0.32,
    )
    assert determine_order_price(**common) is None
    assert determine_order_price(**common, wall_threshold=1000) == 0.29


def test_cumulative_threshold_is_configurable():
    # 累计 2000:默认阈值 6000 拦下;降到 1500 就挂下一档。
    bids = _make_bids([(0.300, 2000), (0.299, 500)])
    common = dict(
        bids=bids,
        max_spread=2,
        tick_size=0.001,
        reward_range_min=0.290,
        reward_range_max=0.310,
    )
    assert determine_order_price(**common) is None
    assert determine_order_price(**common, cumulative_threshold=1500) == 0.299


def test_ge3_first_wall_wins_no_second_search():
    # 0.30 是墙(3000),它的下一档 0.29 出了奖励区间[0.28,0.32]吗?没有。
    # 换个构造:墙在 0.30,下一档 0.26 低于 min_price(0.30-3*0.01=0.27) -> 不挂,
    # 且不会继续往下找 0.26 这堵更厚的墙。
    bids = _make_bids([(0.30, 3000), (0.26, 9999), (0.25, 9999)])
    result = determine_order_price(
        bids=bids,
        max_spread=3,
        tick_size=0.01,
        reward_range_min=0.20,
        reward_range_max=0.32,
    )
    assert result is None


def test_ge3_wall_at_last_level_has_no_next():
    # 墙是最后一档,没有下一档可挂 -> 不挂。
    bids = _make_bids([(0.30, 500), (0.29, 3000)])
    result = determine_order_price(
        bids=bids,
        max_spread=3,
        tick_size=0.01,
        reward_range_min=0.26,
        reward_range_max=0.32,
    )
    assert result is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_legacy_wall.py -q`
Expected: 全部 FAIL，`ModuleNotFoundError: No module named 'engine.legacy_wall'`

- [ ] **Step 3: 实现**

新建 `engine/legacy_wall.py`。这是 v1.0.15 `engine/strategy.py` 的原样移植，唯一改动是把两个写死常量提成带默认值的参数：

```python
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
        return _spread2_coarse(
            bids, reward_range_min, reward_range_max, wall_threshold
        )
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_legacy_wall.py -q`
Expected: 17 passed

Run: `python -m pytest tests/ -q`
Expected: 1021 + 17 passed

- [ ] **Step 5: 提交**

```bash
git add engine/legacy_wall.py tests/test_legacy_wall.py
git commit -m "feat(legacy): 移植 v1.0.15 厚墙定价纯函数(两个阈值提为可配参数)"
```

---

### Task 2: 挂单份数纯函数

**Files:**
- Create: `engine/order_sizing.py`
- Test: `tests/test_order_sizing.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces: `compute_order_size(mode, order_price, balance, min_size, custom_usd) -> int | None`

- [ ] **Step 1: 写移植测试（先红）**

新建 `tests/test_order_sizing.py`，从 v1.0.15 原样移植：

```python
"""tests/test_order_sizing.py — 老策略挂单份数(纯函数,不触网)。"""

from engine.order_sizing import compute_order_size


def test_min_mode_returns_min_size_regardless_of_balance_and_price():
    assert compute_order_size("min", 0.50, 1000.0, 10, 0.0) == 10
    assert compute_order_size("min", 0.99, 5.0, 7, 0.0) == 7


def test_balance_mode_floors_balance_over_price():
    assert compute_order_size("balance", 0.50, 1000.0, 10, 0.0) == 2000


def test_balance_mode_floors_inexact_division():
    # 除不尽向下取整(取整向上会在成交时超出余额)。
    assert compute_order_size("balance", 0.30, 1000.0, 10, 0.0) == 3333


def test_balance_mode_skips_when_below_min_size():
    # floor(3.0/0.50)=6 < min_size 10 -> None
    assert compute_order_size("balance", 0.50, 3.0, 10, 0.0) is None


def test_custom_mode_floors_cap_over_price():
    # cap 50 美元,余额充足 -> floor(50/0.50)=100
    assert compute_order_size("custom", 0.50, 1000.0, 10, 50.0) == 100


def test_custom_mode_capped_by_balance_when_cap_exceeds_balance():
    # cap 50 但余额只有 20 -> 用 20:floor(20/0.50)=40
    assert compute_order_size("custom", 0.50, 20.0, 10, 50.0) == 40


def test_custom_mode_skips_when_below_min_size():
    # cap 4 美元 -> floor(4/0.50)=8 < min_size 10 -> None
    assert compute_order_size("custom", 0.50, 1000.0, 10, 4.0) is None


def test_custom_mode_zero_cap_skips():
    # cap 0 -> 预算 0 -> size 0 < min_size -> None(不挂)
    assert compute_order_size("custom", 0.50, 1000.0, 10, 0.0) is None


def test_non_positive_price_returns_none():
    assert compute_order_size("balance", 0.0, 1000.0, 10, 0.0) is None
    assert compute_order_size("custom", -0.10, 1000.0, 10, 50.0) is None
    assert compute_order_size("min", 0.0, 1000.0, 10, 0.0) is None


def test_unknown_mode_falls_back_to_min():
    assert compute_order_size("weird", 0.50, 1000.0, 10, 50.0) == 10
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_order_sizing.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'engine.order_sizing'`

- [ ] **Step 3: 实现**

新建 `engine/order_sizing.py`，从 v1.0.15 原样移植（一字未改）：

```python
"""下单量计算:按模式决定每笔买单挂多少份。

纯函数,不触网。老策略(placement_mode=legacy_wall)下由 place_orders 调用;
gap_single 模式用档位模块的 shares,不走这里。
"""


def compute_order_size(
    mode: str,
    order_price: float,
    balance: float,
    min_size: int,
    custom_usd: float,
) -> int | None:
    """返回应下单的份额(int),或 None 表示跳过该市场。

    - order_price <= 0 -> None(任何模式;价格非正不可能成单)。
    - "min":     返回 min_size。恒满足奖励门槛;能否买得起由 place_orders
                 里已有的 min_cost 门槛在前面拦,这里不再判余额。
    - "custom":  预算 = min(custom_usd, balance),按美元上限下单但不超过余额。
    - "balance": 预算 = balance,全额下单。
    - "custom"/"balance":size = floor(预算 / order_price);若 size < min_size
                 则返回 None(份额不够拿奖励,挂了也吃不到 -> 跳过)。
    - 未知 mode -> 按 "min" 处理(安全兜底)。
    """
    if order_price <= 0:
        return None
    if mode not in ("custom", "balance"):
        # "min" 和任何未知模式都按最小合格份额处理(安全兜底)。
        return min_size
    budget = balance if mode == "balance" else min(custom_usd, balance)
    size = int(budget / order_price)
    if size < min_size:
        return None
    return size
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_order_sizing.py -q`
Expected: 10 passed

Run: `python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add engine/order_sizing.py tests/test_order_sizing.py
git commit -m "feat(legacy): 恢复 v1.0.15 挂单份数模式(min/custom/balance)"
```

---

### Task 3: 决策解释与两边组装

**Files:**
- Modify: `engine/legacy_wall.py`
- Test: `tests/test_legacy_wall.py`

**Interfaces:**
- Consumes: Task 1 的 `determine_order_price`、Task 2 的 `compute_order_size`
- Produces:
  - `explain_legacy_order(bids, max_spread, tick_size, reward_range_min, reward_range_max, min_size, order_size_mode, balance, custom_usd, wall_threshold=2000, cumulative_threshold=6000) -> dict`
    返回 `{"action": "place"|"skip", "rule": "wall"|"cumulative"|None, "threshold": float|None, "hit_index": int|None, "hit_size": float|None, "cumulative": float|None, "price": float|None, "shares": int|None, "levels": [{"price","size","cum"}], "skip_reason": str|None}`
  - `legacy_reason(d) -> str`
  - `legacy_price_basis(d, reward_range_min, reward_range_max) -> str`
  - `compute_market_legacy_orders(side_a, side_b, market_budget_usd, max_exposure_shares, balance, order_size_mode, order_size_custom_usd, wall_threshold=2000, cumulative_threshold=6000) -> {"a": [(price, shares)], "b": [...]}`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_legacy_wall.py`：

```python
from engine.legacy_wall import (
    compute_market_legacy_orders,
    explain_legacy_order,
    legacy_price_basis,
    legacy_reason,
)


def _explain(bids, max_spread=2, tick=0.01, rmin=0.28, rmax=0.32, min_size=20,
             mode="min", balance=1000.0, custom=0.0, wall=2000, cum=6000):
    return explain_legacy_order(
        bids, max_spread, tick, rmin, rmax, min_size, mode, balance, custom,
        wall_threshold=wall, cumulative_threshold=cum,
    )


def test_explain_wall_hit_carries_evidence():
    # 买一 3000 > 2000 -> 命中墙,挂买二 0.29,份数 min 模式 = min_size。
    d = _explain(_make_bids([(0.30, 3000), (0.29, 500)]))
    assert d["action"] == "place"
    assert d["rule"] == "wall"
    assert d["hit_index"] == 0
    assert d["hit_size"] == 3000
    assert d["threshold"] == 2000
    assert d["price"] == 0.29
    assert d["shares"] == 20


def test_explain_skip_reason_names_threshold():
    d = _explain(_make_bids([(0.30, 1500), (0.29, 500)]))
    assert d["action"] == "skip"
    assert "1500" in d["skip_reason"]
    assert "2000" in d["skip_reason"]


def test_explain_cumulative_carries_running_total():
    d = _explain(
        _make_bids([(0.300, 2000), (0.299, 2000), (0.298, 2500), (0.297, 500)]),
        max_spread=2, tick=0.001, rmin=0.290, rmax=0.310, cum=6000,
    )
    assert d["rule"] == "cumulative"
    assert d["hit_index"] == 2
    assert d["cumulative"] == 6500
    assert d["price"] == 0.297


def test_explain_levels_carry_running_cum():
    d = _explain(_make_bids([(0.30, 3000), (0.29, 500)]))
    assert [lv["cum"] for lv in d["levels"]] == [3000, 3500]


def test_explain_size_mode_balance_overrides_min_size():
    # balance 模式:floor(1000/0.29)=3448 份。
    d = _explain(_make_bids([(0.30, 3000), (0.29, 500)]), mode="balance")
    assert d["shares"] == 3448


def test_explain_size_mode_skip_when_below_min_size():
    # custom 上限 4 美元 -> floor(4/0.29)=13 < min_size 20 -> 跳过。
    d = _explain(_make_bids([(0.30, 3000), (0.29, 500)]), mode="custom", custom=4.0)
    assert d["action"] == "skip"
    assert "份数" in d["skip_reason"]


def test_reason_placed_mentions_rule_and_threshold():
    d = _explain(_make_bids([(0.30, 3000), (0.29, 500)]))
    r = legacy_reason(d)
    assert "厚墙" in r
    assert "3000" in r
    assert "2000" in r


def test_reason_skip_uses_skip_reason():
    d = _explain(_make_bids([(0.30, 1500), (0.29, 500)]))
    assert legacy_reason(d) == d["skip_reason"]


def test_price_basis_has_levels_and_source():
    d = _explain(_make_bids([(0.30, 3000), (0.29, 500)]))
    b = legacy_price_basis(d, 0.28, 0.32)
    assert "0.3000" in b
    assert "get_orderbook" in b
    assert "奖励区间" in b


def _side(bids, rmin=0.28, rmax=0.32, min_size=20, tick="0.01", max_spread=2):
    return {
        "bids": bids,
        "reward_range_min": rmin,
        "reward_range_max": rmax,
        "min_size": min_size,
        "tick_size": float(tick),
        "max_spread": max_spread,
        "token_id": "t",
        "outcome": "Yes",
    }


def test_compute_one_side_places_one():
    side = _side(_make_bids([(0.30, 3000), (0.29, 500)]))
    out = compute_market_legacy_orders(side, None, 1000.0, 500, 1000.0, "min", 0.0)
    assert out["a"] == [(0.29, 20)]
    assert out["b"] == []


def test_compute_budget_caps_shares():
    # balance 模式想挂 3448 份,但市场预算只有 10 美元 -> floor(10/0.29)=34 份。
    side = _side(_make_bids([(0.30, 3000), (0.29, 500)]))
    out = compute_market_legacy_orders(side, None, 10.0, 500, 1000.0, "balance", 0.0)
    assert out["a"] == [(0.29, 34)]


def test_compute_drops_side_when_cap_below_min_size():
    # 预算只够 3 份 < min_size 20 -> 放弃该边。
    side = _side(_make_bids([(0.30, 3000), (0.29, 500)]))
    out = compute_market_legacy_orders(side, None, 1.0, 500, 1000.0, "min", 0.0)
    assert out["a"] == []


def test_compute_both_sides_share_budget():
    a = _side(_make_bids([(0.30, 3000), (0.29, 500)]))
    b = _side(_make_bids([(0.30, 3000), (0.29, 500)]))
    # 预算只够 a 边挂满 20 份(0.29*20=5.8),b 边剩 0.2 美元 -> 放弃。
    out = compute_market_legacy_orders(a, b, 6.0, 500, 1000.0, "min", 0.0)
    assert out["a"] == [(0.29, 20)]
    assert out["b"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_legacy_wall.py -q`
Expected: 新用例 FAIL（`ImportError: cannot import name 'explain_legacy_order'`）

- [ ] **Step 3: 实现**

追加到 `engine/legacy_wall.py`：

```python
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
):
    """老策略的完整判断(纯函数,不下单)。

    既驱动 compute_market_legacy_orders,也供记账/预演展示完整依据。字段见
    plan 的 Interfaces 段;levels 逐档带累计值 cum,便于看清累计路径怎么触发。
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
    # 命中档:累计路径=累计首次超阈值那档;厚墙路径=第一个挂量超阈值那档。
    for i, lv in enumerate(levels):
        hit = lv["cum"] > cumulative_threshold if is_fine_tick else lv["size"] > wall_threshold
        if hit:
            d["hit_index"] = i
            d["hit_size"] = lv["size"]
            d["cumulative"] = lv["cum"]
            break

    if price is None:
        if d["hit_index"] is None:
            d["skip_reason"] = (
                f"{_RULE_LABEL[d['rule']]}:无档达到阈值 {d['threshold']:g}"
                f"(最厚 {max(lv['size'] for lv in levels):g}) → 不挂"
            )
        else:
            d["skip_reason"] = (
                f"{_RULE_LABEL[d['rule']]}:第{d['hit_index'] + 1}档挂量"
                f" {d['hit_size']:g} > 阈值 {d['threshold']:g},但下一档不可挂"
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
        hit = (
            f"第{d['hit_index'] + 1}档累计 {d['cumulative']:g} > 阈值 {d['threshold']:g}"
        )
    else:
        hit = f"第{d['hit_index'] + 1}档挂量 {d['hit_size']:g} > 阈值 {d['threshold']:g}"
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
    per = " · ".join(
        f"{lv['price']:.4f}×{lv['size']:g}(累计{lv['cum']:g})"
        + ("[命中]" if i == d.get("hit_index") else "")
        for i, lv in enumerate(levels)
    )
    parts = [f"买单簿(价降序):{per}", f"阈值 {d['threshold']:g}"]
    if d.get("action") == "place":
        parts.append(f"挂下一档 @{d['price']:.4f} × {d['shares']}份")
    else:
        parts.append(d.get("skip_reason") or "不挂")
    parts.append(src)
    return ";".join(parts)


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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_legacy_wall.py -q`
Expected: 全部 passed

Run: `python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add engine/legacy_wall.py tests/test_legacy_wall.py
git commit -m "feat(legacy): 老策略决策解释、中文原因与两边共享预算的组装"
```

---

### Task 4: 五个新模板键

**Files:**
- Modify: `config.py`（`TEMPLATE_DEFAULTS`，在 `"size_tiers": []` 之前插入）
- Test: `tests/test_settings_routes.py`

**Interfaces:**
- Consumes: 无
- Produces: 模板可读出 `placement_mode` / `legacy_wall_threshold` / `legacy_cumulative_threshold` / `order_size_mode` / `order_size_custom_usd`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_settings_routes.py` 末尾。该文件的既有写法是 `client, db = _client_with_db(tmp_path, monkeypatch)` 再 `tid = db.get_default_template_id()`（见 `test_size_tiers_roundtrip_via_template_put`），照抄：

```python
def test_placement_mode_defaults_to_gap_single(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    tid = db.get_default_template_id()
    tmpl = db.get_template(tid)
    assert tmpl["placement_mode"] == "gap_single"
    assert tmpl["legacy_wall_threshold"] == 2000
    assert tmpl["legacy_cumulative_threshold"] == 6000
    assert tmpl["order_size_mode"] == "min"
    assert tmpl["order_size_custom_usd"] == 0


def test_legacy_keys_roundtrip_via_template_put(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    tid = db.get_default_template_id()
    r = client.put(
        f"/api/templates/{tid}",
        json={
            "placement_mode": "legacy_wall",
            "legacy_wall_threshold": 1500,
            "legacy_cumulative_threshold": 5000,
            "order_size_mode": "balance",
            "order_size_custom_usd": 25,
        },
    )
    assert r.status_code == 200
    saved = db.get_template(tid)
    assert saved["placement_mode"] == "legacy_wall"
    assert saved["legacy_wall_threshold"] == 1500
    assert saved["legacy_cumulative_threshold"] == 5000
    assert saved["order_size_mode"] == "balance"
    assert saved["order_size_custom_usd"] == 25
```

不要新造夹具，`_client_with_db` 是该文件已有的。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_settings_routes.py -q -k "placement_mode or legacy_keys"`
Expected: FAIL，`KeyError: 'placement_mode'`

- [ ] **Step 3: 实现**

`config.py` 的 `TEMPLATE_DEFAULTS` 里，在 `"size_tiers": []` 那几行**之前**插入：

```python
    # 挂单模式:gap_single=断层单档(现行);legacy_wall=v1.0.15 老策略「找厚墙、挂墙
    # 下一档」。默认 gap_single,升级零行为变化。模板级,可让部分钱包跑老策略做对比。
    "placement_mode": "gap_single",
    # 老策略的两个阈值(默认值即 v1.0.15 的写死常量,不改即等价原版)。
    "legacy_wall_threshold": 2000,  # 1 美分盘:某档挂量 > 此值算一堵墙
    "legacy_cumulative_threshold": 6000,  # 0.1 美分盘:累计挂量 > 此值即停
    # 老策略的挂单份数:min=最低合格份额 | custom=按美元上限 | balance=全额。
    # gap_single 模式用档位模块的 shares,不看这两个键。
    "order_size_mode": "min",
    "order_size_custom_usd": 0,
```

`TEMPLATE_DEFAULTS` 是白名单，模板读写会自动带上新键，无需改数据库层。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_settings_routes.py -q`
Expected: PASS

Run: `python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add config.py tests/test_settings_routes.py
git commit -m "feat(config): 老策略五个模板键(默认 gap_single,零回归)"
```

---

### Task 5: place_orders 按模式分支

**Files:**
- Modify: `engine/manager.py`（`place_orders` 的 side 构造段、ladders 计算段、记账段；`_maybe_record_gap_skip`）
- Test: `tests/test_place_orders.py`

**Interfaces:**
- Consumes: Task 3 的 `compute_market_legacy_orders` / `explain_legacy_order` / `legacy_reason` / `legacy_price_basis`；Task 4 的五个模板键
- Produces: `placement_mode="legacy_wall"` 时 `place_orders` 走老策略定价与份数；记账文案走 legacy 版本

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_place_orders.py` 末尾。该文件已有 `_gap_template()`（约 403 行）、`_make_worker(template=...)`、`_ob(bids, asks)`、`_elig(...)`、`_actions(db, type)` 夹具，直接复用：

```python
def _legacy_template(**over):
    # 老策略模板:档位模块只用来做选品门控(size=20),shares/系数在老策略下被忽略。
    t = _gap_template()
    t["placement_mode"] = "legacy_wall"
    t["legacy_wall_threshold"] = 2000
    t["legacy_cumulative_threshold"] = 6000
    t["order_size_mode"] = "min"
    t["order_size_custom_usd"] = 0
    t.update(over)
    return t


def test_legacy_mode_places_below_the_wall():
    # 买一 3000 > 2000 -> 挂买二 0.29,份数 min 模式 = min_size 20。
    worker, api, db = _make_worker(template=_legacy_template())
    api.get_orderbook.return_value = _ob([(0.30, 3000), (0.29, 500)], [(0.32, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=20)])
    assert api.place_limit_buy.call_count == 1
    c = api.place_limit_buy.call_args_list[0]
    assert round(c.args[1], 2) == 0.29 and c.args[2] == 20


def test_legacy_mode_thin_wall_skips_and_records():
    # 买一 1500 <= 2000 -> 整个市场不挂,并记一条 gap_skip 说明阈值。
    worker, api, db = _make_worker(template=_legacy_template())
    api.get_orderbook.return_value = _ob([(0.30, 1500), (0.29, 500)], [(0.32, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=20)])
    api.place_limit_buy.assert_not_called()
    skips = _actions(db, "gap_skip")
    assert len(skips) == 1
    assert "2000" in skips[0].kwargs["reason"]


def test_legacy_mode_place_buy_reason_is_legacy_not_gap():
    worker, api, db = _make_worker(template=_legacy_template())
    api.get_orderbook.return_value = _ob([(0.30, 3000), (0.29, 500)], [(0.32, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=20)])
    calls = _actions(db, "place_buy")
    assert len(calls) == 1
    reason = calls[0].kwargs["reason"]
    assert "厚墙" in reason
    assert "断层" not in reason


def test_legacy_wall_threshold_is_read_from_template():
    # 阈值降到 1000 -> 同一副 1500 的盘口这次挂得出来。
    worker, api, db = _make_worker(
        template=_legacy_template(legacy_wall_threshold=1000)
    )
    api.get_orderbook.return_value = _ob([(0.30, 1500), (0.29, 500)], [(0.32, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=20)])
    assert api.place_limit_buy.call_count == 1


def test_legacy_order_size_mode_balance_is_read_from_template():
    # balance 模式:余额由 _make_worker 的 balance 决定,份数 = floor(balance/0.29),
    # 再被 max_exposure_shares / 市场预算封顶。断言比 min_size 大即可证明模式生效。
    worker, api, db = _make_worker(
        balance=100.0, template=_legacy_template(order_size_mode="balance")
    )
    api.get_orderbook.return_value = _ob([(0.30, 3000), (0.29, 500)], [(0.32, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=20)])
    assert api.place_limit_buy.call_count == 1
    assert api.place_limit_buy.call_args_list[0].args[2] > 20


def test_legacy_mode_ignores_tier_shares():
    # 老策略下档位模块只用 size 做选品门控,shares 被忽略:档位配 shares=40,
    # 但 order_size_mode="min" 应该挂 min_size=20 份而不是 40。
    tmpl = _legacy_template()
    tmpl["size_tiers"] = [
        _tier(20, shares=40, av=[{"upper": 1.0, "value": 1}])
    ]
    worker, api, db = _make_worker(template=tmpl)
    api.get_orderbook.return_value = _ob([(0.30, 3000), (0.29, 500)], [(0.32, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=20)])
    assert api.place_limit_buy.call_count == 1
    assert api.place_limit_buy.call_args_list[0].args[2] == 20


def test_gap_single_mode_unaffected_by_legacy_keys():
    # 回归:默认模板(gap_single)行为不变——同一副盘口仍按断层单档判,原因含「规则」。
    worker, api, db = _make_worker(template=_gap_template())
    api.get_orderbook.return_value = _ob([(0.28, 50), (0.27, 40)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=20)])
    calls = _actions(db, "place_buy")
    assert len(calls) == 1
    assert "规则3" in calls[0].kwargs["reason"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_place_orders.py -q -k legacy`
Expected: FAIL（老策略键没被读取，仍走 gap_single，`place_limit_buy` 的价格/份数对不上）

- [ ] **Step 3: 实现**

**(a) side 构造补两个字段。** `place_orders` 里组装 `sides.append({...})` 的那个 dict（约 375-387 行，已含 `tick_size_str` / `max_spread`）再加一个 `tick_size`（float），老策略要用它判粗细 tick：

```python
                        "tick_size": float(ob.get("tick_size", "0.01") or 0.01),
```

`max_spread` 该 dict 里已经有（float，不截断），保持不动。

**(b) 读模板的五个新键。** 在读 `tier` 的那段（约 341-348 行，`gap_high_coeff_sum_min` / `rule1_min_coeff` 那几行附近）之后加：

```python
            placement_mode = str(tmpl.get("placement_mode", "gap_single"))
            legacy_wall_threshold = int(tmpl.get("legacy_wall_threshold", 2000))
            legacy_cum_threshold = int(tmpl.get("legacy_cumulative_threshold", 6000))
            order_size_mode = str(tmpl.get("order_size_mode", "min"))
            order_size_custom_usd = float(tmpl.get("order_size_custom_usd", 0))
```

`tmpl` 是该函数里已有的模板 dict（读 `gap_wide_cents` 等键用的就是它）。若变量名不同，按当前代码里的实际名字来。

**(c) ladders 计算按模式分支。** 现在 `if budget_ok:` 里是 `ladders = compute_market_single_orders(...)` 后面跟着一个给两边算 `gap_explains` 的循环。改成：

```python
            if budget_ok:
                ca = None if side_a["token_id"] in held_assets else side_a
                cb = None if (side_b and side_b["token_id"] in held_assets) else side_b
                if placement_mode == "legacy_wall":
                    ladders = compute_market_legacy_orders(
                        ca,
                        cb,
                        budget,
                        shares_budget,
                        balance,
                        order_size_mode,
                        order_size_custom_usd,
                        wall_threshold=legacy_wall_threshold,
                        cumulative_threshold=legacy_cum_threshold,
                    )
                    for gkey, gside in (("a", ca), ("b", cb)):
                        if gside is not None:
                            gap_explains[gkey] = explain_legacy_order(
                                gside["bids"],
                                int(gside["max_spread"]),
                                gside["tick_size"],
                                gside["reward_range_min"],
                                gside["reward_range_max"],
                                gside["min_size"],
                                order_size_mode,
                                balance,
                                order_size_custom_usd,
                                wall_threshold=legacy_wall_threshold,
                                cumulative_threshold=legacy_cum_threshold,
                            )
                else:
                    ladders = compute_market_single_orders(... 原样不动 ...)
                    for gkey, gside in (("a", ca), ("b", cb)):
                        ... 原样不动 ...
```

注意 `explain_legacy_order` 的 `max_spread` 实参传 `int(gside["max_spread"])`（v1.0.15 的截断行为）。`compute_market_legacy_orders` 内部已经自己做了 `int(side["max_spread"])`，无需在这里额外处理。

**(d) 记账按模式分派。** 挂单成功那段（约 507-522 行）现在写死调 `gap_single_reason` / `gap_single_price_basis`。改成先按模式选一对函数：

```python
                        if gap_d and gap_d.get("action") == "place":
                            if placement_mode == "legacy_wall":
                                r_fn, b_fn = legacy_reason, legacy_price_basis
                            else:
                                r_fn, b_fn = gap_single_reason, gap_single_price_basis
                            self._record_place_buy_tier(
                                mid,
                                side,
                                price,
                                shares,
                                reason=r_fn(gap_d),
                                price_basis=b_fn(
                                    gap_d,
                                    side["reward_range_min"],
                                    side["reward_range_max"],
                                ),
                            )
```

**(e) `_maybe_record_gap_skip` 按模式分派。** 该方法（约 561 行）内部硬编码 `from engine.laddering import gap_single_price_basis`。给它加一个 `placement_mode="gap_single"` 参数（**加在末尾**），并按模式选 price_basis 函数：

```python
    def _maybe_record_gap_skip(self, market_id, side, decision, placement_mode="gap_single"):
        """判成不挂时记一条 gap_skip(按 token 去重:同一原因只记一次,避免每轮下单
        往历史刷同一条)。decision=None/非 skip -> 不记。两种挂单模式共用这一条动作
        类型,只是价格依据的格式化函数不同。"""
        from engine.laddering import gap_single_price_basis
        from engine.legacy_wall import legacy_price_basis

        basis_fn = (
            legacy_price_basis if placement_mode == "legacy_wall" else gap_single_price_basis
        )
```

函数体里 `price_basis=gap_single_price_basis(...)` 改成 `price_basis=basis_fn(...)`，其余不动。调用处（约 528 行）传上 `placement_mode`：

```python
                self._maybe_record_gap_skip(
                    mid, side, gap_explains.get(key), placement_mode
                )
```

**(f) 顶部 import。** `engine/manager.py` 顶部加：

```python
from engine.legacy_wall import (
    compute_market_legacy_orders,
    explain_legacy_order,
    legacy_price_basis,
    legacy_reason,
)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_place_orders.py -q`
Expected: PASS

Run: `python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 验证分支的区分力**

临时把 (c) 里的 `if placement_mode == "legacy_wall":` 改成 `if False:`，确认 `-k legacy` 那批测试变红，然后还原。把两次输出写进报告。

- [ ] **Step 6: 提交**

```bash
git add engine/manager.py tests/test_place_orders.py
git commit -m "feat(legacy): place_orders 按 placement_mode 走老策略定价与记账"
```

---

### Task 6: 配置页与预演页

**Files:**
- Modify: `web/routes.py`（ladder 预览路由按模式分支）
- Modify: `web/templates/config.html`（挂单模式下拉 + 老策略参数块 + 显隐）
- Modify: `web/templates/markets.html`（预演按模式渲染）
- Modify: `web/templates/help.html`（说明两种模式）
- Test: `tests/test_markets_route.py`

**Interfaces:**
- Consumes: Task 3 的 `explain_legacy_order`、Task 4 的模板键
- Produces: 预演接口在老策略下返回 legacy 决策；配置页可切换模式并配老策略参数

**含中文的三个模板文件由主 agent 亲自编辑，subagent 只做 `web/routes.py` 与测试。**

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_markets_route.py`。该文件已有 `_FakeAPI`（买单簿固定为 `0.54/150`、`0.52/300`，asks `0.56`，tick `0.01`）、`_FakeDB`（`get_template_for` 返回带一个 `size=100` 档位的模板）与 `client` fixture。这里继承 `_FakeDB` 换掉模板：

```python
def test_ladder_preview_uses_legacy_when_mode_is_legacy_wall(monkeypatch):
    # 模板切到老策略 -> 预演走厚墙定价而非断层分级。
    # 买单簿 0.54/150、0.52/300:把厚墙阈值降到 100 让 0.54 那档算墙(默认 2000 会
    # 判不挂),目标价 = 墙的下一档 0.52。奖励区间按盘口现算:
    # midpoint=(0.54+0.56)/2=0.55,max_spread=4 -> [0.51,0.59],0.52 在区间内。
    class _LegacyDB(_FakeDB):
        def get_template_for(self, addr):
            t = super().get_template_for(addr)
            t["placement_mode"] = "legacy_wall"
            t["legacy_wall_threshold"] = 100
            t["legacy_cumulative_threshold"] = 6000
            t["order_size_mode"] = "min"
            t["order_size_custom_usd"] = 0
            return t

    routes.app.config["TESTING"] = True
    monkeypatch.setattr(routes, "db", _LegacyDB())
    monkeypatch.setattr(routes, "manager", None)
    monkeypatch.setattr(routes, "_wallet_apis", lambda only=None: {"0xw": _FakeAPI()})
    monkeypatch.setattr(routes, "_enrich_rows", lambda rows, key: None)
    with routes.app.test_client() as c:
        with c.session_transaction() as s:
            s["logged_in"] = True
        r = c.get("/api/markets/c1/ladder?wallet=0xw")
    assert r.status_code == 200
    data = r.get_json()
    assert data["placement_mode"] == "legacy_wall"
    side = data["sides"][0]
    assert side["rule"] == "wall"
    assert side["action"] == "place"
    assert side["chosen_price"] == 0.52
```

既有的 `test_ladder_preview_route` 断言 `data["placement_mode"] == "gap_single"`，它必须继续通过：`_FakeDB` 的模板没有这个键，路由按 `tmpl.get("placement_mode", "gap_single")` 兜底。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_markets_route.py -q -k legacy`
Expected: FAIL。`data["placement_mode"]` 仍是硬编码的 `"gap_single"`，且 side 里没有 `rule == "wall"`（预演仍走 gap_single）。

- [ ] **Step 3: 实现 routes 分支**

`web/routes.py` 的 ladder 路由现在无条件调 `preview_gap_single_market`，并在 `jsonify` 里返回一个**硬编码**的 `"placement_mode": "gap_single"`（约 1330 行，是 v4.0.0 删除 placement_mode 时留下的残骸）。

改动两处：

1. `"placement_mode": "gap_single"` 改成读模板的真实值：`"placement_mode": placement_mode`，其中 `placement_mode = str(tmpl.get("placement_mode", "gap_single"))` 在函数里读模板那段一并取出。前端按这个**顶层**字段分支，**不需要**给每个 side 加 `mode` 字段。
2. 组装 `sides` 那段按模式分支：`legacy_wall` 时对每边调 `explain_legacy_order`，组装 side dict；否则维持现在的 `preview_gap_single_market` 调用不动。

老策略的 side dict 字段（与 gap_single 预演的 side 尽量同名，方便前端复用外框）：`outcome` / `token_id` / `best_bid` / `best_ask` / `spread_cents` / `reward_range` / `rule`（`"wall"` 或 `"cumulative"`）/ `threshold` / `hit_index` / `cumulative` / `action` / `chosen_price`（取决策的 `price`）/ `chosen_shares`（取 `shares`）/ `skip_reason` / `levels`（逐档 `price` / `size` / `cum`）。

`explain_legacy_order` 的实参从模板与该侧盘口取：`max_spread` 传 `int(...)` 截断值，奖励区间用路由里已经算好的 float 值，`balance` 用路由里已有的余额，`min_size` 用该市场的 `rewards_min_size`。

「无匹配档位模块」那条 fallback 分支保持现状即可（它已经返回 `action="skip"` 且前端对缺字段是容错的），不需要为本任务改动。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 提交 routes 部分**

```bash
git add web/routes.py tests/test_markets_route.py
git commit -m "feat(legacy): 预演接口按 placement_mode 返回对应策略的判断"
```

- [ ] **Step 6: 前端三个模板（主 agent 做）**

`config.html`：挂单参数区加一个「挂单模式」下拉（`gap_single` / `legacy_wall`），下方分两块参数，按选中模式显隐。老策略块含：厚墙阈值（1 美分盘）、累计阈值（0.1 美分盘）、挂单份数模式（下拉 min/custom/balance）、自定义金额上限（仅 custom 模式显示）。切模式时用 JS 显隐，参数照常提交（不清空另一模式的值）。

`markets.html`：`renderGapSingle` 之外加 `renderLegacyWall`，按响应的**顶层** `data.placement_mode` 分派（不是 side 上的字段）。老策略表格列：档位 / 价格 / 盘口量 / 累计 / 命中 / 选档。

`help.html`：「断层单档选档规则」一节前加一段说明两种挂单模式的区别与切换位置。

- [ ] **Step 7: 校验前端**

```bash
python -c "
import re
for f in ['markets','config']:
    s=open('web/templates/%s.html'%f,encoding='utf-8').read()
    js='\n'.join(re.findall(r'<script[^>]*>(.*?)</script>',s,re.S))
    open('_check.js','w',encoding='utf-8').write(js)
    print(f,'extracted')
" && node --check _check.js && rm -f _check.js
```

三个模板都要确认 no-BOM、UTF-8 可解码。再跑一次全量 pytest。

- [ ] **Step 8: 提交前端**

```bash
git add web/templates/config.html web/templates/markets.html web/templates/help.html
git commit -m "feat(ui): 配置页切换挂单模式,预演页按模式渲染"
```

---

## 收尾

- [ ] 全量 `python -m pytest tests/ -q` 绿
- [ ] `git diff --stat` 复核没有被格式化 hook 卷进无关文件
- [ ] README.md 与 CLAUDE.md 补一段两种挂单模式的说明（主 agent 做，中文文档）
- [ ] 发版说明要点：新增老策略模式，默认关闭（`placement_mode=gap_single`），行为零变化；切到老策略后档位模块只用 `size` 做选品门控，其余字段忽略；老模式下 Step 3 的悬崖复查仍会跑，想要纯 v1.0.15 行为需把 `cliff_probe_cents` 配成 0
