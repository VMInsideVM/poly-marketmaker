# 三级各自的高位系数和闸门 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让规则2、规则3 也各有一道「断层上方到买一的厚度」市场闸门，默认关闭，规则1 行为一字不改。

**Architecture:** `explain_gap_single_order` 里三级归级后统一算 `high_sum` 并与该级门槛比对（现在只有规则1 算）。两个新门槛作为**关键字参数加在签名末尾**，默认 `0.0`，透传层逐层补上。门槛为 `0` 时 `high_sum < 0` 永不成立，等于这道闸不存在。

**Tech Stack:** Python 3.12 + pytest；前端是 Jinja 模板里的原生 JS。

## Global Constraints

- 设计依据：`docs/superpowers/specs/2026-08-14-per-rule-high-coeff-gate-design.md`。
- 规则1 的键名 `gap_high_coeff_sum_min` **保留不改**，不做重命名迁移。
- 新键 `rule2_high_coeff_sum_min` / `rule3_high_coeff_sum_min`，默认 `0`，`0` = 不拦。
- 判闸口径沿用规则1：`high_sum < 门槛` 才拦，`== 门槛` 放行。
- 新参数一律加在函数签名**末尾**并带默认值。`engine/laddering.py` 的现有调用方（测试夹具 `_plan`、`manager.place_orders`、`routes` 预览）大量使用**位置参数**，插在中间会让每个调用点静默错位。
- 「高位」定义三级共用：`levels[0 .. split_idx]`，即最大断层上方到买一的全部买档（全簿口径，含奖励区间外的档）。
- 中文 UI 字符串保持简体中文。前端模板文件由主 agent 直接写，写后跑 `node --check`（抽出 script 块）并检查 BOM。
- 每个任务结束时全量 `pytest` 必须绿（当前基线 996 passed）。

---

### Task 1: 档位模块接受两个新键

**Files:**
- Modify: `engine/tiers.py:7-12`
- Test: `tests/test_tiers.py`

**Interfaces:**
- Consumes: 无
- Produces: `validate_size_tiers` 归一化后的 tier dict 含 `rule2_high_coeff_sum_min` / `rule3_high_coeff_sum_min`（float，缺省 0.0，负数报错）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_tiers.py` 末尾：

```python
def test_validate_accepts_rule2_rule3_high_coeff_gates():
    # 规则2/3 的高位系数和门槛与规则1 的 gap_high_coeff_sum_min 同级,逐档配置。
    out, err = validate_size_tiers(
        [
            {
                "size": 20,
                "shares": 20,
                "gap_high_coeff_sum_min": 20,
                "rule2_high_coeff_sum_min": 8,
                "rule3_high_coeff_sum_min": 3,
            }
        ]
    )
    assert err is None
    assert out[0]["rule2_high_coeff_sum_min"] == 8.0
    assert out[0]["rule3_high_coeff_sum_min"] == 3.0


def test_validate_rule2_rule3_gates_default_zero():
    # 缺省 = 0 = 不拦,老配置升级后行为不变。
    out, err = validate_size_tiers([{"size": 20, "shares": 20}])
    assert err is None
    assert out[0]["rule2_high_coeff_sum_min"] == 0.0
    assert out[0]["rule3_high_coeff_sum_min"] == 0.0


def test_validate_rejects_negative_rule2_gate():
    out, err = validate_size_tiers(
        [{"size": 20, "shares": 20, "rule2_high_coeff_sum_min": -1}]
    )
    assert out is None
    assert "rule2_high_coeff_sum_min" in err
```

先确认 `tests/test_tiers.py` 顶部已 `from engine.tiers import validate_size_tiers`；若导入形式不同，按该文件既有写法调整。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_tiers.py -q`
Expected: 3 条 FAIL（前两条 `KeyError`，第三条 `err` 为 `None`）

- [ ] **Step 3: 实现**

`engine/tiers.py` 把 `_TIER_COEFF_KEYS` 改成：

```python
# 逐档配置的系数类参数(统一按 float 归一化、不得为负)。
# 注意 gap_high_coeff_sum_min 就是「规则1」的高位系数和门槛,名字里没有 rule1:
# 它早于规则2/3 的门槛存在,已带值跑在生产配置的 size_tiers JSON 里,改名要写迁移,
# 而迁移写漏会让闸门静默变成 0(不拦),代价比命名不对称大得多。
_TIER_COEFF_KEYS = (
    "rule1_min_coeff",
    "rule2_min_coeff",
    "rule3_min_coeff",
    "gap_high_coeff_sum_min",
    "rule2_high_coeff_sum_min",
    "rule3_high_coeff_sum_min",
)
```

`validate_size_tiers` 里遍历 `_TIER_COEFF_KEYS` 的循环不用动，它已经做了 float 转换、缺省 0、负数报错。

- [ ] **Step 4: 补一条路由往返测试**

`tests/test_settings_routes.py` 的 `_tier_payload()`（约 96 行）加两个键：

```python
        "gap_high_coeff_sum_min": 20,
        "rule2_high_coeff_sum_min": 8,
        "rule3_high_coeff_sum_min": 3,
```

并在 `test_size_tiers_roundtrip_via_template_put`（约 111 行）的断言后补一行，确认新键真的存得进、读得出：

```python
    assert saved[0]["rule2_high_coeff_sum_min"] == 8
    assert saved[0]["rule3_high_coeff_sum_min"] == 3
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_tiers.py tests/test_settings_routes.py -q`
Expected: PASS

Run: `python -m pytest tests/ -q`
Expected: 996+3 passed

- [ ] **Step 6: 提交**

```bash
git add engine/tiers.py tests/test_tiers.py tests/test_settings_routes.py
git commit -m "feat(tiers): 档位模块接受规则2/3 的高位系数和门槛(默认0=不拦)"
```

---

### Task 2: 三级统一判闸

**Files:**
- Modify: `engine/laddering.py`（`explain_gap_single_order` 的归级段 + `plan_gap_single_order` 薄壳）
- Test: `tests/test_gap_single.py`

**Interfaces:**
- Consumes: 无（纯函数）
- Produces:
  - `explain_gap_single_order(bids, reward_range_min, reward_range_max, min_size, amount_value_table, gap_wide_cents, gap_mid_cents, gap_high_coeff_sum_min, rule1_min_coeff, rule2_min_coeff, rule3_min_coeff, cliff_probe_cents=0, shares=None, rule2_high_coeff_sum_min=0.0, rule3_high_coeff_sum_min=0.0)`
  - `plan_gap_single_order(...)` 同样两个末尾 kwarg
  - 决策 dict 的 `high_sum` / `gate_min` 三级都有值（不再是规则2/3 为 `None`）

- [ ] **Step 1: 写失败测试**

先把 `tests/test_gap_single.py` 的两个夹具加上新参数（`_plan` 在文件约 16 行、`_explain` 在约 143 行）：

```python
def _plan(
    bids,
    rmin=0.10,
    rmax=0.31,
    min_size=20,
    av=None,
    wide=10,
    mid=5,
    gate=20,
    x1=0,
    x2=0,
    x3=0,
    cliff=0,
    shares=None,
    gate2=0,
    gate3=0,
):
    return plan_gap_single_order(
        bids,
        rmin,
        rmax,
        min_size,
        AV if av is None else av,
        wide,
        mid,
        gate,
        x1,
        x2,
        x3,
        cliff_probe_cents=cliff,
        shares=shares,
        rule2_high_coeff_sum_min=gate2,
        rule3_high_coeff_sum_min=gate3,
    )
```

```python
def _explain(
    bids,
    rmin=0.10,
    rmax=0.31,
    min_size=20,
    av=None,
    wide=10,
    mid=5,
    gate=20,
    x1=0,
    x2=0,
    x3=0,
    gate2=0,
    gate3=0,
):
    return explain_gap_single_order(
        bids,
        rmin,
        rmax,
        min_size,
        AV if av is None else av,
        wide,
        mid,
        gate,
        x1,
        x2,
        x3,
        rule2_high_coeff_sum_min=gate2,
        rule3_high_coeff_sum_min=gate3,
    )
```

改既有测试 `test_explain_non_rule1_gate_min_none`（约 254 行），它断言的是旧语义：

```python
def test_explain_gate_min_present_on_every_rule():
    # 三级都带 gate_min/high_sum(旧版只有规则1 有,规则2/3 是 None)。
    d = _explain([_b(0.28, 50), _b(0.21, 400)], gate2=7)  # 断层7¢ -> 规则2
    assert d["rule"] == 2
    assert d["gate_min"] == 7
    assert d["high_sum"] is not None
    d3 = _explain([_b(0.28, 50), _b(0.27, 40)], gate3=2)  # 断层1¢ -> 规则3
    assert d3["rule"] == 3
    assert d3["gate_min"] == 2
    assert d3["high_sum"] is not None
    dn = _explain([_b(0.05, 500)])  # 区间内无买档 -> rule None
    assert dn["gate_min"] is None
```

追加新用例到 `tests/test_gap_single.py` 末尾：

```python
# --- 规则2/3 各自的高位系数和闸门(2026-08-14) -------------------------------


def test_rule2_gate_blocks_when_high_sum_below():
    # 断层 7¢ -> 规则2。高位={0.28} 系数 50/(20*2)=1.25 < 门槛 5 -> 整市场不挂。
    assert _plan([_b(0.28, 50), _b(0.21, 400)], gate2=5) is None


def test_rule2_gate_passes_at_exactly_threshold():
    # 高位系数和恰等门槛 -> 放行(与规则1 同约定:< 才拦)。
    assert _plan([_b(0.28, 50), _b(0.21, 400)], gate2=1.25) == (0.28, 20)


def test_rule2_gate_default_zero_never_blocks():
    # 默认 0:高位再薄也放行,升级零回归。
    assert _plan([_b(0.28, 1), _b(0.21, 400)]) == (0.28, 20)


def test_rule3_gate_blocks_when_high_sum_below():
    # 断层 1¢ -> 规则3。高位={0.28} 系数 1.25 < 门槛 3 -> 不挂。
    assert _plan([_b(0.28, 50), _b(0.27, 40)], gate3=3) is None


def test_rule3_gate_passes_at_exactly_threshold():
    assert _plan([_b(0.28, 50), _b(0.27, 40)], gate3=1.25) == (0.28, 20)


def test_rule3_gate_default_zero_never_blocks():
    assert _plan([_b(0.28, 1), _b(0.27, 40)]) == (0.28, 20)


def test_gates_do_not_cross_between_rules():
    # 规则2 的市场(断层7¢)只认 gate2:gate/gate3 抬到 999 也不该拦它。
    assert _plan([_b(0.28, 50), _b(0.21, 400)], gate=999, gate3=999) == (0.28, 20)
    # 规则3 的市场(断层1¢)只认 gate3。
    assert _plan([_b(0.28, 50), _b(0.27, 40)], gate=999, gate2=999) == (0.28, 20)
    # 规则1 的市场(断层12¢)只认 gate。
    bids1 = [_b(0.28, 50), _b(0.27, 800), _b(0.15, 400)]
    assert _plan(bids1, gate2=999, gate3=999) == (0.28, 20)


def test_rule1_gate_behavior_unchanged():
    # 回归:规则1 的闸门与默认 20 一字不动。
    assert _plan([_b(0.28, 50), _b(0.27, 30), _b(0.15, 400)]) is None
    assert _plan([_b(0.28, 50), _b(0.27, 800), _b(0.15, 400)]) == (0.28, 20)


def test_rule2_gate_skip_reason_names_the_rule():
    d = _explain([_b(0.28, 50), _b(0.21, 400)], gate2=5)
    assert d["action"] == "skip"
    assert "规则2" in d["skip_reason"]
    assert "高位系数和" in d["skip_reason"]
    assert "整市场不挂" in d["skip_reason"]


def test_single_level_book_gate_uses_that_level():
    # 边界(spec 明确):全簿只有一档 -> max_gap=0 -> 规则3,高位就是那唯一一档。
    # 系数 50/(20*2)=1.25:门槛 2 拦下,门槛 1 放行。
    assert _plan([_b(0.28, 50)], gate3=2) is None
    assert _plan([_b(0.28, 50)], gate3=1) == (0.28, 20)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_gap_single.py -q`
Expected: FAIL。新用例报 `TypeError: ... unexpected keyword argument 'rule2_high_coeff_sum_min'`；`test_explain_gate_min_present_on_every_rule` 报 `assert None == 7`。

- [ ] **Step 3: 实现**

`engine/laddering.py` 的 `explain_gap_single_order` 签名末尾加两个参数：

```python
def explain_gap_single_order(
    bids,
    reward_range_min,
    reward_range_max,
    min_size,
    amount_value_table,
    gap_wide_cents,
    gap_mid_cents,
    gap_high_coeff_sum_min,
    rule1_min_coeff,
    rule2_min_coeff,
    rule3_min_coeff,
    cliff_probe_cents=0,
    shares=None,
    rule2_high_coeff_sum_min=0.0,
    rule3_high_coeff_sum_min=0.0,
):
```

新参数**必须加在末尾**：现有调用方按位置传前 11 个参数，插在中间会静默错位。`gap_high_coeff_sum_min` 是规则1 的门槛（历史键名，见 Task 1 注释）。

把归级那段（现为 `if max_gap > gap_wide_cents:` 到 `d["rule"], d["min_coeff"], d["gate_passed"] = 3, min_coeff, True`）整段替换成：

```python
    # 按最大价差归级,取该级的选档门槛与高位系数和门槛;三级结构完全一致。
    # 高位系数和闸门原先只有规则1 有,现在三级各一个(规则2/3 默认 0 = 不拦)。
    if max_gap > gap_wide_cents:
        rule, min_coeff, gate_min = 1, rule1_min_coeff, gap_high_coeff_sum_min
    elif max_gap >= gap_mid_cents:
        rule, min_coeff, gate_min = 2, rule2_min_coeff, rule2_high_coeff_sum_min
    else:
        rule, min_coeff, gate_min = 3, rule3_min_coeff, rule3_high_coeff_sum_min
    # 高位 = 最大断层上方到买一的全部买档(全簿口径,含奖励区间外的档)。
    high_sum = sum(lv["coeff"] for lv in levels[: split_idx + 1])
    d["rule"], d["min_coeff"] = rule, min_coeff
    d["high_sum"], d["gate_min"] = high_sum, gate_min
    if high_sum < gate_min:
        d["skip_reason"] = (
            f"{_GAP_RULE_LABEL[rule].rstrip(')')},最大断层{max_gap:g}¢):"
            f"高位系数和 {high_sum:g} < 门槛 {gate_min:g} → 整市场不挂"
        )
        return d
    d["gate_passed"] = True
```

`_GAP_RULE_LABEL[rule].rstrip(')')` 把 `"规则1(宽断层)"` 变成 `"规则1(宽断层"`，拼出与原来一字不差的规则1 文案 `规则1(宽断层,最大断层12¢):高位系数和 X < 门槛 Y → 整市场不挂`。`_GAP_RULE_LABEL` 定义在文件下方，Python 运行时才解析函数体，前向引用没问题。

`plan_gap_single_order` 加同样两个末尾 kwarg 并透传：

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
    rule1_min_coeff,
    rule2_min_coeff,
    rule3_min_coeff,
    cliff_probe_cents=0,
    shares=None,
    rule2_high_coeff_sum_min=0.0,
    rule3_high_coeff_sum_min=0.0,
):
```

函数体里调 `explain_gap_single_order` 时补：

```python
        cliff_probe_cents=cliff_probe_cents,
        shares=shares,
        rule2_high_coeff_sum_min=rule2_high_coeff_sum_min,
        rule3_high_coeff_sum_min=rule3_high_coeff_sum_min,
    )
```

同时更新 `explain_gap_single_order` docstring 里这两行：

```
      high_sum: 该级高位系数和(断层上方到买一);rule=None 时 None
      gate_min: 该级高位系数和门槛(规则1=gap_high_coeff_sum_min,规则2/3 各自的键);rule=None 为 None
```

以及归级口径那段末尾补一句：

```
    三级各有一道高位系数和闸门(规则2/3 默认 0 = 不拦);< 门槛才拦,== 放行。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_gap_single.py -q`
Expected: PASS

Run: `python -m pytest tests/ -q`
Expected: 全绿。若 `tests/test_place_orders.py` 或 `tests/test_markets_route.py` 有红，多半是夹具模板没带新键而默认 0 不该拦，属真回归，读报错定位后修。

- [ ] **Step 5: 提交**

```bash
git add engine/laddering.py tests/test_gap_single.py
git commit -m "feat(laddering): 三级各自的高位系数和闸门(规则2/3 默认0=不拦)"
```

---

### Task 3: 原因与价格依据文案覆盖三级

**Files:**
- Modify: `engine/laddering.py`（`gap_single_reason`、`gap_single_price_basis`）
- Test: `tests/test_gap_single.py`

**Interfaces:**
- Consumes: Task 2 的决策 dict（`high_sum` / `gate_min` 三级都有值）
- Produces: 文案函数签名不变，仅输出内容随规则变化

**口径**：闸门**启用时**（`gate_min > 0`）才在文案里出现「高位系数和…(过闸)」。门槛为 0 说明这道闸没开，写出来是噪音，且能保证默认配置下规则2/3 的文案与现在完全一致。规则1 默认 20 > 0，文案不变。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_gap_single.py` 末尾：

```python
def test_reason_rule2_shows_high_sum_when_gate_enabled():
    # 规则2 闸门开着且过闸 -> 原因里带高位系数和(旧版只有规则1 带)。
    d = _explain([_b(0.28, 50), _b(0.21, 400)], gate2=1)
    r = gap_single_reason(d)
    assert "规则2(中断层)" in r
    assert "高位系数和1.25(过闸)" in r


def test_reason_rule2_omits_high_sum_when_gate_off():
    # 门槛 0 = 闸没开 -> 不写高位系数和,默认配置文案零回归。
    d = _explain([_b(0.28, 50), _b(0.21, 400)])
    r = gap_single_reason(d)
    assert "高位系数和" not in r


def test_price_basis_rule2_gate_fail_shows_addends():
    # 规则2 被闸门拦下:逐档证据 + 高位系数和加数展开 + 门槛。
    d = _explain([_b(0.28, 50), _b(0.21, 400)], gate2=5)
    b = gap_single_price_basis(d, 0.10, 0.31)
    assert "规则2(中断层)" in b
    assert "高位系数和 1.25=1.25 < 门槛5" in b
    assert "整市场不挂" in b


def test_price_basis_rule3_gate_fail_shows_addends():
    d = _explain([_b(0.28, 50), _b(0.27, 40)], gate3=3)
    b = gap_single_price_basis(d, 0.10, 0.31)
    assert "规则3(密盘)" in b
    assert "< 门槛3" in b
    assert "整市场不挂" in b
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_gap_single.py -q -k "rule2_shows_high_sum or rule2_omits or rule2_gate_fail_shows or rule3_gate_fail_shows"`
Expected: FAIL（文案里没有高位系数和，因为两个函数仍写死 `rule == 1`）

- [ ] **Step 3: 实现**

`gap_single_reason` 里把

```python
    if d["rule"] == 1 and d.get("high_sum") is not None:
```

改成

```python
    # 三级都有高位系数和闸门,但门槛为 0 = 没开这道闸,写出来只是噪音。
    if d.get("gate_min") and d.get("high_sum") is not None:
```

`gap_single_price_basis` 的 skip 分支里，把

```python
        if d.get("rule") == 1 and not d.get("gate_passed"):
```

改成

```python
        if not d.get("gate_passed"):
```

（走到这里 `levels` 非空，`rule` 必有值：`rule=None` 和悬崖两种情况都在更早处 return 了，所以 `gate_passed=False` 只可能是被闸门拦下。）

同一函数 else 分支里，把

```python
            if d.get("rule") == 1 and d.get("high_sum") is not None:
```

改成

```python
            if d.get("gate_min") and d.get("high_sum") is not None:
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_gap_single.py -q`
Expected: PASS

Run: `python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add engine/laddering.py tests/test_gap_single.py
git commit -m "feat(laddering): 原因/价格依据按三级输出高位系数和(门槛0时不写)"
```

---

### Task 4: 透传层补上两个参数

**Files:**
- Modify: `engine/laddering.py`（`compute_market_single_orders`、`preview_gap_single_market`）
- Modify: `engine/manager.py:341-346` 附近（读 tier）与 `engine/manager.py:415-446`（两处调用）
- Modify: `web/routes.py:1306-1318`
- Test: `tests/test_gap_single.py`、`tests/test_place_orders.py`

**Interfaces:**
- Consumes: Task 2 的 `plan_gap_single_order` / `explain_gap_single_order` 末尾两个 kwarg
- Produces:
  - `compute_market_single_orders(..., cliff_probe_cents=0, shares=None, rule2_high_coeff_sum_min=0.0, rule3_high_coeff_sum_min=0.0)`
  - `preview_gap_single_market(..., cliff_probe_cents=0, shares=None, rule2_high_coeff_sum_min=0.0, rule3_high_coeff_sum_min=0.0)`
  - `manager.place_orders` 从 tier 读 `rule2_high_coeff_sum_min` / `rule3_high_coeff_sum_min`（缺省 0）并传给上述两者及 `explain_gap_single_order`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_gap_single.py` 末尾：

```python
def test_compute_threads_rule2_gate():
    # compute 必须把规则2 门槛透下去,否则整层闸门形同虚设。
    side = {
        "bids": [_b(0.28, 50), _b(0.21, 400)],
        "reward_range_min": 0.10,
        "reward_range_max": 0.31,
        "min_size": 20,
    }
    out = compute_market_single_orders(
        side, None, 1000, 500, AV, 10, 5, 20, 0, 0, 0, rule2_high_coeff_sum_min=5
    )
    assert out["a"] == []


def test_preview_threads_rule3_gate():
    side = _pside([_b(0.28, 50), _b(0.27, 40)])
    out = preview_gap_single_market(
        side, None, AV, 10, 5, 20, 0, 0, 0, rule3_high_coeff_sum_min=3
    )
    assert out["a"]["action"] == "skip"
    assert "规则3" in out["a"]["skip_reason"]
```

`_pside` 是该文件已有的预演夹具（约 401 行）。

追加到 `tests/test_place_orders.py` 末尾。该文件已有 `_gap_template()`（约 403 行）返回带一个 size=20 档位的 gap_single 模板，照 `test_gap_single_skip_records_reason_and_dedups` 的写法改档位字段：

```python
def test_place_orders_threads_rule2_high_coeff_gate():
    # 档位模块配了规则2 高位门槛 -> 该市场被闸门拦下,不下单。
    # 断层 0.28->0.21 = 7¢ -> 规则2;高位={0.28} 系数 50/(20*2)=1.25 < 5。
    tmpl = _gap_template()
    tmpl["size_tiers"][0]["rule2_high_coeff_sum_min"] = 5
    worker, api, db = _make_worker(template=tmpl)
    api.get_orderbook.return_value = _ob([(0.28, 50), (0.21, 400)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=20)])
    api.place_limit_buy.assert_not_called()


def test_place_orders_rule2_gate_default_still_places():
    # 同一副盘口,不配门槛(默认0)-> 照常下单,证明上一条是闸门拦的而非别的原因。
    worker, api, db = _make_worker(template=_gap_template())
    api.get_orderbook.return_value = _ob([(0.28, 50), (0.21, 400)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=20)])
    assert api.place_limit_buy.call_count == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_gap_single.py tests/test_place_orders.py -q`
Expected: FAIL（`TypeError: unexpected keyword argument`，以及 place_orders 那条因为参数没透传而实际下了单）

- [ ] **Step 3: 实现**

`engine/laddering.py` 的 `compute_market_single_orders` 签名末尾加：

```python
    cliff_probe_cents=0,
    shares=None,
    rule2_high_coeff_sum_min=0.0,
    rule3_high_coeff_sum_min=0.0,
):
```

函数体里调 `plan_gap_single_order` 时补两行：

```python
            cliff_probe_cents=cliff_probe_cents,
            shares=shares,
            rule2_high_coeff_sum_min=rule2_high_coeff_sum_min,
            rule3_high_coeff_sum_min=rule3_high_coeff_sum_min,
        )
```

`preview_gap_single_market` 同样：签名末尾加两个参数，函数体里调 `explain_gap_single_order` 时补两行透传。

`engine/manager.py` 读 tier 那段（现为 `rule3_min_coeff = float(tier.get("rule3_min_coeff", 0))` 之后）加两行：

```python
            rule2_high_gate = float(tier.get("rule2_high_coeff_sum_min", 0))
            rule3_high_gate = float(tier.get("rule3_high_coeff_sum_min", 0))
```

`compute_market_single_orders(...)` 调用处，把 `shares=tier_shares,` 一行改成：

```python
                    shares=tier_shares,
                    rule2_high_coeff_sum_min=rule2_high_gate,
                    rule3_high_coeff_sum_min=rule3_high_gate,
                )
```

同一段下方 `explain_gap_single_order(...)` 调用处（`shares=tier_shares,` 那行）做同样处理。

`web/routes.py` 的 `preview_gap_single_market(...)` 调用处，把 `shares=int(tier.get("shares", 0) or 0) or None,` 一行改成：

```python
            shares=int(tier.get("shares", 0) or 0) or None,
            rule2_high_coeff_sum_min=float(tier.get("rule2_high_coeff_sum_min", 0)),
            rule3_high_coeff_sum_min=float(tier.get("rule3_high_coeff_sum_min", 0)),
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add engine/laddering.py engine/manager.py web/routes.py tests/test_gap_single.py tests/test_place_orders.py
git commit -m "feat: 规则2/3 高位系数和门槛透传到下单与预演"
```

---

### Task 5: 配置页、预演页、帮助页

**Files:**
- Modify: `web/templates/config.html:432`（输入框）、`:445`（新建档位默认值）、`:488`（收集）
- Modify: `web/templates/markets.html`（`renderGapSingle` 的 `gateTxt`）
- Modify: `web/templates/help.html`（「规则1 的额外闸门」一条）
- Test: 手工 `node --check` + 既有路由测试

**Interfaces:**
- Consumes: Task 1 的键名、Task 2 的决策 dict（`gate_min` 三级有值）
- Produces: 用户可在配置页逐档填规则2/3 的门槛

**含中文的前端文件由主 agent 直接用 Write/Edit 写**，不要派给 subagent（历史上 subagent 反复把中文写成形近别字并加 BOM）。

- [ ] **Step 1: 配置页加两个输入框**

`web/templates/config.html` 第 432 行那条 `.st-gate` 之后加两行：

```html
        <div class="form-group"><label>规则2 高位风险系数和门槛（0=不拦）</label><input type="number" class="st-gate2" step="0.1" value="${t.rule2_high_coeff_sum_min ?? 0}"></div>
        <div class="form-group"><label>规则3 高位风险系数和门槛（0=不拦）</label><input type="number" class="st-gate3" step="0.1" value="${t.rule3_high_coeff_sum_min ?? 0}"></div>
```

第 488 行 `gap_high_coeff_sum_min: parseFloat(...)` 之后加两行：

```javascript
            rule2_high_coeff_sum_min: parseFloat(card.querySelector('.st-gate2').value) || 0,
            rule3_high_coeff_sum_min: parseFloat(card.querySelector('.st-gate3').value) || 0,
```

第 119 行的说明文字，把「再查断层上方各档风险系数之和≥门槛，不过则整市场不挂」改成「再查断层上方各档风险系数之和≥门槛，不过则整市场不挂；规则2/3 也各有一道同样的闸，门槛填 0 就是不拦」。

- [ ] **Step 2: 预演页闸门行不再限规则1**

`web/templates/markets.html` 的 `renderGapSingle` 里：

```javascript
        const gateTxt = s.rule === 1
            ? ` · 高位系数和 ${s.high_sum != null ? s.high_sum.toFixed(2) : '—'} ${s.gate_passed ? '≥ 闸门(过闸)' : '< 闸门(拦下)'}`
            : '';
```

改成：

```javascript
        // 三级都可能有高位系数和闸门;门槛为 0 表示这级没开闸,不显示。
        const gateTxt = s.gate_min
            ? ` · 高位系数和 ${s.high_sum != null ? s.high_sum.toFixed(2) : '—'} ${s.gate_passed ? `≥ 闸门${s.gate_min}(过闸)` : `< 闸门${s.gate_min}(拦下)`}`
            : '';
```

`preview_gap_single_market` 返回的 side 里已经有 `gate_min` 字段，无需改后端。

- [ ] **Step 3: 帮助页改写**

`web/templates/help.html` 里「规则1 的额外闸门」那条 `<li>` 整条替换成：

```html
        <li><strong>高位厚度闸门</strong>：选档之前先看「断层上方到买一」这一段的风险系数之和够不够。三级各有自己的门槛（在档位模块里配），不够就整个市场不挂。规则1 默认 20；规则2、规则3 默认 0，0 表示不设这道闸。</li>
```

同一文件第 85 行有一处「再配这一档的三个选档门槛、高位系数和门槛、金额数值表」，把它改成「再配这一档的三个选档门槛、三个高位系数和门槛、金额数值表」。

`help.html` 只有这两处提到「高位」（第 85、100 行），改完这两处即可。

- [ ] **Step 4: 校验前端**

```bash
python -c "
import re
for f in ['web/templates/markets.html','web/templates/config.html','web/templates/help.html']:
    b=open(f,'rb').read()
    assert b[:3]!=b'\xef\xbb\xbf', f+' 有 BOM'
    b.decode('utf-8')
    print(f,'ok')
"
```

抽出 script 块跑 `node --check`：

```bash
python -c "
import re
s=open('web/templates/markets.html',encoding='utf-8').read()
js='\n'.join(re.findall(r'<script[^>]*>(.*?)</script>',s,re.S))
open('_check.js','w',encoding='utf-8').write(js)
" && node --check _check.js && rm _check.js
```

对 `web/templates/config.html` 重复一遍（把文件名换掉）。

Run: `python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add web/templates/config.html web/templates/markets.html web/templates/help.html
git commit -m "feat(ui): 配置页逐档配规则2/3 高位门槛,预演页三级都显示闸门"
```

---

## 收尾

- [ ] 全量 `python -m pytest tests/ -q` 绿
- [ ] `git diff --stat` 复核没有被格式化 hook 卷进无关文件
- [ ] 提醒用户：这不是行为改变型发布（新门槛默认 0），但要在发版说明里写清楚「规则2/3 现在也能配高位厚度闸门，默认不拦，要去配置页填」
