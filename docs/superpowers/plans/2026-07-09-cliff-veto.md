# 悬崖否决(奖励区间下方空档 → 该侧不挂)实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** gap_single 在归级选档前加一道否决:奖励区间下沿往下 `cliff_probe_cents`¢ 内无买档(悬崖)则该 token 侧不挂;配置默认 2(启用)。

**Architecture:** `engine/laddering.py` 的 `explain_gap_single_order` 加 `cliff_probe_cents` 参数与空档检查,并沿 `plan_gap_single_order`/`compute_market_single_orders`/`preview_gap_single_market` 透传;`manager.place_orders` 两处调用与 ladder 预览路由从模板读值传入;`TEMPLATE_DEFAULTS` 加 `cliff_probe_cents:2`,配置页加输入框。

**Tech Stack:** Python 3、pytest、Flask、原生 JS。

## Global Constraints

- **纯函数签名默认 `cliff_probe_cents=0`**(追加在各签名末尾):`0` = 不做悬崖检查,逐字节等价现状;保护大量用**位置参数**调 `explain/plan/compute/preview` 的现有单测。
- **配置默认 `TEMPLATE_DEFAULTS["cliff_probe_cents"]=2`**:产品默认启用(N=2¢)。这是**有意的行为改变**。
- 判定:`in_range` 非空后,若 `[reward_range_min − N·0.01, reward_range_min)` 内(用原始 `bids`)无买档 → `skip`。带下界含、上界不含。
- 范围:每 token 侧独立(gap_single 本就 per-token)。
- 不动 rule1/2/3 归级、系数选档、rule1 高位和门槛、`in_range` 断层计算。
- `manager`/route 读值统一 `float(tmpl.get("cliff_probe_cents", 2))`。
- config.html 含中文,由**主 agent** 直接改;校验无 BOM、中文正确。
- `pytest` 全绿是每个任务的闸。

---

### Task 1: laddering 悬崖检查 + 渲染 + 透传(纯函数)

**Files:**
- Modify: `engine/laddering.py`(`explain_gap_single_order` 29-140、`plan_gap_single_order` 143-171、`compute_market_single_orders` 244-256/269-281、`preview_gap_single_market` 334-371、`gap_single_price_basis` 195-241)
- Test: `tests/test_gap_single.py`

**Interfaces:**
- Produces(全部在原签名末尾追加 `cliff_probe_cents=0`):
  - `explain_gap_single_order(..., rule3_min_coeff, cliff_probe_cents=0)` — 决策 dict 加 `"cliff"` 键;悬崖时 `action="skip"`、`cliff=True`、`skip_reason` 含「悬崖」、提前返回(`rule=None`、`levels=[]`)
  - `plan_gap_single_order(..., rule3_min_coeff, cliff_probe_cents=0)` / `compute_market_single_orders(..., rule3_min_coeff, cliff_probe_cents=0)` / `preview_gap_single_market(..., rule3_min_coeff, cliff_probe_cents=0)` — 透传
  - `gap_single_price_basis` 对 `d["cliff"]` 输出悬崖专用串(不落入「区间内无可评估买档」误报)

- [ ] **Step 1: 写失败测试**

`tests/test_gap_single.py`:先给 `_plan` 助手加 `cliff=0` 形参(把 `x3=0,` 行后加 `cliff=0,`,并在 `plan_gap_single_order(...)` 调用的 `x3,` 后加 `cliff_probe_cents=cliff,`)。然后在文件末尾追加:

```python
from engine.laddering import (
    explain_gap_single_order,
    compute_market_single_orders,
    preview_gap_single_market,
    gap_single_price_basis,
)


def test_cliff_below_zone_skips():
    # in_range 有 0.18/0.17(≥rmin 0.10);[0.08,0.10) 内无档(下一档直接 0.02)→ 悬崖
    out = _plan([_b(0.18, 50), _b(0.17, 40), _b(0.02, 9999)], cliff=2)
    assert out is None


def test_cliff_support_within_band_places():
    # 同上但 0.09 落在 [0.08,0.10) 内 → 有支撑 → 照常挂 0.18
    out = _plan([_b(0.18, 50), _b(0.17, 40), _b(0.09, 30), _b(0.02, 9999)], cliff=2)
    assert out == (0.18, 20)


def test_cliff_disabled_places_despite_void():
    # 下方真空但 cliff=0 → 照常挂(零回归)
    out = _plan([_b(0.18, 50), _b(0.17, 40), _b(0.02, 9999)], cliff=0)
    assert out == (0.18, 20)


def test_explain_cliff_sets_flag_and_reason():
    d = explain_gap_single_order(
        [_b(0.18, 50), _b(0.17, 40), _b(0.02, 9999)],
        0.10, 0.31, 20, AV, 10, 5, 20, 0, 0, 0, cliff_probe_cents=2,
    )
    assert d["action"] == "skip"
    assert d["cliff"] is True
    assert "悬崖" in d["skip_reason"]
    assert d["rule"] is None


def test_price_basis_renders_cliff_not_empty_book():
    d = explain_gap_single_order(
        [_b(0.18, 50), _b(0.17, 40), _b(0.02, 9999)],
        0.10, 0.31, 20, AV, 10, 5, 20, 0, 0, 0, cliff_probe_cents=2,
    )
    pb = gap_single_price_basis(d, 0.10, 0.31)
    assert "悬崖" in pb
    assert "无可评估买档" not in pb  # 不能误报成空区间


def test_compute_threads_cliff_probe():
    side = {"bids": [_b(0.18, 50), _b(0.17, 40), _b(0.02, 9999)],
            "reward_range_min": 0.10, "reward_range_max": 0.31, "min_size": 20}
    out = compute_market_single_orders(
        side, None, 1000.0, 500, AV, 10, 5, 20, 0, 0, 0, cliff_probe_cents=2,
    )
    assert out["a"] == []


def test_preview_threads_cliff_probe():
    side = {"outcome": "Yes", "token_id": "t", "min_size": 20,
            "reward_range_min": 0.10, "reward_range_max": 0.31,
            "best_bid": 0.18, "best_ask": 0.19, "spread_cents": 1,
            "bids": [_b(0.18, 50), _b(0.17, 40), _b(0.02, 9999)]}
    out = preview_gap_single_market(
        side, None, AV, 10, 5, 20, 0, 0, 0, cliff_probe_cents=2,
    )
    assert out["a"]["action"] == "skip"
    assert out["a"]["cliff"] is True
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `pytest tests/test_gap_single.py -k "cliff or price_basis_renders" -v`
Expected: FAIL —— `plan_gap_single_order() got an unexpected keyword argument 'cliff_probe_cents'` / `KeyError: 'cliff'` 等。

- [ ] **Step 3: `explain_gap_single_order` 加参数 + cliff 键 + 检查**

`engine/laddering.py`:

签名(当前 29-41)末尾 `rule3_min_coeff,` 后加一行 `cliff_probe_cents=0,`。

init dict(当前 60-73)`"skip_reason": None,` 后加一行:
```python
        "cliff": False,
```

在 `if not in_range:` 跳过块(当前 86-88)之后、`for lv in in_range:`(当前 89)之前插入:
```python
    if cliff_probe_cents and cliff_probe_cents > 0:
        lo = reward_range_min - cliff_probe_cents * 0.01
        if not any(lo <= float(b["price"]) < reward_range_min for b in bids):
            d["cliff"] = True
            d["skip_reason"] = (
                f"奖励区间下方 {cliff_probe_cents:g}¢ 内无买档支撑(悬崖)→ 不挂"
            )
            return d
```

- [ ] **Step 4: `gap_single_price_basis` 加悬崖分支**

`engine/laddering.py` `gap_single_price_basis`,在 `if d.get("action") != "place" or d.get("chosen_index") is None:`(当前 203)之后、`levels = d.get("levels") or []`(当前 205)之前插入:
```python
        if d.get("cliff"):
            return f"{d.get('skip_reason', '')};{src}"
```

- [ ] **Step 5: 透传 plan / compute / preview**

`plan_gap_single_order`(当前 143-171):签名末尾加 `cliff_probe_cents=0,`;`explain_gap_single_order(...)` 调用(158-170)末尾 `rule3_min_coeff,` 后加 `cliff_probe_cents,`。

`compute_market_single_orders`(当前 244-256):签名末尾加 `cliff_probe_cents=0,`;`plan_gap_single_order(...)` 调用(269-281)末尾 `rule3_min_coeff,` 后加 `cliff_probe_cents,`。

`preview_gap_single_market`(当前 334-344):签名末尾加 `cliff_probe_cents=0,`;`explain_gap_single_order(...)` 调用(359-371)末尾 `rule3_min_coeff,` 后加 `cliff_probe_cents,`。

- [ ] **Step 6: 跑测试,确认通过**

Run: `pytest tests/test_gap_single.py -v`
Expected: PASS(新用例 + 全部现有 gap_single 用例——位置参数调用因默认 0 不受影响)。

- [ ] **Step 7: 提交**

```bash
git add engine/laddering.py tests/test_gap_single.py
git commit -m "feat(laddering): gap_single 悬崖否决(区间下方空档→该侧不挂),纯函数+透传"
```

---

### Task 2: 配置默认 + manager/route 透传 + 集成/契约

**Files:**
- Modify: `config.py`(`TEMPLATE_DEFAULTS` 60-83)、`engine/manager.py`(读值 ~161、compute 调用 ~295、explain 调用 ~310)、`web/routes.py`(preview 调用 ~1077)
- Test: `tests/test_place_orders.py`(`_make_worker` 17-28)、`tests/test_database.py`

**Interfaces:**
- Consumes: `compute_market_single_orders`/`explain_gap_single_order`/`preview_gap_single_market` 的 `cliff_probe_cents=`(Task 1)。
- Produces: `TEMPLATE_DEFAULTS["cliff_probe_cents"]==2`;place_orders 与预览路由按模板值启用悬崖否决。

- [ ] **Step 1: 写失败测试**

`tests/test_place_orders.py`:先给 `_make_worker` 的共享 `tmpl`(当前 17-28)加一行 `"cliff_probe_cents": 0,`(保现有集成测试悬崖关闭)。然后末尾追加:

```python
def test_cliff_below_zone_skips_side():
    worker, api, db = _make_worker(template={"cliff_probe_cents": 2})
    # 买一0.28/买二0.27(区间内),下方直接砸到0.05(区间下沿2¢内无支撑)→ 悬崖 → 不挂
    api.get_orderbook.return_value = _ob(
        [(0.28, 50), (0.27, 40), (0.05, 9999)], [(0.31, 1000)]
    )
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=20)])
    assert api.place_limit_buy.call_count == 0
```

`tests/test_database.py`:在现有 TEMPLATE_DEFAULTS 断言处(如 `test_template_defaults_*`)加一行:
```python
    assert TEMPLATE_DEFAULTS["cliff_probe_cents"] == 2
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `pytest tests/test_place_orders.py::test_cliff_below_zone_skips_side tests/test_database.py -k "cliff or template_default" -v`
Expected: FAIL —— 集成用例仍挂单(manager 未透传 cliff);`KeyError: 'cliff_probe_cents'`(TEMPLATE_DEFAULTS 未加)。

- [ ] **Step 3: config.py 加默认**

`config.py` `TEMPLATE_DEFAULTS`,在 `"rule3_min_coeff": 0,`(当前 66)之后加:
```python
    # 悬崖否决:奖励区间下沿往下这么多美分内无买档 → 该侧不挂。0=关闭。
    "cliff_probe_cents": 2,
```

- [ ] **Step 4: manager 读值 + 两处透传**

`engine/manager.py`:在读 `rule3_min_coeff = float(tmpl.get("rule3_min_coeff", 0))`(当前 161)之后加:
```python
        cliff_probe_cents = float(tmpl.get("cliff_probe_cents", 2))
```

`compute_market_single_orders(...)` 调用(当前 295-307)末尾 `rule3_min_coeff,` 后加 `cliff_probe_cents,`。
`explain_gap_single_order(...)` 调用(当前 310-322)末尾 `rule3_min_coeff,` 后加 `cliff_probe_cents,`。

- [ ] **Step 5: 预览路由透传**

`web/routes.py` 的 `preview_gap_single_market(...)` 调用(当前 1077-1087),末尾 `float(tmpl.get("rule3_min_coeff", 0)),` 后加:
```python
        float(tmpl.get("cliff_probe_cents", 2)),
```

- [ ] **Step 6: 跑测试,确认通过**

Run: `pytest tests/test_place_orders.py tests/test_database.py -v`
Expected: PASS(新集成/契约用例绿;现有集成用例因 `_make_worker` 加了 `cliff_probe_cents:0` 仍绿)。

- [ ] **Step 7: 全量回归**

Run: `pytest -q`
Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add config.py engine/manager.py web/routes.py tests/test_place_orders.py tests/test_database.py
git commit -m "feat(strategy): cliff_probe_cents 默认2 + manager/预览透传,悬崖否决启用"
```

---

### Task 3: 配置页加「悬崖探测」输入框(主 agent 直接改)

**Files:**
- Modify: `web/templates/config.html`(「挂单参数」区,`rule3_min_coeff` 输入行 ~110 之后)

**Interfaces:**
- Consumes: `TEMPLATE_DEFAULTS["cliff_probe_cents"]`(Task 2);策略表单 submit 收所有 `input[type=number][name]`,自动带上。

> ⚠️ 含中文,主 agent 用 Edit 直接改;写后 `grep` 校验中文 + `head -c 3 | xxd` 无 BOM。前端无 JS 测试,靠人工/`verify`。

- [ ] **Step 1: 加输入框**

`web/templates/config.html`,在 `rule3_min_coeff` 那个 `form-group`(当前 ~110)之后加:
```html
                <div class="form-group"><label>悬崖探测 (美分，区间下沿往下此距离内无买档则该侧不挂；0=关)</label><input type="number" name="cliff_probe_cents" step="0.1"></div>
```

- [ ] **Step 2: 校验无 BOM + 中文**

Run:
```bash
head -c 3 web/templates/config.html | xxd            # 期望非 ef bb bf
grep -n "悬崖探测\|cliff_probe_cents" web/templates/config.html
```
Expected: 无 BOM;命中新 label(中文正确)与 input。

- [ ] **Step 3: 全量回归**

Run: `pytest -q`
Expected: PASS(纯模板改动)。

- [ ] **Step 4: 提交**

```bash
git add web/templates/config.html
git commit -m "feat(config): 配置页加悬崖探测(cliff_probe_cents)输入框"
```

---

## 收尾(全部任务后)

- [ ] `pytest` 全绿。
- [ ] 人工验收:配置页可编辑「悬崖探测」;设 2 后,对奖励区间正下方空档的市场该侧不挂,历史/预演「原因」显示悬崖、「价格依据」不误报空区间。
- [ ] 合并到 `main`(`superpowers:finishing-a-development-branch`)。此为策略行为改变,按 `docs/版本号规范.md` 属**主版本号**;发版待用户确认。

## Self-Review 记录

- **Spec 覆盖:** 判定口径→Task1 Step3;渲染(cliff 键+price_basis)→Task1 Step3/4;透传 plan/compute/preview→Task1 Step5;config 默认2→Task2 Step3;manager 两处+route→Task2 Step4/5;fixture 一行→Task2 Step1;契约→Task2 Step1;config.html→Task3;测试(悬崖跳/有支撑/N=0/explain/price_basis/透传/集成/契约)分布 Task1/2。全覆盖。
- **占位符:** 无 TBD/TODO;每步实代码或实命令。
- **类型一致:** 各函数末位 `cliff_probe_cents=0` 一致;manager/route 读 `float(tmpl.get("cliff_probe_cents", 2))` 一致;决策 dict `"cliff"` 键在 explain 产出、price_basis/preview 消费一致;`_plan(cliff=0)`/`_make_worker` fixture 与被测签名对齐。
