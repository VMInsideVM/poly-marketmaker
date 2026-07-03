# 网关式单档挂单 + 浮盈市价离场 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增可选「挂单模式」`gap_single`（断层分级 + 高位系数和闸门 + 顺延选一档，每市场一单）与「浮盈市价卖 + 可关止损」离场，`laddering`/`maker` 默认零回归。

**Architecture:** 两个新纯函数（`plan_gap_single_order`、`compute_market_single_orders`）落在 `engine/laddering.py`；`plan_exit`/`effective_theta_stop` 加参数支持 market 止盈与关止损；`manager.place_orders` 与 `monitor.check_exit` 按模板 `placement_mode`/`take_profit_mode` 分支；配置项进 `TEMPLATE_DEFAULTS` 白名单自动存取，配置页加下拉与参数框。

**Tech Stack:** Python 3 / Flask / SQLite / pytest；纯函数不触网，全部单测覆盖。

## Global Constraints

- 用户面向字符串一律简体中文（UI、状态行、卖单理由）。
- 「绝不低于成本卖出」不变量保留：浮盈市价卖仅在 `买一 > 成本` 时触发，成交必 ≥ 成本。
- `laddering` 与 `maker` 为默认值，既有行为与既有测试**零回归**。
- 新配置键必须同时加入 `config.py TEMPLATE_DEFAULTS`，才能被 `/api/settings`、`/api/templates/<id>` 白名单自动存取。
- TDD：先写失败测试、跑红、最小实现、跑绿、提交。频繁小提交。
- 提交只 stage 本任务改动的文件，不卷入仓库既有未提交 WIP。
- 分支：`feat/gap-single-placement`（已创建，spec 已提交）。

---

## Task 1: `plan_gap_single_order` 纯函数

**Files:**
- Modify: `engine/laddering.py`（新增函数，紧跟现有 `amount_value` 之后）
- Test: `tests/test_gap_single.py`（新建）

**Interfaces:**
- Consumes: `engine.laddering.amount_value(price, table)`（已存在）。
- Produces: `plan_gap_single_order(bids, reward_range_min, reward_range_max, min_size, amount_value_table, gap_wide_cents, gap_mid_cents, gap_high_coeff_sum_min, single_order_min_coeff) -> (price: float, shares: int) | None`。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_gap_single.py`：

```python
"""tests/test_gap_single.py — 网关式单档挂单纯函数(不触网)。"""

from engine.laddering import plan_gap_single_order

AV = [
    {"upper": 0.20, "value": 1},
    {"upper": 0.25, "value": 1.5},
    {"upper": 0.31, "value": 2},
]


def _b(price, size):
    return {"price": str(price), "size": str(size)}


def _plan(bids, rmin=0.10, rmax=0.31, min_size=20, av=None,
          wide=10, mid=5, gate=20, x=0):
    return plan_gap_single_order(
        bids, rmin, rmax, min_size,
        AV if av is None else av, wide, mid, gate, x,
    )


def test_normal_market_picks_highest_in_range():
    # 相邻价差 1¢ < 5 -> 规则3;买一 0.28 系数 50/(20*2)=1.25 > 0 -> 挂 0.28 一单 20 股。
    out = _plan([_b(0.28, 50), _b(0.27, 40)])
    assert out == (0.28, 20)


def test_out_of_range_levels_ignored():
    # 0.05 低于 rmin 0.10,剔除;仍挂 0.28。
    out = _plan([_b(0.28, 50), _b(0.05, 9999)])
    assert out == (0.28, 20)


def test_wide_gap_gate_fail_skips_market():
    # 0.27->0.15 价差 12¢ > 10 -> 规则1;高位{0.28,0.27}系数和 1.25+0.75=2 < 20 -> None。
    out = _plan([_b(0.28, 50), _b(0.27, 30), _b(0.15, 400)])
    assert out is None


def test_wide_gap_gate_pass_places():
    # 同上但 0.27 厚 -> 高位系数和 1.25+20=21.25 >= 20 -> 顺延挂买一 0.28。
    out = _plan([_b(0.28, 50), _b(0.27, 800), _b(0.15, 400)])
    assert out == (0.28, 20)


def test_mid_gap_no_gate_places():
    # 价差 7¢ 在 [5,10] -> 规则2,无闸门,挂买一 0.28。
    out = _plan([_b(0.28, 50), _b(0.21, 400)])
    assert out == (0.28, 20)


def test_fallthrough_to_lower_when_top_below_x():
    # x=2:买一 0.28 系数 1.25 不>2 -> 顺延买二 0.27 系数 200/40=5 >2 -> 挂 0.27。
    out = _plan([_b(0.28, 50), _b(0.27, 200)], x=2)
    assert out == (0.27, 20)


def test_none_qualify_returns_none():
    out = _plan([_b(0.28, 50), _b(0.27, 40)], x=10)
    assert out is None


def test_empty_or_zero_min_size_returns_none():
    assert _plan([]) is None
    assert _plan([_b(0.28, 50)], min_size=0) is None


def test_no_in_range_returns_none():
    # 全部买档在奖励区间外。
    assert _plan([_b(0.05, 500), _b(0.04, 500)]) is None


def test_gap_exactly_10_is_rule2_not_gated():
    # 价差恰 10¢ 不 >10 -> 规则2,不触发高位闸门;高位系数虽小仍挂买一。
    out = _plan([_b(0.28, 10), _b(0.18, 10)])
    assert out == (0.28, 20)


def test_amount_value_missing_gives_zero_coeff():
    # 价 0.35 超金额表(顶 0.31)-> 系数 0,x=0 时 0 不>0 -> None。
    out = _plan([_b(0.35, 100)], rmax=0.40)
    assert out is None


def test_tie_max_gap_splits_at_topmost():
    # 两处价差都 10¢、wide=8 都算宽断层;取最靠上那处劈分 -> 高位仅{0.30}系数 2.5 <20 -> None。
    # (若误在第二处劈分,高位含 0.20 厚档系数和会 >=20 而误挂。)
    out = _plan([_b(0.30, 100), _b(0.20, 800), _b(0.10, 100)], wide=8)
    assert out is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_gap_single.py -v`
Expected: FAIL（`ImportError: cannot import name 'plan_gap_single_order'`）。

- [ ] **Step 3: 最小实现**

在 `engine/laddering.py` 的 `amount_value` 函数之后插入：

```python
def plan_gap_single_order(
    bids,
    reward_range_min,
    reward_range_max,
    min_size,
    amount_value_table,
    gap_wide_cents,
    gap_mid_cents,
    gap_high_coeff_sum_min,
    single_order_min_coeff,
):
    """网关式单档挂单(v4 用户策略,纯函数)。

    对单个 token 的买单簿:取奖励区间内买档 -> 算风险系数(份数/(最低份数×金额数值))
    -> 按相邻在区间档的最大价差分三级(>宽 / 中~宽 / <中)-> 宽断层再查
    「断层上方各档风险系数之和 >= gap_high_coeff_sum_min」市场闸门(不过则整市场不挂)
    -> 自最高在区间档往下取第一个「系数 > single_order_min_coeff」的档,挂 min_size 一单。
    返回 (price, shares) 或 None(不挂)。价差单位为美分;闸门/选档口径:价差 10/5 归中一级
    (规则1 严格 >宽、规则3 严格 <中);高位系数和 == 门槛 放行;选档严格 > x。
    """
    if min_size <= 0 or not bids:
        return None
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
        return None
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
    # 仅宽断层加「高位风险系数和」市场闸门。
    if max_gap > gap_wide_cents:
        high_sum = sum(lv["coeff"] for lv in in_range[: split_idx + 1])
        if high_sum < gap_high_coeff_sum_min:
            return None
    # 三级统一顺延:自上而下第一个 coeff > x。
    for lv in in_range:
        if lv["coeff"] > single_order_min_coeff:
            return (lv["price"], int(min_size))
    return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_gap_single.py -v`
Expected: PASS（12 项全绿）。

- [ ] **Step 5: 提交**

```bash
git add engine/laddering.py tests/test_gap_single.py
git commit -m "feat(laddering): plan_gap_single_order 断层分级单档挂单纯函数"
```

---

## Task 2: `compute_market_single_orders` 预算/敞口封顶

**Files:**
- Modify: `engine/laddering.py`（新增函数，紧跟 `plan_gap_single_order` 之后）
- Test: `tests/test_gap_single.py`（追加）

**Interfaces:**
- Consumes: `plan_gap_single_order`（Task 1）。
- Produces: `compute_market_single_orders(side_a, side_b, market_budget_usd, max_exposure_shares, amount_value_table, gap_wide_cents, gap_mid_cents, gap_high_coeff_sum_min, single_order_min_coeff) -> {"a": [(price, shares)]|[], "b": [...]}`。`side_x` 结构：`{"bids", "reward_range_min", "reward_range_max", "min_size"}` 或 `None`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_gap_single.py` 末尾追加：

```python
from engine.laddering import compute_market_single_orders


def _side(bids, rmin=0.10, rmax=0.31, min_size=20):
    return {
        "bids": bids,
        "reward_range_min": rmin,
        "reward_range_max": rmax,
        "min_size": min_size,
    }


def test_single_orders_one_side_places_one():
    side = _side([_b(0.28, 50), _b(0.27, 40)])
    out = compute_market_single_orders(side, None, 1000.0, 500, AV, 10, 5, 20, 0)
    assert out == {"a": [(0.28, 20)], "b": []}


def test_single_orders_budget_too_small_skips():
    # 预算 3 USD:0.28×20=5.6 > 3 -> 封顶 int(3/0.28)=10 < min_size 20 -> 放弃。
    side = _side([_b(0.28, 50)])
    out = compute_market_single_orders(side, None, 3.0, 500, AV, 10, 5, 20, 0)
    assert out == {"a": [], "b": []}


def test_single_orders_both_sides_share_budget():
    a = _side([_b(0.28, 50)])
    b = _side([_b(0.22, 400)])
    out = compute_market_single_orders(a, b, 1000.0, 500, AV, 10, 5, 20, 0)
    assert out["a"] == [(0.28, 20)]
    assert out["b"] == [(0.22, 20)]


def test_single_orders_none_side_skipped():
    out = compute_market_single_orders(None, None, 1000.0, 500, AV, 10, 5, 20, 0)
    assert out == {"a": [], "b": []}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_gap_single.py -k single_orders -v`
Expected: FAIL（`cannot import name 'compute_market_single_orders'`）。

- [ ] **Step 3: 最小实现**

在 `engine/laddering.py` 的 `plan_gap_single_order` 之后插入：

```python
def compute_market_single_orders(
    side_a,
    side_b,
    market_budget_usd,
    max_exposure_shares,
    amount_value_table,
    gap_wide_cents,
    gap_mid_cents,
    gap_high_coeff_sum_min,
    single_order_min_coeff,
):
    """两边共享敞口的网关式单档计划(每边至多一单)。

    对每边调 plan_gap_single_order,再按剩余预算/敞口封顶;封顶后不足 min_size 则放弃
    该边(不挂残档,与 compute_market_ladders 口径一致)。a 先于 b 扣预算。
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
            single_order_min_coeff,
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_gap_single.py -v`
Expected: PASS（全绿）。

- [ ] **Step 5: 提交**

```bash
git add engine/laddering.py tests/test_gap_single.py
git commit -m "feat(laddering): compute_market_single_orders 单档预算/敞口封顶"
```

---

## Task 3: `plan_exit` 浮盈市价模式 + 止损可关

**Files:**
- Modify: `engine/take_profit.py`（`effective_theta_stop`、`plan_exit`）
- Test: `tests/test_exit_plan.py`（追加；现有测试不改，验证零回归）

**Interfaces:**
- Consumes: `ceil_to_tick`（已存在）。
- Produces: `effective_theta_stop(cost, mode, percent, cents)` 在 `mode=="off"` 时返回 `None`；`plan_exit(cost, best_bid, best_ask, tick, theta_loss, theta_stop, case_a_mode, size, take_profit_mode="maker")`，`theta_stop` 可为 `None`（不触发 B0）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_exit_plan.py` 末尾追加：

```python
def test_effective_theta_stop_off_returns_none():
    assert effective_theta_stop(0.30, "off", 20, 5) is None


def _pm(cost, best_bid, best_ask, tier, action, price=None,
        theta_stop=0.05, size=100, tick=0.01):
    out = plan_exit(
        cost, best_bid, best_ask, tick, 0.02, theta_stop, "ask", size,
        take_profit_mode="market",
    )
    assert out["tier"] == tier and out["action"] == action
    if price is not None:
        assert abs(out["price"] - price) < 1e-9
    return out


def test_market_mode_profit_cost_below_bid_market_sells():
    # 成本 0.28 < 买一 0.30(浮盈)-> 市价清仓。
    _pm(0.28, 0.30, 0.33, "A_market", "market")


def test_market_mode_boundary_cost_equals_bid_rests_at_cost():
    # 成本 == 买一 归保本侧 -> 挂成本价。
    _pm(0.30, 0.30, 0.33, "B_park", "rest", price=0.30)


def test_market_mode_underwater_stop_off_rests_at_cost():
    # 套牢 + 止损关(theta_stop None)-> 挂成本价,绝不 B0。
    out = plan_exit(0.40, 0.30, 0.42, 0.01, 0.02, None, "ask", 100,
                    take_profit_mode="market")
    assert out["tier"] == "B_park" and out["action"] == "rest"
    assert abs(out["price"] - 0.40) < 1e-9


def test_market_mode_underwater_stop_on_fires_b0():
    # 套牢 + 止损开:亏损 0.10 >= theta_stop 0.05 -> B0 市价。
    _pm(0.40, 0.30, 0.42, "B0", "market", theta_stop=0.05)


def test_market_mode_no_book_noops():
    out = plan_exit(0.30, None, None, 0.01, 0.02, None, "ask", 100,
                    take_profit_mode="market")
    assert out["tier"] == "none" and out["action"] == "noop"


def test_maker_mode_stop_off_no_b0():
    # maker 默认 + 止损关:套牢挂成本价、不 B0(theta_stop None 被安全跳过)。
    out = plan_exit(0.40, 0.30, 0.42, 0.01, 0.02, None, "ask", 100)
    assert out["tier"] == "B_park" and out["action"] == "rest"
    assert abs(out["price"] - 0.40) < 1e-9
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_exit_plan.py -k "market_mode or theta_stop_off" -v`
Expected: FAIL（market 分支未实现，`A_market`/`B_park` 断言不符；`theta_stop=None` 触发 `TypeError` 或错误 tier）。

- [ ] **Step 3: 实现**

`engine/take_profit.py` — 在 `effective_theta_stop` 顶部加 `off` 分支：

```python
def effective_theta_stop(cost, mode, percent, cents):
    """B0 强平的有效阈值(价位单位 = ¢/100)。三种模式:

    - "off":返回 None,调用方据此彻底关闭 B0 强平(套牢仓无兜底,用户明确选择)。
    - "percent"(默认):成本 × 百分比/100,相对成本的最大回撤(默认 20%)。
    - "fixed":固定美分 / 100(默认 5¢),与成本无关的固定亏损额。

    任一参数异常时回退固定美分,绝不让止损阈值算成 0/负(那会一触即发或永不触发)。
    """
    if str(mode) == "off":
        return None
    try:
        if str(mode) == "fixed":
            return max(0.0, float(cents) / 100.0)
        return max(0.0, float(cost) * float(percent) / 100.0)
    except (TypeError, ValueError):
        try:
            return max(0.0, float(cents) / 100.0)
        except (TypeError, ValueError):
            return 0.05
```

`engine/take_profit.py` — 改 `plan_exit`（加 `take_profit_mode` 形参、market 分支、`theta_stop is not None` 护栏）。将现有 `plan_exit` 整体替换为：

```python
def plan_exit(
    cost,
    best_bid,
    best_ask,
    tick,
    theta_loss,
    theta_stop,
    case_a_mode,
    size,
    take_profit_mode="maker",
):
    """两段式离场决策。theta_stop 为价位单位(=¢/100),None 表示关闭 B0 强平。

    take_profit_mode:
      - "maker"(默认,现状):盈利(成本 ≤ 买一)挂卖一做 maker 捕价差(对成本取下限);
        套牢挂成本价;亏损 ≥ theta_stop 市价止损(B0)。
      - "market":浮盈(成本 < 买一)立即市价清仓(A_market;成交必 ≥ 成本);保本/套牢
        (成本 ≥ 买一)挂成本价等回本;theta_stop 非 None 时仍有 B0 兜底。

    两个模式都绝不低于成本卖出:唯一认亏出口是 B0 强平(theta_stop 为 None 时无此出口)。
    cost>0、size>0 由调用方保证。theta_loss / case_a_mode 保留形参不再使用。
    """
    cost_floor = ceil_to_tick(cost, tick)

    if take_profit_mode == "market":
        # 浮盈:成本 < 买一 -> 立即市价卖出(买一>成本,成交必 ≥ 成本)。
        if best_bid is not None and cost < best_bid:
            return {"tier": "A_market", "action": "market", "price": None, "size": size}
        # 兜底止损(仅在开启时):亏损 ≥ theta_stop 市价清仓。
        if (
            theta_stop is not None
            and best_bid is not None
            and (cost - best_bid) >= theta_stop
        ):
            return {"tier": "B0", "action": "market", "price": None, "size": size}
        # 完全无盘口:不挂进死 book。
        if best_bid is None and (best_ask is None or best_ask <= 0):
            return {"tier": "none", "action": "noop", "price": None, "size": 0.0}
        # 保本/套牢:挂成本价等回本(绝不低于成本)。
        return {"tier": "B_park", "action": "rest", "price": cost_floor, "size": size}

    # --- maker 模式(默认,现状) ---
    # 盈利:成本 ≤ 买一 -> 挂卖一做 maker,但绝不挂在成本下方(异常/幻影盘口兜底)。
    if best_bid is not None and cost <= best_bid:
        if best_ask is not None and best_ask > 0:
            price = max(best_ask, cost_floor)
        else:
            price = cost_floor
        return {"tier": "A", "action": "rest", "price": price, "size": size}

    # 成本 > 买一:套牢/保本。亏损过大兜底市价止损(theta_stop None 时关闭)。
    if (
        theta_stop is not None
        and best_bid is not None
        and (cost - best_bid) >= theta_stop
    ):
        return {"tier": "B0", "action": "market", "price": None, "size": size}

    # 完全无盘口:不挂进死 book。
    if best_bid is None and (best_ask is None or best_ask <= 0):
        return {"tier": "none", "action": "noop", "price": None, "size": 0.0}

    # 套牢/无买盘:挂成本价等回本(绝不低于成本)。
    return {"tier": "B_park", "action": "rest", "price": cost_floor, "size": size}
```

- [ ] **Step 4: 跑测试确认通过（含现有回归）**

Run: `pytest tests/test_exit_plan.py -v`
Expected: PASS（新老全绿；现有 `_p(...)` 走 `maker` 默认，行为不变）。

- [ ] **Step 5: 提交**

```bash
git add engine/take_profit.py tests/test_exit_plan.py
git commit -m "feat(take_profit): plan_exit 浮盈市价止盈模式 + effective_theta_stop 可关"
```

---

## Task 4: 配置默认值 + 白名单契约

**Files:**
- Modify: `config.py`（`TEMPLATE_DEFAULTS` 新增键 + 金额表顶档 0.30→0.31）
- Modify: `tests/test_database.py:611-615`（同步默认金额表断言）
- Test: `tests/test_settings_routes.py`（追加新键 round-trip 契约）

**Interfaces:**
- Produces: `TEMPLATE_DEFAULTS` 含 `placement_mode/gap_wide_cents/gap_mid_cents/gap_high_coeff_sum_min/single_order_min_coeff/take_profit_mode`；`amount_value_table` 顶档 `{"upper": 0.31, "value": 2}`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_settings_routes.py` 末尾追加：

```python
def test_post_settings_roundtrips_gap_single_keys(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    payload = {
        "placement_mode": "gap_single",
        "gap_wide_cents": 10,
        "gap_mid_cents": 5,
        "gap_high_coeff_sum_min": 20,
        "single_order_min_coeff": 1.5,
        "take_profit_mode": "market",
        "stop_loss_mode": "off",
    }
    resp = client.post("/api/settings", json=payload)
    assert resp.status_code == 200
    tmpl = db.get_template(db.get_default_template_id())
    assert tmpl["placement_mode"] == "gap_single"
    assert tmpl["gap_wide_cents"] == 10
    assert tmpl["gap_mid_cents"] == 5
    assert tmpl["gap_high_coeff_sum_min"] == 20
    assert tmpl["single_order_min_coeff"] == 1.5
    assert tmpl["take_profit_mode"] == "market"
    assert tmpl["stop_loss_mode"] == "off"
```

同时把 `tests/test_database.py` 的默认金额表断言（约 611-615 行）改为 0.31：

```python
    table = TEMPLATE_DEFAULTS["amount_value_table"]
    assert table == [
        {"upper": 0.20, "value": 1},
        {"upper": 0.25, "value": 1.5},
        {"upper": 0.31, "value": 2},
    ]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_settings_routes.py::test_post_settings_roundtrips_gap_single_keys tests/test_database.py::test_template_defaults_has_tier_match_and_size_range -v`
Expected: FAIL（新键不在白名单被 POST 丢弃；金额表断言仍是 0.30）。

- [ ] **Step 3: 实现**

`config.py` — 在 `TEMPLATE_DEFAULTS` 内新增键（放在 `case_a_mode` 之后一段合适位置）：

```python
    # 挂单模式(v4 用户策略):laddering=多档做市(默认);gap_single=断层分级单档。
    "placement_mode": "laddering",
    "gap_wide_cents": 10,
    "gap_mid_cents": 5,
    "gap_high_coeff_sum_min": 20,
    "single_order_min_coeff": 0,
    # 止盈方式:maker=挂卖一吃价差(默认);market=浮盈(成本<买一)立即市价清仓。
    "take_profit_mode": "maker",
```

`config.py` — 把 `amount_value_table` 顶档 0.30 改 0.31：

```python
    "amount_value_table": [
        {"upper": 0.20, "value": 1},
        {"upper": 0.25, "value": 1.5},
        {"upper": 0.31, "value": 2},
    ],
```

（`stop_loss_mode` 已存在，无需新增；"off" 作为可选值由配置页与 `effective_theta_stop` 支持，`TEMPLATE_DEFAULTS` 保持默认 "percent"。）

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_settings_routes.py tests/test_database.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add config.py tests/test_settings_routes.py tests/test_database.py
git commit -m "feat(config): 新增 gap_single/take_profit 模板键 + 金额表顶档 0.31"
```

---

## Task 5: manager 接入 placement_mode 分支

**Files:**
- Modify: `engine/manager.py`（`place_orders`：局部 import、读模板键、tier_rules 闸门条件化、分支调用）
- Test: `tests/test_place_orders.py`（追加 gap_single 用例）

**Interfaces:**
- Consumes: `compute_market_single_orders`（Task 2）；模板键（Task 4）。
- Produces: gap_single 模板下，`place_orders` 每市场至多挂一单。

- [ ] **Step 1: 写失败测试**

在 `tests/test_place_orders.py` 末尾追加：

```python
def test_gap_single_places_one_order_highest_qualifying():
    worker, api, db = _make_worker(template={
        "placement_mode": "gap_single",
        "tier_rules": [],  # gap_single 不需要 tier_rules,不应被空 tier_rules 闸拦
        "amount_value_table": [
            {"upper": 0.20, "value": 1},
            {"upper": 0.25, "value": 1.5},
            {"upper": 0.31, "value": 2},
        ],
        "gap_wide_cents": 10,
        "gap_mid_cents": 5,
        "gap_high_coeff_sum_min": 20,
        "single_order_min_coeff": 0,
    })
    # 相邻价差 1¢(规则3);买一 0.28 系数 50/(20*2)=1.25 >0 -> 挂一单 20 股 @0.28。
    api.get_orderbook.return_value = _ob([(0.28, 50), (0.27, 40)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=20)])
    assert api.place_limit_buy.call_count == 1
    c = api.place_limit_buy.call_args_list[0]
    assert round(c.args[1], 2) == 0.28 and c.args[2] == 20
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_place_orders.py::test_gap_single_places_one_order_highest_qualifying -v`
Expected: FAIL（空 `tier_rules` 触发早退 return，`place_limit_buy` 未被调用）。

- [ ] **Step 3: 实现**

`engine/manager.py` — 局部 import（约 126-130 行）加入新函数：

```python
        from engine.laddering import (
            compute_market_ladders,
            compute_market_single_orders,
            apply_double_sided_floor,
            reconcile_buy_orders,
        )
```

`engine/manager.py` — 读模板键处（`tier_match_var`/`amount_value_table` 之后，约 152 行后）新增：

```python
        placement_mode = tmpl.get("placement_mode", "laddering")
        gap_wide_cents = float(tmpl.get("gap_wide_cents", 10))
        gap_mid_cents = float(tmpl.get("gap_mid_cents", 5))
        gap_high_coeff_sum_min = float(tmpl.get("gap_high_coeff_sum_min", 20))
        single_order_min_coeff = float(tmpl.get("single_order_min_coeff", 0))
```

`engine/manager.py` — 空 tier_rules 早退闸（约 153 行）改为仅 laddering 模式生效：

```python
        if placement_mode == "laddering" and not tier_rules:
```

`engine/manager.py` — 计算 ladders 的分支（约 290-302 行 `if budget_ok:` 块）改为：

```python
            if budget_ok:
                ca = None if side_a["token_id"] in held_assets else side_a
                cb = None if (side_b and side_b["token_id"] in held_assets) else side_b
                if placement_mode == "gap_single":
                    ladders = compute_market_single_orders(
                        ca,
                        cb,
                        budget,
                        shares_budget,
                        amount_value_table,
                        gap_wide_cents,
                        gap_mid_cents,
                        gap_high_coeff_sum_min,
                        single_order_min_coeff,
                    )
                else:
                    ladders = compute_market_ladders(
                        ca,
                        cb,
                        tier_rules,
                        budget,
                        shares_budget,
                        tier_match_var,
                        amount_value_table,
                    )
                    ladders = apply_double_sided_floor(
                        ladders, min_price_double_cents
                    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_place_orders.py -v`
Expected: PASS（新用例绿；既有多档用例不变）。

- [ ] **Step 5: 提交**

```bash
git add engine/manager.py tests/test_place_orders.py
git commit -m "feat(manager): place_orders 按 placement_mode 分支单档/多档"
```

---

## Task 6: monitor + routes 接入 take_profit_mode / 关止损

**Files:**
- Modify: `engine/monitor.py`（`check_exit` 读 `take_profit_mode` 并传参；`_exit_position` 加形参、传给 `plan_exit`、日志护栏、市价分支止盈/止损分标签）
- Modify: `web/routes.py`（positions 展示 `stop_price` 对 `eff=None` 兜底）
- Test: `tests/test_monitor.py`（追加离场模式行为用例）

**Interfaces:**
- Consumes: `plan_exit(..., take_profit_mode=...)`、`effective_theta_stop(...) -> None`（Task 3）。
- Produces: gap_single 用户组合（market + off）下，浮盈市价清仓、套牢挂成本、无 B0。

- [ ] **Step 1: 写失败测试**

在 `tests/test_monitor.py` 末尾追加：

```python
class TestExitTakeProfitMode:
    def _pos(self):
        return {"asset": "tok1", "size": 100.0, "curPrice": 0.30, "conditionId": "mkt1"}

    def test_market_mode_profit_market_sells(self):
        monitor, api, db = _make_monitor(
            {"take_profit_mode": "market", "stop_loss_mode": "off"}
        )
        api.get_user_positions.return_value = [self._pos()]
        api.get_open_orders.return_value = []
        monitor._cost_lots = MagicMock(return_value=(0.30, []))
        # 买一 0.35 > 成本 0.30 -> 浮盈。
        monitor._sell_book = MagicMock(return_value=(0.01, "0.01", 0.35, 0.40))
        monitor.check_exit()
        api.place_market_sell.assert_called_once()
        api.place_limit_sell.assert_not_called()

    def test_market_mode_underwater_stop_off_rests_at_cost(self):
        monitor, api, db = _make_monitor(
            {"take_profit_mode": "market", "stop_loss_mode": "off"}
        )
        api.get_user_positions.return_value = [self._pos()]
        api.get_open_orders.return_value = []
        monitor._cost_lots = MagicMock(return_value=(0.40, []))
        # 买一 0.30 < 成本 0.40 -> 套牢;止损关 -> 挂成本价,不 B0。
        monitor._sell_book = MagicMock(return_value=(0.01, "0.01", 0.30, 0.42))
        monitor.check_exit()
        api.place_market_sell.assert_not_called()
        api.place_limit_sell.assert_called_once()
        args, kwargs = api.place_limit_sell.call_args
        assert abs(args[1] - 0.40) < 1e-9
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_monitor.py::TestExitTakeProfitMode -v`
Expected: FAIL（`check_exit` 未读/未传 `take_profit_mode`，浮盈仍走 maker 挂卖一而非市价）。

- [ ] **Step 3: 实现**

`engine/monitor.py` `check_exit` — 读取新模板键（在 `case_a_mode = ...` 之后，约 306 行）：

```python
        take_profit_mode = tmpl.get("take_profit_mode", "maker")
```

并把它加进 `_exit_position(...)` 调用（约 319-327 行）参数列表末尾：

```python
                self._exit_position(
                    pos,
                    open_orders,
                    theta_loss,
                    stop_mode,
                    stop_percent,
                    stop_cents,
                    case_a_mode,
                    take_profit_mode,
                )
```

`engine/monitor.py` `_exit_position` — 函数签名（约 331-340 行）末尾加形参：

```python
    def _exit_position(
        self,
        pos,
        open_orders,
        theta_loss,
        stop_mode,
        stop_percent,
        stop_cents,
        case_a_mode,
        take_profit_mode="maker",
    ):
```

`engine/monitor.py` `_exit_position` — `plan_exit` 调用（约 368-370 行）加 `take_profit_mode`：

```python
        plan = plan_exit(
            cost,
            best_bid,
            best_ask,
            tick,
            theta_loss,
            theta_stop,
            case_a_mode,
            size,
            take_profit_mode,
        )
```

`engine/monitor.py` `_exit_position` — 离场决策日志（约 373-386 行）对 `theta_stop=None` 兜底。把日志格式串里的 `强平阈值=%.4f(%s)` 改为 `强平阈值=%s(%s)`，并把对应实参 `theta_stop` 改为安全字符串：

```python
        stop_txt = f"{theta_stop:.4f}" if theta_stop is not None else "关闭"
        logger.info(
            "离场决策 asset=%s 成本=%.4f 买一=%s 卖一=%s tick=%s size=%s 强平阈值=%s(%s) -> tier=%s action=%s 挂价=%s",
            asset_id,
            cost,
            best_bid,
            best_ask,
            tick_str,
            size,
            stop_txt,
            stop_mode,
            plan["tier"],
            plan["action"],
            plan["price"],
        )
```

`engine/monitor.py` `_exit_position` 市价分支 — 止盈(A_market)与止损(B0)分标签（约 530-562 行）。将该段替换为：

```python
            fill = market_fill_price(resp, best_bid, cur)
            is_profit = plan["tier"] == "A_market"
            if plan["tier"] == "B0":
                self.db.record_trade(
                    wallet=self.wallet_address,
                    market_id=cid,
                    market_name="",
                    side="stop_loss",
                    price=fill,
                    size=size,
                    pnl=(fill - cost) * size,
                )
            self._record_action(
                market_id=cid,
                action_type="exit_market",
                side="卖出",
                price=fill,
                size=size,
                reason=(
                    f"{plan['tier']}：浮盈市价止盈离场"
                    if is_profit
                    else f"{plan['tier']}：市价清仓离场"
                ),
                price_basis=(
                    f"{basis}；市价清仓·成交≈买一{fill:.4f}（精确成交以链上为准）；"
                    f"来源：CLOB get_trades+get_orderbook"
                ),
            )
            self._status_add(
                market=cid,
                side="卖出",
                price=f"{fill:.4f}",
                size=str(size),
                matched="-",
                stage="离场",
                action=(
                    f"浮盈市价止盈({plan['tier']})"
                    if is_profit
                    else f"市价清仓({plan['tier']})"
                ),
                detail=f"成本{cost:.4f} 成交≈{fill:.4f}",
            )
            return
```

`web/routes.py` positions 展示 — `stop_price` 对 `eff=None` 兜底（约 873 行）：

```python
                        "stop_price": (
                            max(0.0, avg - eff) if eff is not None else None
                        ),
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_monitor.py -v`
Expected: PASS（新用例绿；既有 monitor 用例不变）。

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py web/routes.py tests/test_monitor.py
git commit -m "feat(monitor): check_exit 透传 take_profit_mode + 关止损兜底展示"
```

---

## Task 7: 配置页 UI（挂单模式 / 止盈 / 关止损）

**Files:**
- Modify: `web/templates/config.html`（HTML 控件 + JS show/hide + 保存收集）

**Interfaces:**
- Consumes: `/api/templates/<id>` GET/PUT（已存在，键由 Task 4 白名单覆盖）。
- Produces: 可编辑并持久化 `placement_mode/gap_*/single_order_min_coeff/take_profit_mode/stop_loss_mode(off)`。

无自动化测试，用手动走查验证（Step 4）。

- [ ] **Step 1: HTML — 止损方式加「关闭」项**

`web/templates/config.html` 把止损方式 select（约 68-71 行）改为：

```html
                <select name="stop_loss_mode" id="stop-loss-mode" onchange="updateStopMode()">
                    <option value="percent">按比例（占成本 %）</option>
                    <option value="fixed">按固定金额（美分）</option>
                    <option value="off">关闭止损（⚠️套牢仓无兜底，可能扛到归零）</option>
                </select>
```

- [ ] **Step 2: HTML — 新增「挂单模式」区块**

在「做市品类」`<h3>`（约 99 行 `<h3>做市品类`）之前插入：

```html
        <h3>挂单模式</h3>
        <div class="form-group">
            <label>挂单模式</label>
            <select name="placement_mode" id="placement-mode" onchange="updatePlacementMode()">
                <option value="laddering">多档做市（默认）</option>
                <option value="gap_single">网关式单档（断层分级 · 每市场一单）</option>
            </select>
        </div>
        <div id="gap-single-params">
            <div class="form-grid">
                <div class="form-group"><label>宽断层阈值 (美分，&gt;此值走规则1)</label><input type="number" name="gap_wide_cents" step="0.1"></div>
                <div class="form-group"><label>中断层阈值 (美分，≥此值走规则2)</label><input type="number" name="gap_mid_cents" step="0.1"></div>
                <div class="form-group"><label>规则1 高位风险系数和门槛</label><input type="number" name="gap_high_coeff_sum_min" step="0.1"></div>
                <div class="form-group"><label>选档风险系数门槛 x（&gt;x 才挂）</label><input type="number" name="single_order_min_coeff" step="0.1"></div>
            </div>
            <p class="hint" style="color:#888;font-size:12px">按买单簿「奖励区间内相邻档最大价差」分三级：&gt;宽断层→规则1（再查断层上方各档风险系数之和≥门槛，不过则整市场不挂）；中~宽→规则2；&lt;中→规则3。三级都从买一往下取第一个「风险系数&gt;x」的档，挂最低份数一单。风险系数＝盘口量÷(最低份数×金额数值)，金额数值用下方表。</p>
        </div>
        <div class="form-group">
            <label>止盈方式</label>
            <select name="take_profit_mode" id="take-profit-mode">
                <option value="maker">挂卖一做 maker（默认 · 吃价差）</option>
                <option value="market">浮盈市价卖（成本&lt;买一立即市价清仓）</option>
            </select>
        </div>
```

- [ ] **Step 3: HTML — 包裹多档规则区块以便隐藏**

把「多档挂单规则」`<h3>`、说明 `<p>`、`<div id="tier-rules-editor">`、`+加一档` 按钮（约 133-136 行）整体用 `<div id="tier-rules-section">` 包裹：

```html
        <div id="tier-rules-section">
        <h3>多档挂单规则</h3>
        <p style="color:#888;font-size:12px;margin-top:4px;">每档从买一往下；每行「<b id="tier-var-name">累计厚度</b> &gt; X → 动作」，取第一个命中的区间，末行「其余」兜底。多行阈值须<b>从大到小</b>填（否则小阈值会遮住大的）。动作：最小份数 / 固定份数 / 固定金额 / 钱包全额 / 不挂。</p>
        <div id="tier-rules-editor"></div>
        <button type="button" class="btn" onclick="addTier()">+ 加一档</button>
        </div>
```

- [ ] **Step 4: JS — 新增 updatePlacementMode，接入 load 与 save**

`web/templates/config.html` `<script>` 内，在 `updateStopMode` 函数之后新增：

```javascript
// 挂单模式切换:单档模式显示断层参数、隐藏多档规则编辑器(其配置在单档模式不参与)。
function updatePlacementMode() {
    const mode = (document.getElementById('placement-mode') || {}).value || 'laddering';
    const gap = document.getElementById('gap-single-params');
    if (gap) gap.style.display = (mode === 'gap_single') ? '' : 'none';
    const tierSec = document.getElementById('tier-rules-section');
    if (tierSec) tierSec.style.display = (mode === 'gap_single') ? 'none' : '';
}
```

在 `loadStrategy` 的 `tmplP.then(...)` 块里，`updateStopMode();`（约 303 行）之后加一行：

```javascript
        updatePlacementMode();
```

在 `strategy-form` 的 submit 处理器里，紧跟 `data.tier_match_var = ...`（约 556 行）之后加两个 select 的收集：

```javascript
    data.placement_mode = (document.getElementById('placement-mode') || {}).value || 'laddering';
    data.take_profit_mode = (document.getElementById('take-profit-mode') || {}).value || 'maker';
```

把 submit 处理器里 tier_rules 的两处校验（`badUpper` / `badParam`，约 568-580 行）用挂单模式判断包起来（gap_single 模式不校验多档规则）。将这段替换为：

```javascript
    const placementMode = (document.getElementById('placement-mode') || {}).value || 'laddering';
    if (placementMode !== 'gap_single') {
        const badUpper = Array.from(
            document.querySelectorAll('#tier-rules-editor .interval-row:not(.catch-all) .upper-input')
        ).some(inp => inp.value === '' || isNaN(parseFloat(inp.value)));
        if (badUpper) { alert('请填写每个区间的' + tierVarName() + '阈值'); return; }
        const badParam = Array.from(
            document.querySelectorAll('#tier-rules-editor .interval-row')
        ).some(row => {
            const sel = row.querySelector('.action-select');
            if (!sel || (sel.value !== 'fixed_shares' && sel.value !== 'fixed_amount')) return false;
            const v = parseFloat(row.querySelector('.param-input').value);
            return isNaN(v) || v <= 0;
        });
        if (badParam) { alert('「固定份数 / 固定金额」必须填写大于 0 的数值'); return; }
    }
    data.tier_rules = serializeTierRules();
```

- [ ] **Step 5: 手动走查验证**

启动应用并登录（`python app.py`，浏览器开 `http://127.0.0.1:8765`）。在「配置」页：
1. 挂单模式选「网关式单档」→ 断层四参数出现、「多档挂单规则」区块隐藏；选回「多档做市」→ 反之。Expected：切换即时生效。
2. 止损方式选「关闭止损」→ 比例/固定框都隐藏。
3. 止盈方式选「浮盈市价卖」，填断层参数（如 10/5/20/0），保存 → 弹「策略参数已保存」。
4. 刷新页面重进配置页 → 四参数、两个下拉、止损「关闭」均回显为刚存的值。Expected：持久化正确。

- [ ] **Step 6: 提交**

```bash
git add web/templates/config.html
git commit -m "feat(config-ui): 挂单模式/止盈方式下拉 + 断层参数 + 关止损项"
```

---

## Task 8（可选，最后增量）: 梯队预演对 gap_single 的诚实提示

**Files:**
- Modify: `web/routes.py`（`/api/markets/<id>/ladder` 响应加 `placement_mode`）
- Modify: `web/templates/markets.html`（`renderLadder` 对 gap_single 显示提示而非误导性多档预演）

**Interfaces:**
- Consumes: `db.get_template_for(addr)`（该路由已取 `tmpl`）。
- Produces: gap_single 模板打开预演时显示「本模式每市场只挂一单，梯队预演仅适用于多档模式」提示。

> 说明：完整的单档预演（逐档标注命中/跳过）留作未来增量；本任务只避免误导。核心（Task 1-7）不依赖它，可在需要时再做。

- [ ] **Step 1: 路由响应加 placement_mode**

`web/routes.py` 的 ladder 路由 `return jsonify({...})`（约 1085-1088 行）里加一个键：

```python
            "placement_mode": tmpl.get("placement_mode", "laddering"),
```

- [ ] **Step 2: 前端 renderLadder 早返回提示**

`web/templates/markets.html` `function renderLadder(data) {` 之后第一行插入：

```javascript
    if (data.placement_mode === 'gap_single') {
        return '<span class="metric-note">此模板为「网关式单档」模式：每市场按断层分级只挂一单，梯队预演仅适用于多档模式（此处仅供参考盘口）。</span>';
    }
```

- [ ] **Step 3: 手动验证**

配置页把默认模板设为 gap_single 并保存；「市场发现」页展开任一市场 → 显示上述提示而非多档表格。切回 laddering → 恢复正常预演。

- [ ] **Step 4: 提交**

```bash
git add web/routes.py web/templates/markets.html
git commit -m "feat(markets): 梯队预演对 gap_single 模式给诚实提示"
```

---

## 收尾验证

- [ ] **全量测试**：`pytest -q` → 全绿（既有 + 新增，无回归）。
- [ ] **验收标准（对照 spec）**：
  1. 模板设 `placement_mode=gap_single` → 每市场至多一单、断层分级与顺延正确；默认模板行为不变。
  2. 该模板设 `take_profit_mode=market` + `stop_loss_mode=off` → 浮盈市价清仓、套牢挂成本、无 B0。
  3. 配置页可编辑并持久化全部新键；`maker`/`laddering` 组合与现状一致。
  4. 新增测试通过，既有测试全绿。
- [ ] **完成分支**：交给 `superpowers:finishing-a-development-branch` 决定合并/PR。

## Self-Review 记录

- **Spec 覆盖**：A→Task1/2/5；B→Task2/5；C→Task3/6；D→Task4/7；预演（次要）→Task8；风险留档→spec + Task7 UI 提示。全覆盖。
- **占位符**：无 TBD/TODO；每步含实际代码与命令。
- **类型一致**：`plan_gap_single_order`/`compute_market_single_orders`/`plan_exit(..., take_profit_mode=)`/`effective_theta_stop(...)->None` 在各任务签名一致；manager/monitor 调用参数与定义对齐。
