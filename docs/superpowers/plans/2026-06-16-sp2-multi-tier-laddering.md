# SP2 多档挂单 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把做市决策从「单边单单」换成 v4 §5 多档挂单:一侧最多 K 档,每档按「累加厚度→份额规则表」(五种动作)定份额,整市场敞口 250U/500share 两边共享,§8 <10¢ 强制双边。

**Architecture:** 新增纯函数引擎 `engine/laddering.py`(build_ladder / resolve_tier_share / compute_market_ladders / apply_double_sided_floor),像旧 `determine_order_price` 一样全单测覆盖。`filter_for_template` 退化为只 gate(不定价)。`place_orders` 重写为按市场分组、下单时读 live 余额定份额、敞口/并发上限/<10¢/已挂幂等。退役 `determine_order_price`/`compute_order_size`/扁平挂单上限。

**Tech Stack:** Python 3.12 / SQLite / pytest(临时库、MagicMock 桩 API、纯逻辑不触网) / Polymarket CLOB。

**关键执行顺序(保持每次提交测试绿):** 先 config(T1,旧 place_orders 用 `.get` 默认值不受影响)→ 纯 laddering 引擎(T2-T5,新文件、附加)→ 轻量化 filter + 退役 scan shim(T6,旧 place_orders 自行重算、容忍字段缺失)→ place_orders 多档重写(T7)→ 最后退役老算法(T8,此时已无人引用)。

---

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `config.py` | 模板默认参数:加多档键、删退役键 | 修改 |
| `engine/laddering.py` | 多档纯函数引擎(核心 IP) | **新建** |
| `engine/scanner.py` | `filter_for_template` 只 gate;删 `scan` shim | 修改 |
| `engine/manager.py` | `place_orders` 多档重写;删 order_sizing import | 修改 |
| `engine/strategy.py` | 删 `determine_order_price`+`_strategy_*`;留 `reward_price_range` | 修改 |
| `engine/order_sizing.py` | `compute_order_size` 退役 | 删除 |
| `tests/test_laddering.py` | 多档纯函数单测 | **新建** |
| `tests/test_scanner.py` | filter 只 gate 的断言;删 scan shim 旧用例 | 修改 |
| `tests/test_place_orders.py` | 多档下单单测 | 修改 |
| `tests/test_strategy.py` | 删 determine_order_price 用例 | 修改 |

---

## Task 1: config — 多档模板键

**Files:** Modify `config.py`(`TEMPLATE_DEFAULTS`). Test: `tests/test_database.py`.

- [ ] **Step 1: 写失败测试**

在 `tests/test_database.py` 加:

```python
def test_template_defaults_has_multitier_keys():
    from config import TEMPLATE_DEFAULTS
    assert TEMPLATE_DEFAULTS["tiers_k"] == 6
    assert len(TEMPLATE_DEFAULTS["tier_rules"]) == 6
    # 默认每档单区间 [0,inf) -> min_size
    assert TEMPLATE_DEFAULTS["tier_rules"][0] == [
        {"upper": None, "action": {"type": "min_size"}}
    ]
    assert TEMPLATE_DEFAULTS["max_exposure_usd"] == 250
    assert TEMPLATE_DEFAULTS["max_exposure_shares"] == 500
    assert TEMPLATE_DEFAULTS["max_concurrent_markets"] == 10
    assert TEMPLATE_DEFAULTS["min_price_double_cents"] == 10
    # 退役键已移除
    assert "order_size_mode" not in TEMPLATE_DEFAULTS
    assert "order_size_custom_usd" not in TEMPLATE_DEFAULTS
    assert "max_buy_orders_per_wallet" not in TEMPLATE_DEFAULTS
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_database.py::test_template_defaults_has_multitier_keys -v`
Expected: FAIL（KeyError / 退役键仍在）

- [ ] **Step 3: 改 config.py**

在 `config.py` 的 `TEMPLATE_DEFAULTS` 中:**删除** `order_size_mode`、`order_size_custom_usd`、`max_buy_orders_per_wallet` 三行;**新增**多档键。改后 `TEMPLATE_DEFAULTS` 为:

```python
TEMPLATE_DEFAULTS = {
    "min_reward_usd": 100.0,
    "max_spread_cents": 3.0,
    "min_price_cents": 10.0,
    "max_price_cents": 50.0,
    "min_settlement_days": 4,
    "stop_loss_pct": 15.0,
    "excluded_categories": ["sports", "esports", "weather"],
    # 多档挂单(SP2)
    "tiers_k": 6,
    "tier_rules": [
        [{"upper": None, "action": {"type": "min_size"}}] for _ in range(6)
    ],
    "max_exposure_usd": 250,
    "max_exposure_shares": 500,
    "max_concurrent_markets": 10,
    "min_price_double_cents": 10,
}
```

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_database.py::test_template_defaults_has_multitier_keys -v`
Expected: PASS

- [ ] **Step 5: 全库 + place_orders 测试无回归**

Run: `python -m pytest tests/test_database.py tests/test_place_orders.py -q`
Expected: ALL PASS（旧 place_orders 用 `tmpl.get("order_size_mode","min")` 等默认值,不受退役键影响）

- [ ] **Step 6: Commit**

```bash
git add config.py tests/test_database.py
git commit -m "feat(config): 多档挂单模板键(tier_rules/敞口/并发上限),退役旧份额/挂单上限键"
```

---

## Task 2: laddering.build_ladder

**Files:** Create `engine/laddering.py`. Test: `tests/test_laddering.py`.

- [ ] **Step 1: 写失败测试**

新建 `tests/test_laddering.py`:

```python
"""tests/test_laddering.py — 多档挂单纯函数(不触网)。"""

from engine.laddering import build_ladder


def _b(price, size):
    return {"price": str(price), "size": str(size)}


def test_ladder_picks_valid_levels_highest_first():
    # min_size=100; 奖励区间 [0.20, 0.40]
    bids = [_b(0.35, 300), _b(0.30, 150), _b(0.25, 50), _b(0.22, 200)]
    # 0.25 厚度 50/100=0.5 <1 -> 跳过;其余厚度>=1 且在区间
    ladder = build_ladder(bids, 0.20, 0.40, 100, 6)
    assert [r["price"] for r in ladder] == [0.35, 0.30, 0.22]


def test_ladder_cumulative_thickness_sums_all_levels_above():
    bids = [_b(0.35, 300), _b(0.30, 150), _b(0.25, 50), _b(0.22, 200)]
    ladder = build_ladder(bids, 0.20, 0.40, 100, 6)
    by_price = {r["price"]: r["cumulative_thickness"] for r in ladder}
    # 累加厚度含所有更高价位(含被跳过的薄档)
    assert by_price[0.35] == 3.0            # 300/100
    assert by_price[0.30] == 3.0 + 1.5      # +150/100
    assert by_price[0.22] == 4.5 + 0.5 + 2.0  # +50/100(跳过档也累加) +200/100


def test_ladder_skips_out_of_reward_range():
    bids = [_b(0.45, 300), _b(0.30, 300)]  # 0.45 超出 rmax
    ladder = build_ladder(bids, 0.20, 0.40, 100, 6)
    assert [r["price"] for r in ladder] == [0.30]


def test_ladder_caps_at_k():
    bids = [_b(0.39 - i * 0.01, 300) for i in range(10)]  # 全合格
    ladder = build_ladder(bids, 0.0, 1.0, 100, 6)
    assert len(ladder) == 6


def test_ladder_empty_when_no_bids_or_min_size_zero():
    assert build_ladder([], 0.0, 1.0, 100, 6) == []
    assert build_ladder([_b(0.3, 300)], 0.0, 1.0, 0, 6) == []
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_laddering.py::test_ladder_picks_valid_levels_highest_first -v`
Expected: FAIL（`engine.laddering` 不存在）

- [ ] **Step 3: 实现**

新建 `engine/laddering.py`:

```python
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
```

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_laddering.py -v`
Expected: PASS（5 个）

- [ ] **Step 5: Commit**

```bash
git add engine/laddering.py tests/test_laddering.py
git commit -m "feat(laddering): build_ladder 构建单边档价梯+累加厚度"
```

---

## Task 3: laddering.resolve_tier_share（五种份额动作）

**Files:** Modify `engine/laddering.py`. Test: `tests/test_laddering.py`.

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_laddering.py`:

```python
from engine.laddering import resolve_tier_share


def _rule(*intervals):
    return list(intervals)


def test_share_min_size():
    rule = _rule({"upper": None, "action": {"type": "min_size"}})
    assert resolve_tier_share(2.0, rule, 0.30, 100, 1000) == 100


def test_share_fixed_shares():
    rule = _rule({"upper": None, "action": {"type": "fixed_shares", "shares": 50}})
    assert resolve_tier_share(2.0, rule, 0.30, 100, 1000) == 50


def test_share_fixed_amount_floor_and_min_bump():
    rule = _rule({"upper": None, "action": {"type": "fixed_amount", "usd": 30}})
    # 30/0.30 = 100 份
    assert resolve_tier_share(2.0, rule, 0.30, 100, 1000) == 100
    # 9/0.30 = 30 份 < min_size 100 -> 上调到 100
    rule2 = _rule({"upper": None, "action": {"type": "fixed_amount", "usd": 9}})
    assert resolve_tier_share(2.0, rule2, 0.30, 100, 1000) == 100


def test_share_wallet_total_uses_remaining_budget():
    rule = _rule({"upper": None, "action": {"type": "wallet_total"}})
    # 60 剩余预算 / 0.30 = 200 份
    assert resolve_tier_share(2.0, rule, 0.30, 100, 60) == 200


def test_share_skip():
    rule = _rule({"upper": None, "action": {"type": "skip"}})
    assert resolve_tier_share(2.0, rule, 0.30, 100, 1000) == 0


def test_interval_selection_half_open_ascending():
    # [0,5)->50份;[5,inf)->min_size
    rule = _rule(
        {"upper": 5.0, "action": {"type": "fixed_shares", "shares": 50}},
        {"upper": None, "action": {"type": "min_size"}},
    )
    assert resolve_tier_share(4.9, rule, 0.30, 100, 1000) == 50   # <5
    assert resolve_tier_share(5.0, rule, 0.30, 100, 1000) == 100  # 边界归下一区间
    assert resolve_tier_share(9.0, rule, 0.30, 100, 1000) == 100
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_laddering.py::test_share_min_size -v`
Expected: FAIL（`resolve_tier_share` 不存在）

- [ ] **Step 3: 实现**

在 `engine/laddering.py` 追加:

```python
def _interval_action(tier_rule, ct):
    """半开升序区间 [前一上界, upper):返回包含 ct 的区间动作。"""
    for interval in tier_rule:
        upper = interval.get("upper")
        if upper is None or ct < upper:
            return interval.get("action", {"type": "skip"})
    return {"type": "skip"}


def resolve_tier_share(cumulative_thickness, tier_rule, price, min_size, remaining_budget_usd):
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
    # skip / 未知
    return 0
```

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_laddering.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/laddering.py tests/test_laddering.py
git commit -m "feat(laddering): resolve_tier_share 五种份额动作+半开区间命中"
```

---

## Task 4: laddering.compute_market_ladders（两边共享敞口）

**Files:** Modify `engine/laddering.py`. Test: `tests/test_laddering.py`.

**说明:** 两边各自 build_ladder,按「档序升序、同档先 a(YES) 后 b(NO)」遍历,共享 `market_budget_usd`/`max_exposure_shares`,逐档算份额并按敞口封顶;`wallet_total` 用当前剩余预算。

- [ ] **Step 1: 写失败测试**

追加:

```python
from engine.laddering import compute_market_ladders


def _side(bids, min_size=100, rmin=0.0, rmax=1.0):
    return {"bids": bids, "reward_range_min": rmin, "reward_range_max": rmax,
            "min_size": min_size}


def test_market_ladders_min_size_both_sides():
    rules = [[{"upper": None, "action": {"type": "min_size"}}] for _ in range(6)]
    a = _side([_b(0.30, 300), _b(0.29, 300)])
    b = _side([_b(0.30, 300)])
    out = compute_market_ladders(a, b, rules, 1000.0, 10000)
    assert out["a"] == [(0.30, 100), (0.29, 100)]
    assert out["b"] == [(0.30, 100)]


def test_market_ladders_usd_budget_caps_across_sides():
    rules = [[{"upper": None, "action": {"type": "min_size"}}] for _ in range(6)]
    a = _side([_b(0.50, 300)])   # 档1-a: 100 份 * 0.50 = 50U
    b = _side([_b(0.50, 300)])   # 档1-b: 想 100 份,但预算只剩 50U-... 
    # 预算 80U:a 先拿 50U(100份),b 剩 30U/0.50=60 份 -> 取 min(100,60)=60
    out = compute_market_ladders(a, b, rules, 80.0, 10000)
    assert out["a"] == [(0.50, 100)]
    assert out["b"] == [(0.50, 60)]


def test_market_ladders_shares_budget_caps():
    rules = [[{"upper": None, "action": {"type": "fixed_shares", "shares": 400}}]
             for _ in range(6)]
    a = _side([_b(0.10, 1000)])  # 想 400 份
    b = _side([_b(0.10, 1000)])  # 份额预算只剩 500-400=100
    out = compute_market_ladders(a, b, rules, 100000.0, 500)
    assert out["a"] == [(0.10, 400)]
    assert out["b"] == [(0.10, 100)]


def test_market_ladders_wallet_total_decrements_across_tiers():
    rules = [[{"upper": None, "action": {"type": "wallet_total"}}] for _ in range(6)]
    a = _side([_b(0.50, 1000), _b(0.40, 1000)])
    b = None
    # 预算 100U:档1-a 100/0.50=200份(占100U);档2-a 剩0 -> 0
    out = compute_market_ladders(a, b, rules, 100.0, 100000)
    assert out["a"] == [(0.50, 200)]
    assert out["b"] == []


def test_market_ladders_cross_side_order_a_before_b_same_tier():
    rules = [[{"upper": None, "action": {"type": "wallet_total"}}] for _ in range(6)]
    a = _side([_b(0.50, 1000)])
    b = _side([_b(0.50, 1000)])
    # 预算 100U:档1-a 先拿 200份(100U),档1-b 剩0 -> 0
    out = compute_market_ladders(a, b, rules, 100.0, 100000)
    assert out["a"] == [(0.50, 200)]
    assert out["b"] == []
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_laddering.py::test_market_ladders_min_size_both_sides -v`
Expected: FAIL

- [ ] **Step 3: 实现**

追加:

```python
def compute_market_ladders(side_a, side_b, tier_rules, market_budget_usd, max_exposure_shares):
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
                side["bids"], side["reward_range_min"], side["reward_range_max"],
                side["min_size"], tiers_k,
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
            shares = resolve_tier_share(ct, tier_rules[j], price, side["min_size"], remaining_usd)
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
```

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_laddering.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/laddering.py tests/test_laddering.py
git commit -m "feat(laddering): compute_market_ladders 两边共享敞口+跨边遍历顺序"
```

---

## Task 5: laddering.apply_double_sided_floor（§8 <10¢ 双边）

**Files:** Modify `engine/laddering.py`. Test: `tests/test_laddering.py`.

- [ ] **Step 1: 写失败测试**

追加:

```python
from engine.laddering import apply_double_sided_floor


def test_double_sided_no_sub_threshold_unchanged():
    ladders = {"a": [(0.30, 100)], "b": []}
    # 无 <10¢ 档 -> 不强制双边,原样返回
    assert apply_double_sided_floor(ladders, 10) == {"a": [(0.30, 100)], "b": []}


def test_double_sided_sub_threshold_both_sides_kept():
    ladders = {"a": [(0.08, 100)], "b": [(0.30, 100)]}
    assert apply_double_sided_floor(ladders, 10) == {"a": [(0.08, 100)], "b": [(0.30, 100)]}


def test_double_sided_sub_threshold_one_side_cleared():
    ladders = {"a": [(0.08, 100)], "b": []}
    # <10¢ 但只有一边 -> 整市场清空
    assert apply_double_sided_floor(ladders, 10) == {"a": [], "b": []}
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_laddering.py::test_double_sided_no_sub_threshold_unchanged -v`
Expected: FAIL

- [ ] **Step 3: 实现**

追加:

```python
def apply_double_sided_floor(ladders, min_price_double_cents):
    """§8:若任一已定档价 < 阈值,则要求两边都有>0档;否则整市场清空。"""
    threshold = min_price_double_cents / 100.0
    has_sub = any(
        price < threshold for side in ladders.values() for (price, _) in side
    )
    if not has_sub:
        return ladders
    if ladders.get("a") and ladders.get("b"):
        return ladders
    return {"a": [], "b": []}
```

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_laddering.py -v`
Expected: PASS（全部 laddering 单测）

- [ ] **Step 5: Commit**

```bash
git add engine/laddering.py tests/test_laddering.py
git commit -m "feat(laddering): apply_double_sided_floor 实现 §8 <10¢ 强制双边"
```

---

## Task 6: filter_for_template 只 gate + 退役 scan shim

**Files:** Modify `engine/scanner.py`、`tests/test_scanner.py`.

**说明:** `filter_for_template` 去掉 `determine_order_price`/`min_cost`/per-token 定价,只保留 gating,输出轻量 per-token 条目(供 place_orders 按 market_id 分组,在下单时 live 重算)。删除 `scan` 兼容 shim 与其旧单测(单边单单模型随 SP2 退役;`fetch_candidates`/`filter_for_template` 是真实路径,scan 仅测试用)。

- [ ] **Step 1: 调整 filter 测试**

`tests/test_scanner.py` 的 `TestFilterForTemplate` 现断言 `e["market_id"]` 的纳入/排除——这些仍成立。新增一条断言「输出不含 order_price」:

在 `TestFilterForTemplate` 内追加方法:

```python
    def test_eligible_entry_has_no_price(self):
        scanner = self._scanner()
        pool = [self._candidate("B", [])]
        out = scanner.filter_for_template(pool, self._template(), "0xW")
        assert out and all("order_price" not in e for e in out)
        # 仍带下游 place_orders 需要的字段
        e = out[0]
        assert e["token_id"] and "rewards_min_size" in e and "rewards_max_spread" in e
```

- [ ] **Step 2: 删除 scan shim 的旧单测**

在 `tests/test_scanner.py` 中删除整类 `TestMarketFiltering`(它的所有 `scanner.scan()` 用例:`test_accepts_valid_market`/`test_rejects_low_reward`/`test_rejects_near_settlement`/`test_rejects_wide_spread`/`test_rejects_price_out_of_range`/`test_low_balance_still_eligible_with_min_cost`/`test_rejects_cooldown_market`/`test_evaluates_each_token_independently`)以及两个独立函数 `test_scan_upserts_market_meta_with_name_and_slugs`、`test_scan_excludes_blacklisted_market`——它们测的是已退役的单边单单 scan 行为。保留 `TestFetchCandidatesCategoryWiring` 与 `TestFilterForTemplate`(它们测 fetch_candidates 采集与 filter gating,仍有效)。

> 注:`_make_scanner` 辅助函数若只被 `TestMarketFiltering` 用,可一并删除;若被其它保留类引用则留着。删后跑 `python -m pytest tests/test_scanner.py -q` 应无 NameError。

- [ ] **Step 3: 运行确认 FAIL**

Run: `python -m pytest tests/test_scanner.py::TestFilterForTemplate::test_eligible_entry_has_no_price -v`
Expected: FAIL（当前输出仍含 order_price）

- [ ] **Step 4: 改 filter_for_template + 删 scan**

在 `engine/scanner.py`:

(a) 把 `filter_for_template` 内 token 循环里「计算 reward_range / min_cost / determine_order_price / 组装含 order_price 的 eligible」整段(从 `tick_size_str = book.get(...)` 到 `eligible.append({...})`)替换为只 gate + 轻量条目:

```python
                tick_size_str = book.get("tick_size", "0.01")
                eligible.append(
                    {
                        "market_id": condition_id,
                        "token_id": token_id,
                        "market_name": market.get("question", ""),
                        "outcome": token.get("outcome", ""),
                        "market_competitiveness": market.get(
                            "market_competitiveness", 0
                        ),
                        "end_date": end_date_str,
                        "daily_reward": market_reward,
                        "rewards_max_spread": max_spread_reward,
                        "rewards_min_size": min_size,
                        "tick_size_str": tick_size_str,
                        "neg_risk": neg_risk,
                        "tags": market.get("tags", []),
                    }
                )
```

保留其上的 gating(best_bid 价格区间、spread、bids/asks 非空检查)。删除该段里对 `determine_order_price`、`reward_price_range`、`ceil_to_tick`、`min_cost`、`order_price` 的使用。

(b) 删除 `scan` 方法(整段 shim)。

(c) 清理 `engine/scanner.py` 顶部 import:若 `determine_order_price` / `ceil_to_tick` 在文件内已无引用则从 import 删除;`reward_price_range` 现在 filter 也不用了(改到 place_orders 用)——若 scanner.py 内已无引用则一并从 import 移除(它仍存在于 strategy.py,place_orders 会从 strategy import)。用 `grep -n "determine_order_price\|reward_price_range\|ceil_to_tick" engine/scanner.py` 确认无残留引用再删 import。

- [ ] **Step 5: 运行确认 PASS + 全套 scanner**

Run: `python -m pytest tests/test_scanner.py -v`
Expected: PASS（保留的 fetch/filter 类全绿）

- [ ] **Step 6: 全套测试无回归（旧 place_orders 仍可跑）**

Run: `python -m pytest -q`
Expected: ALL PASS。旧 `place_orders` 自行 `get_orderbook`+`determine_order_price` 重算,`market.get("min_cost",0)`→0(闸门 no-op),`market.get("order_size")` 不再有但用 `rewards_min_size`,均容忍字段缺失。若个别 `test_place_orders` 用例断言依赖 `min_cost`,临时放宽(下个任务会重写 place_orders 测试)。

- [ ] **Step 7: Commit**

```bash
git add engine/scanner.py tests/test_scanner.py
git commit -m "refactor(scanner): filter_for_template 只 gate 不定价;退役 scan 兼容 shim"
```

---

## Task 7: place_orders 多档重写

**Files:** Modify `engine/manager.py`、`tests/test_place_orders.py`.

**说明:** 重写 `WalletWorker.place_orders`:按 `market_id` 分组传入的 per-token eligible;每市场跳过黑名单/持仓/冷却;并发市场上限;读 live 余额算预算(扣除该市场已有挂单敞口);每 token live 取订单簿算 reward_range + 两边 sides;调 `compute_market_ladders`+`apply_double_sided_floor`;对未在该价位挂过的 (token,price) 逐个 `place_limit_buy`。

- [ ] **Step 1: 写失败测试**

把 `tests/test_place_orders.py` 中针对**真实** `WalletWorker.place_orders` 的旧用例(基于单边单单 + order_size_mode 的)整体替换为多档用例。新建/替换为:

```python
"""tests/test_place_orders.py — place_orders 多档下单(mock API)。"""

from unittest.mock import MagicMock
from engine.manager import WalletWorker


def _make_worker(balance=10000.0, template=None):
    api = MagicMock()
    db = MagicMock()
    api.get_balance.return_value = balance
    api.get_open_orders.return_value = []
    api.get_user_positions.return_value = []
    api.get_funder.return_value = "0xF"
    db.get_blacklist_ids.return_value = set()
    db.is_in_cooldown.return_value = False
    tmpl = {
        "tiers_k": 6,
        "tier_rules": [[{"upper": None, "action": {"type": "min_size"}}] for _ in range(6)],
        "max_exposure_usd": 250,
        "max_exposure_shares": 500,
        "max_concurrent_markets": 10,
        "min_price_double_cents": 10,
    }
    if template:
        tmpl.update(template)
    db.get_template_for.return_value = tmpl
    return WalletWorker(api, db, "0xW", {"fill_check_interval_sec": 5}), api, db


def _ob(bids, asks=None, tick="0.01"):
    return {
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in (asks or [(0.5, 1000)])],
        "tick_size": tick,
    }


def _elig(market_id, token_id, outcome, min_size=100):
    return {
        "market_id": market_id, "token_id": token_id, "outcome": outcome,
        "market_name": "M", "rewards_min_size": min_size, "rewards_max_spread": 6,
        "tick_size_str": "0.01", "neg_risk": False, "market_competitiveness": 0,
    }


def test_places_multi_tier_min_size_on_one_side():
    worker, api, db = _make_worker()
    # 两个有效档(0.30/0.29 厚度>=1,在 reward 区间内);ask 0.31 -> mid 0.305
    api.get_orderbook.return_value = _ob([(0.30, 300), (0.29, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    # 每档 min_size=100;敞口足够
    calls = api.place_limit_buy.call_args_list
    placed = sorted((c.args[1], c.args[2]) for c in calls)  # (price, size)
    assert (0.30, 100) in placed and (0.29, 100) in placed


def test_exposure_caps_total_usd():
    worker, api, db = _make_worker(template={"max_exposure_usd": 35})
    api.get_orderbook.return_value = _ob([(0.30, 300), (0.29, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    # 档1: 100*0.30=30U;档2 剩 5U/0.29=17 份 -> 取 min(100,17)=17
    placed = {round(c.args[1], 2): c.args[2] for c in api.place_limit_buy.call_args_list}
    assert placed.get(0.30) == 100
    assert placed.get(0.29) == 17


def test_concurrent_market_cap_skips_new_markets():
    worker, api, db = _make_worker(template={"max_concurrent_markets": 1})
    # 已有市场 X 的挂单 -> 已占 1 个并发名额
    api.get_open_orders.return_value = [
        {"side": "BUY", "market": "X", "asset_id": "X-y", "price": "0.30",
         "original_size": "100", "id": "o1"}
    ]
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])  # A 是新市场,超并发上限
    api.place_limit_buy.assert_not_called()


def test_double_sided_floor_blocks_market_when_one_side_only():
    worker, api, db = _make_worker()
    # 价 0.08 < 10¢,只有一边 -> 整市场不挂
    api.get_orderbook.return_value = _ob([(0.08, 300)], [(0.09, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_not_called()


def test_idempotent_skips_existing_price():
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {"side": "BUY", "market": "A", "asset_id": "A-y", "price": "0.30",
         "original_size": "100", "id": "o1"}
    ]
    api.get_orderbook.return_value = _ob([(0.30, 300), (0.29, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    # 0.30 已有挂单 -> 跳过;只挂 0.29
    placed = {round(c.args[1], 2) for c in api.place_limit_buy.call_args_list}
    assert 0.30 not in placed and 0.29 in placed


def test_skips_held_and_cooldown_and_blacklist():
    worker, api, db = _make_worker()
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    db.is_in_cooldown.return_value = True
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_not_called()
```

> `_elig` 模拟 filter_for_template 的轻量 per-token 条目。`place_limit_buy(token_id, price, size, tick_size=, neg_risk=)` 的调用用 positional `c.args`:args[0]=token_id, args[1]=price, args[2]=size。

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_place_orders.py -v`
Expected: FAIL（旧 place_orders 是单边单单,行为不符）

- [ ] **Step 3: 重写 place_orders**

把 `engine/manager.py` 的 `place_orders` 整个方法体替换为:

```python
    def place_orders(self, eligible_markets: list[dict], limit: int | None = None):
        """多档挂单:按市场分组,下单时算 K 档/边,整市场敞口共享,§8 <10¢ 双边。

        eligible_markets = filter_for_template 的轻量 per-token 条目。
        limit 设置时,达到该数量的成功下单后停止。
        """
        from engine.laddering import compute_market_ladders, apply_double_sided_floor
        from engine.strategy import reward_price_range

        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed for %s: %s", self.wallet_address, e)
            return
        try:
            positions = self.api.get_user_positions(self.api.get_funder())
        except Exception as e:
            logger.error(
                "get_user_positions failed for %s, skip placement: %s",
                self.wallet_address, e,
            )
            return
        held = held_condition_ids(positions)
        blacklist = self.db.get_blacklist_ids()
        tmpl = self.db.get_template_for(self.wallet_address)
        tier_rules = tmpl.get("tier_rules") or []
        max_exposure_usd = float(tmpl.get("max_exposure_usd", 250))
        max_exposure_shares = int(tmpl.get("max_exposure_shares", 500))
        max_concurrent = int(tmpl.get("max_concurrent_markets", 10))
        min_price_double_cents = float(tmpl.get("min_price_double_cents", 10))

        buy_orders = [o for o in open_orders if o.get("side") == "BUY"]
        # 已有挂单:每市场已用敞口 + 已挂 (token,price) 集 + 已在做市场集
        exposure_usd, exposure_shares = {}, {}
        open_price_keys, markets_with_open = set(), set()
        for o in buy_orders:
            mkt = o.get("market", "")
            aid = o.get("asset_id", "")
            try:
                p = float(o.get("price", 0) or 0)
                sz = float(o.get("original_size", o.get("size", 0)) or 0)
            except (TypeError, ValueError):
                p, sz = 0.0, 0.0
            exposure_usd[mkt] = exposure_usd.get(mkt, 0.0) + p * sz
            exposure_shares[mkt] = exposure_shares.get(mkt, 0) + int(sz)
            open_price_keys.add((aid, round(p, 4)))
            if mkt:
                markets_with_open.add(mkt)

        # 按市场分组(保持竞争度顺序)
        grouped, order = {}, []
        for e in eligible_markets:
            mid = e["market_id"]
            if mid not in grouped:
                grouped[mid] = []
                order.append(mid)
            grouped[mid].append(e)

        placed = 0
        for mid in order:
            if mid in blacklist or mid in held:
                continue
            if self.db.is_in_cooldown(self.wallet_address, mid):
                continue
            if mid not in markets_with_open and len(markets_with_open) >= max_concurrent:
                continue

            sides = []
            for e in grouped[mid]:
                token_id = e["token_id"]
                try:
                    ob = self.api.get_orderbook(token_id)
                except Exception as ex:
                    logger.warning("Orderbook failed for %s: %s", e.get("market_name", ""), ex)
                    continue
                bids = sorted(ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
                asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
                if not bids or not asks:
                    continue
                best_bid, best_ask = float(bids[0]["price"]), float(asks[0]["price"])
                midpoint = (best_bid + best_ask) / 2
                max_spread = float(e.get("rewards_max_spread", 2))
                rmin, rmax = reward_price_range(midpoint, max_spread)
                sides.append({
                    "token_id": token_id,
                    "outcome": e.get("outcome", ""),
                    "neg_risk": e.get("neg_risk", False),
                    "tick_size_str": ob.get("tick_size", "0.01"),
                    "min_size": int(e.get("rewards_min_size", 0) or 0),
                    "bids": bids,
                    "reward_range_min": rmin,
                    "reward_range_max": rmax,
                    "max_spread": max_spread,
                })
            if not sides:
                continue

            side_a = sides[0]
            side_b = sides[1] if len(sides) > 1 else None
            balance = self.api.get_balance()
            budget = min(balance, max_exposure_usd) - exposure_usd.get(mid, 0.0)
            shares_budget = max_exposure_shares - exposure_shares.get(mid, 0)
            if budget <= 0 or shares_budget <= 0:
                continue
            ladders = compute_market_ladders(side_a, side_b, tier_rules, budget, shares_budget)
            ladders = apply_double_sided_floor(ladders, min_price_double_cents)

            for key, side in (("a", side_a), ("b", side_b)):
                if side is None:
                    continue
                for (price, shares) in ladders.get(key, []):
                    pk = (side["token_id"], round(price, 4))
                    if pk in open_price_keys:
                        continue
                    try:
                        self.api.place_limit_buy(
                            side["token_id"], price, shares,
                            tick_size=side["tick_size_str"], neg_risk=side["neg_risk"],
                        )
                        placed += 1
                        open_price_keys.add(pk)
                        markets_with_open.add(mid)
                        self._record_place_buy_tier(mid, side, price, shares)
                        if limit is not None and placed >= limit:
                            return
                    except Exception as ex:
                        logger.error("place_limit_buy failed %s: %s", side["token_id"], ex)
```

把旧的 `_record_place_buy`(签名含 order_price/max_spread/rmin/rmax)替换为 `_record_place_buy_tier`:

```python
    def _record_place_buy_tier(self, market_id, side, price, shares):
        """记一档买单到 actions(不抛异常)。"""
        try:
            self.db.record_action(
                wallet=self.wallet_address,
                market_id=market_id,
                action_type="place_buy",
                side="买入",
                price=price,
                size=shares,
                reason="多档:在奖励区间内按累加厚度规则表挂买单",
                price_basis=(
                    f"档价 {price:.4f}（{side.get('outcome','')}）；"
                    f"奖励区间[{side['reward_range_min']:.4f},{side['reward_range_max']:.4f}]；"
                    f"来源：CLOB get_orderbook"
                ),
            )
        except Exception as e:
            logger.warning("record_action(place_buy) failed: %s", e)
```

删除 `engine/manager.py` 顶部 `from engine.order_sizing import compute_order_size`(下个任务退役该模块;此处先去 import)。`_record_cap_cancel` 已无调用者(旧 cap 逻辑删除),一并删除该方法。

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_place_orders.py -v`
Expected: PASS

- [ ] **Step 5: 全套测试**

Run: `python -m pytest -q`
Expected: ALL PASS。`test_manager.py` 的 `TestTestPlaceOrders` 用 FakeScanner.filter_for_template 透传、worker 多为 MagicMock,不受真实 place_orders 改写影响;若某用例断言旧字段需小调。

- [ ] **Step 6: Commit**

```bash
git add engine/manager.py tests/test_place_orders.py
git commit -m "feat(manager): place_orders 多档重写(K档/边+整市场敞口+并发上限+<10¢双边+幂等)"
```

---

## Task 8: 退役 determine_order_price / compute_order_size

**Files:** Modify `engine/strategy.py`、`engine/order_sizing.py`(删除)、`tests/test_strategy.py`.

- [ ] **Step 1: 确认无引用**

Run:
```
grep -rn "determine_order_price\|compute_order_size\|order_sizing" engine/ web/ --include=*.py
```
Expected: 仅 `engine/strategy.py`(定义)与 `engine/order_sizing.py`(定义)出现;`manager.py` 已无 import(Task 7 已删)。若 manager.py 仍有 `compute_order_size` 调用残留,先清掉(Task 7 应已删)。

- [ ] **Step 2: 删除老算法 + 用例**

(a) `engine/strategy.py`:删除 `determine_order_price` 函数与三个 `_strategy_cumulative`/`_strategy_spread2_coarse`/`_strategy_spread_ge3_coarse` 私有函数。**保留** `reward_price_range`。

(b) 删除文件 `engine/order_sizing.py`。

(c) `tests/test_strategy.py`:删除所有 `determine_order_price` 相关用例/类。若 `reward_price_range` 有用例则保留;若删后该文件为空则删除整个文件。

(d) 删除 `tests/test_order_sizing.py`(若存在,它测 compute_order_size)。

- [ ] **Step 3: 全套测试**

Run: `python -m pytest -q`
Expected: ALL PASS。

- [ ] **Step 4: 冒烟**

Run: `python -c "import app; print('import ok')"`
Expected: `import ok`

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: 退役 determine_order_price/compute_order_size(被多档引擎取代)"
```

---

## 验收 checkpoint（对应 spec §九）

1. 默认模板(每档 min_size)跑:`test_places_multi_tier_min_size_on_one_side` 证明买一往下多个有效档各挂 min_size;`test_exposure_caps_total_usd` 证明 250U 敞口封顶。
2. 非默认 tier_rules(wallet_total/fixed_shares/skip):`tests/test_laddering.py` 的 compute_market_ladders 用例证明按累加厚度分段 + 敞口/预算递减正确。
3. <10¢ 双边:`test_double_sided_floor_blocks_market_when_one_side_only` + laddering 单测。
4. 并发市场上限:`test_concurrent_market_cap_skips_new_markets`。
5. `pytest -q` 全绿;`determine_order_price`/`compute_order_size` 已删且无残留引用(Task 8 Step 1 grep)。

## 范围之外（留后续）

SP3 三段式离场 · SP4 单份奖励阈值+取档 · SP5 三档节奏+观察名单+成交后单侧暂停+撤改收敛(SP2 仅"按计划挂"+敞口幂等,不撤换价格漂移的旧单) · SP6 模板编辑 UI(逐档规则表 + 退役死字段收口)。
