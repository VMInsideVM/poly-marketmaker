# 断层单档「判定不挂」详细价格依据 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 历史里 `gap_skip`（断层单档·判定不挂）行的「价格依据/来源」列，从一句通用话改成展开驱动"不挂"的逐档盘口证据。

**Architecture:** 三处小改，全在纯函数与其单一调用点。① 给 `explain_gap_single_order` 决策 dict 补记 `gate_min`（仅规则1 有值，不改任何返回值）；② 重写 `gap_single_price_basis` 的**跳过分支**（现在只回退通用串），按决策 dict 已有的 `levels/max_gap/high_sum/min_coeff/gate_passed/gate_min` 展开逐档证据；③ `manager._maybe_record_gap_skip` 改调 `gap_single_price_basis`，不再手写通用串。挂单成功路径、「原因」列、去重口径、前端 `history.html` 全部不动。

**Tech Stack:** Python，pytest（纯逻辑、不触网）。

## Global Constraints

- 用户可见字符串一律**简体中文**，保持现状措辞风格。
- 数值格式沿用现有：价格 `:.4f`、系数/门槛/断层 `:g`（`gap_single_reason` 同款）。
- 测试放 `tests/`（纯逻辑、无网络、无 DB）。
- TDD：先写失败测试→跑红→最小实现→跑绿→提交。
- 提交只 stage 本任务文件，**不**卷入用户未提交的 WIP（`git add` 具体路径，勿 `git add -A`）。
- 不改前端 `web/templates/history.html`（它 `escapeHtml(a.price_basis)` 原样渲染、自动换行）。
- 不动挂单成功（`action=="place"`）的价格依据、不动「原因」列、不动 `gap_skip` 去重。

---

### Task 1: 决策 dict 补记 `gate_min`

**Files:**
- Modify: `engine/laddering.py`（`explain_gap_single_order`：init dict + 规则1 分支）
- Test: `tests/test_gap_single.py`

**Interfaces:**
- Produces: `explain_gap_single_order(...)` 返回 dict 新增键 `gate_min`：规则1 决策 = `gap_high_coeff_sum_min`（传入的高位系数和门槛），规则2/3 及区间内无买档 = `None`。不改其余任何键与 `plan_gap_single_order` 薄壳的返回值。

- [ ] **Step 1: 写失败测试**

在 `tests/test_gap_single.py` 里 `test_explain_no_in_range_rule_none`（约 234 行）后面加：

```python
def test_explain_rule1_carries_gate_min():
    # 规则1 决策带 gate_min(=高位系数和门槛),供价格依据自解释门槛数值。
    d = _explain([_b(0.28, 30), _b(0.27, 20), _b(0.15, 400)], gate=25)
    assert d["rule"] == 1
    assert d["gate_min"] == 25


def test_explain_non_rule1_gate_min_none():
    d = _explain([_b(0.28, 50), _b(0.21, 400)])  # 断层7¢ -> 规则2
    assert d["rule"] == 2
    assert d["gate_min"] is None
    d3 = _explain([_b(0.28, 50), _b(0.27, 40)])  # 断层1¢ -> 规则3
    assert d3["rule"] == 3
    assert d3["gate_min"] is None
    dn = _explain([_b(0.05, 500)])  # 区间外 -> rule None
    assert dn["gate_min"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_gap_single.py::test_explain_rule1_carries_gate_min -v`
Expected: FAIL（`KeyError: 'gate_min'`）

- [ ] **Step 3: 最小实现**

在 `engine/laddering.py` `explain_gap_single_order` 的 init dict 里，`"high_sum": None,` 一行后加 `"gate_min": None,`：

```python
        "high_sum": None,
        "gate_min": None,
        "gate_passed": False,
```

在规则1 分支（`if max_gap > gap_wide_cents:`）里，`d["rule"], d["min_coeff"], d["high_sum"] = 1, min_coeff, high_sum` 一行后加一行：

```python
        d["rule"], d["min_coeff"], d["high_sum"] = 1, min_coeff, high_sum
        d["gate_min"] = gap_high_coeff_sum_min
        if high_sum < gap_high_coeff_sum_min:
```

- [ ] **Step 4: 跑测试确认通过 + 无回归**

Run: `pytest tests/test_gap_single.py -v`
Expected: 新增 2 条 PASS，既有 gap_single 全部 PASS（新增 dict 键不影响任何返回值）。

- [ ] **Step 5: 提交**

```bash
git add engine/laddering.py tests/test_gap_single.py
git commit -m "feat(gap_single): explain 决策补记 gate_min(规则1 高位系数和门槛)"
```

---

### Task 2: `gap_single_price_basis` 跳过分支展开逐档证据

**Files:**
- Modify: `engine/laddering.py`（`gap_single_price_basis` 跳过分支 + docstring）
- Test: `tests/test_gap_single.py`

**Interfaces:**
- Consumes: 决策 dict 的 `levels`（每档 `price/size/coeff/high_side`）、`max_gap`、`rule`、`high_sum`、`min_coeff`、`gate_passed`、`gate_min`（Task 1）。
- Produces: `gap_single_price_basis(d, reward_range_min, reward_range_max)` 对跳过决策返回详细串：区间内有买档 → 逐档 `价×量→系数[高位]` + 断层分级 + 闸门/门槛判定 + 公共尾巴；区间内无买档（`levels` 空）→ 简洁串「…内无可评估买档;来源:CLOB get_orderbook」。挂单成功分支不变。

- [ ] **Step 1: 写失败测试**

在 `tests/test_gap_single.py` 里，把现有 `test_price_basis_skip_is_minimal`（约 293 行）**整体替换**为下面三条（改名 + 新增）：

```python
def test_price_basis_skip_no_in_range_is_minimal():
    # 区间内无买档(全在区间外):无档可枚举,退化简洁版。
    d = _explain([_b(0.05, 500), _b(0.04, 500)])
    b = gap_single_price_basis(d, 0.10, 0.31)
    assert "无可评估买档" in b
    assert "get_orderbook" in b
    assert "档价" not in b
    assert "→系数" not in b


def test_price_basis_skip_rule1_high_sum_shows_evidence():
    # 规则1 高位系数和不足:逐档证据 + 断层 + 高位系数和 + 门槛。
    d = _explain([_b(0.28, 30), _b(0.27, 20), _b(0.15, 400)])  # gate 默认 20
    assert d["action"] == "skip" and d["rule"] == 1
    b = gap_single_price_basis(d, 0.10, 0.31)
    assert "0.2800×30→系数" in b
    assert "[高位]" in b
    assert "最大断层" in b
    assert "高位系数和" in b
    assert "门槛20" in b
    assert "整市场不挂" in b
    assert "get_orderbook" in b


def test_price_basis_skip_no_coeff_shows_evidence():
    # 顺延无档过门槛(规则3):逐档系数 + 门槛。
    d = _explain([_b(0.28, 50), _b(0.27, 40)], x3=100)
    assert d["action"] == "skip" and d["rule"] == 3
    b = gap_single_price_basis(d, 0.10, 0.31)
    assert "0.2800×50→系数" in b
    assert "各档系数均 ≤ 选档门槛100" in b
    assert "get_orderbook" in b
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_gap_single.py::test_price_basis_skip_rule1_high_sum_shows_evidence tests/test_gap_single.py::test_price_basis_skip_no_coeff_shows_evidence -v`
Expected: FAIL（当前跳过分支只返回通用 `src`，不含逐档/断层/门槛）

- [ ] **Step 3: 实现**

在 `engine/laddering.py` `gap_single_price_basis` 里，把跳过分支这两行：

```python
    if d.get("action") != "place" or d.get("chosen_index") is None:
        return src
```

替换为：

```python
    if d.get("action") != "place" or d.get("chosen_index") is None:
        # 跳过:展开逐档盘口证据(完整逐档),供历史「价格依据/来源」不点代码即可复盘。
        levels = d.get("levels") or []
        if not levels:
            # 无簿/最低份数≤0/全在区间外:无档可枚举,退化简洁版(两类区别在「原因」列)。
            return (
                f"奖励区间[{reward_range_min:.4f},{reward_range_max:.4f}]内无可评估买档;"
                f"来源:CLOB get_orderbook"
            )
        per = " · ".join(
            f"{lv['price']:.4f}×{lv['size']:g}→系数{lv['coeff']:g}"
            + ("[高位]" if lv.get("high_side") else "")
            for lv in levels
        )
        parts = [
            f"区间内买档(价降序):{per}",
            f"最大断层 {d['max_gap']:g}¢→{_GAP_RULE_LABEL.get(d['rule'], '')}",
        ]
        if d.get("rule") == 1 and not d.get("gate_passed"):
            addends = "+".join(
                f"{lv['coeff']:g}" for lv in levels if lv.get("high_side")
            )
            parts.append(
                f"高位系数和 {addends}={d['high_sum']:g} < 门槛{d['gate_min']:g}"
                f" → 整市场不挂"
            )
        else:
            if d.get("rule") == 1 and d.get("high_sum") is not None:
                parts.append(f"高位系数和{d['high_sum']:g}(过闸)")
            parts.append(f"各档系数均 ≤ 选档门槛{d['min_coeff']:g}")
        parts.append("系数=挂量÷(最低份数×金额数值)")
        parts.append(src)
        return ";".join(parts)
```

并把函数 docstring 更新为（原「挂单的价格依据…」一行）：

```python
    """价格依据/来源串。挂单:选中档价 + 系数构成 + 断层分级 + 数据来源;
    跳过:区间内有买档时逐档展开(价×量→系数[高位]) + 断层分级 + 闸门/门槛判定,
    区间内无买档时退化简洁版。"""
```

- [ ] **Step 4: 跑测试确认通过 + 无回归**

Run: `pytest tests/test_gap_single.py -v`
Expected: 三条跳过用例 PASS；`test_price_basis_placed_has_coeff_and_source`（挂单分支）仍 PASS。

- [ ] **Step 5: 提交**

```bash
git add engine/laddering.py tests/test_gap_single.py
git commit -m "feat(gap_single): 判定不挂的价格依据展开逐档盘口证据"
```

---

### Task 3: manager 记账改用 `gap_single_price_basis`

**Files:**
- Modify: `engine/manager.py`（`_maybe_record_gap_skip`）
- Test: `tests/test_place_orders.py`（`test_gap_single_skip_records_reason_and_dedups`）

**Interfaces:**
- Consumes: `gap_single_price_basis(decision, reward_range_min, reward_range_max)`（Task 2）。
- Produces: `gap_skip` 动作的 `price_basis` 即上函数输出（详细逐档/简洁版）；`reason`（`skip_reason`）、`price=-1`/`size=0`、按 token 去重全不变。

- [ ] **Step 1: 写失败测试**

在 `tests/test_place_orders.py` `test_gap_single_skip_records_reason_and_dedups`（约 436 行）里，`assert "规则3" in skips[0].kwargs["reason"]` 之后加：

```python
    basis = skips[0].kwargs["price_basis"]
    assert "区间内买档" in basis
    assert "各档系数均 ≤ 选档门槛100" in basis
    assert "get_orderbook" in basis
    assert "断层单档判定不挂" not in basis  # 旧通用串已弃用
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_place_orders.py::test_gap_single_skip_records_reason_and_dedups -v`
Expected: FAIL（现在 `price_basis` 是手写通用串「断层单档判定不挂（…）；…」，不含逐档证据）

- [ ] **Step 3: 实现**

在 `engine/manager.py` `_maybe_record_gap_skip` 方法体开头加局部 import（其余方法用同款局部 import 风格），并把 `record_action(...)` 里的 `price_basis=(...)` 块换成调用：

方法开头（`token_id = side["token_id"]` 之前）加：

```python
        from engine.laddering import gap_single_price_basis
```

把这段：

```python
                price_basis=(
                    f"断层单档判定不挂（{side.get('outcome','')}）；"
                    f"奖励区间[{side['reward_range_min']:.4f},{side['reward_range_max']:.4f}]；"
                    f"来源：CLOB get_orderbook"
                ),
```

换成：

```python
                price_basis=gap_single_price_basis(
                    decision,
                    side["reward_range_min"],
                    side["reward_range_max"],
                ),
```

- [ ] **Step 4: 跑测试确认通过 + 无回归**

Run: `pytest tests/test_place_orders.py -v`
Expected: `test_gap_single_skip_records_reason_and_dedups` PASS（含新断言、去重仍成立）；`test_gap_single_place_buy_records_rule_reason`（挂单路径）仍 PASS。

- [ ] **Step 5: 全量回归 + 提交**

Run: `pytest`
Expected: 全绿。

```bash
git add engine/manager.py tests/test_place_orders.py
git commit -m "feat(gap_single): gap_skip 记账改用 gap_single_price_basis 展开逐档证据"
```

---

## Self-Review

**1. Spec coverage：**
- 完整逐档证据（区间内有买档）→ Task 2 Step 3 `per` 逐档 + 断层 + 闸门/门槛 ✓
- 区间内无买档退化简洁版 → Task 2 Step 3 `if not levels` 分支 ✓
- 门槛值来源 `gate_min` → Task 1 ✓
- `_maybe_record_gap_skip` 改调 → Task 3 ✓
- 去重口径不变 → Task 3 只换 `price_basis`，`_last_gap_skip`/`reason` 未动 ✓
- 挂单成功价格依据、「原因」列、前端不动 → 三个 Task 均未触及 ✓
- 测试：`test_price_basis_skip_is_minimal` 正名 + 两新增 + `gate_min` 断言 + manager 断言 → Task 1/2/3 ✓

**2. Placeholder scan：** 无 TBD/TODO/「add error handling」；每步含实际代码/命令/期望输出。✓

**3. Type consistency：** `gate_min` 在 Task 1 产出、Task 2 消费，命名一致；`gap_single_price_basis(d, reward_range_min, reward_range_max)` 签名不变、Task 3 按位置传 `decision, side["reward_range_min"], side["reward_range_max"]` 一致；`_GAP_RULE_LABEL`/`high_sum`/`min_coeff`/`gate_passed` 均为决策 dict 现有键。✓
