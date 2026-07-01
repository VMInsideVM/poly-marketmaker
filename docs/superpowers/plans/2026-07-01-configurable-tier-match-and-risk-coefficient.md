# 可配置区间变量 + 风险系数 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给多档做市加两个通用旗——奖励最低份额范围筛选、每档区间匹配变量可选(累计厚度/风险系数)+可编辑金额数值表——让讨论中的策略靠配置复现,代码零写死。

**Architecture:** 复用现有 `tier_rules` 每档区间→动作结构;把匹配变量从写死的「累计厚度」抽象成可选,新增纯函数 `amount_value()` 查表算风险系数;`config.py` 加四个默认键(`template_settings` 无需迁移);`scanner.py`/`laddering.py`/调用点(`manager.py`、`routes.py`)与配置页透传。

**Tech Stack:** Python 3.12、pytest、Flask、原生 JS(config.html)。

## Global Constraints

- 默认 `tier_match_var="cumulative_thickness"` → 现有多档行为**完全不变**(所有现存 `test_laddering*.py` 必须保持绿)。
- 价格单位:引擎内部用小数价(0.20),配置页展示用美分(20),前端转换。
- `amount_value(price, table)`:返回第一个 `price <= upper` 的 `value`;价超最大 `upper` → `None`;`None`/`<=0` → 该档不挂。
- 金额数值仅在 `tier_match_var="risk_coefficient"` 时生效。
- UI 字符串一律简体中文。含中文的 `config.html` 由主 agent 直接 Write,写后 `node --check` + 查 BOM/中文别字。
- 每个 pure-function 任务用 TDD:先写失败测试→跑红→最小实现→跑绿→提交。
- 不改 `per_share_reward_thresholds`、`reward_bracket`、离场逻辑。

---

## File Structure

- `config.py` — `TEMPLATE_DEFAULTS` 加 4 键(Task 1)。
- `engine/laddering.py` — 新增 `amount_value()`;`build_ladder`/`resolve_tier_share`/`compute_market_ladders`/`preview_market_ladders`/`_verbose_levels` 加匹配变量抽象(Task 2-4)。
- `engine/scanner.py` — `filter_for_template` 的 `rewards_min_size` 范围筛选(Task 5)。
- `engine/manager.py` + `web/routes.py` — 调用点透传新旗(Task 6)。
- `web/templates/config.html` — 区间变量下拉 + 金额表编辑器 + 份额范围字段(Task 7)。
- 测试:`tests/test_laddering.py`、`tests/test_laddering_preview.py`、`tests/test_scanner.py`、`tests/test_database.py`。

---

### Task 1: 配置新增四个模板字段

**Files:**
- Modify: `config.py:16-43`(`TEMPLATE_DEFAULTS`)
- Test: `tests/test_database.py`(尾部追加)

**Interfaces:**
- Produces: `TEMPLATE_DEFAULTS["rewards_min_size_min"]=1`、`["rewards_min_size_max"]=250`、`["tier_match_var"]="cumulative_thickness"`、`["amount_value_table"]=[{"upper":0.20,"value":1},{"upper":0.25,"value":1.5},{"upper":0.30,"value":2}]`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_database.py`:

```python
def test_template_defaults_has_tier_match_and_size_range():
    from config import TEMPLATE_DEFAULTS

    assert TEMPLATE_DEFAULTS["rewards_min_size_min"] == 1
    assert TEMPLATE_DEFAULTS["rewards_min_size_max"] == 250
    assert TEMPLATE_DEFAULTS["tier_match_var"] == "cumulative_thickness"
    table = TEMPLATE_DEFAULTS["amount_value_table"]
    assert table == [
        {"upper": 0.20, "value": 1},
        {"upper": 0.25, "value": 1.5},
        {"upper": 0.30, "value": 2},
    ]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_database.py::test_template_defaults_has_tier_match_and_size_range -v`
Expected: FAIL(KeyError)

- [ ] **Step 3: 实现**

在 `config.py` `TEMPLATE_DEFAULTS` 里(`per_share_reward_thresholds` 之后、闭合 `}` 之前)加:

```python
    # 奖励最低份额范围筛选(向上取档硬顶 250)
    "rewards_min_size_min": 1,
    "rewards_min_size_max": 250,
    # 每档区间匹配变量 + 金额数值表(风险系数模式用)
    "tier_match_var": "cumulative_thickness",  # "cumulative_thickness" | "risk_coefficient"
    "amount_value_table": [
        {"upper": 0.20, "value": 1},
        {"upper": 0.25, "value": 1.5},
        {"upper": 0.30, "value": 2},
    ],
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_database.py -q`
Expected: PASS(全绿)

- [ ] **Step 5: 提交**

```bash
git add config.py tests/test_database.py
git commit -m "feat(config): 加 rewards_min_size 范围 + tier_match_var + amount_value_table 模板字段"
```

---

### Task 2: 纯函数 `amount_value(price, table)`

**Files:**
- Modify: `engine/laddering.py`(文件顶部、`build_ladder` 之前加函数)
- Test: `tests/test_laddering.py`(尾部追加)

**Interfaces:**
- Produces: `amount_value(price: float, table: list[dict]) -> float | None`。table 为 `[{"upper","value"}]`;返回第一个 `price <= upper` 的 `value`(按 upper 升序);价超最大 upper 或表空 → `None`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_laddering.py`:

```python
from engine.laddering import amount_value

_AVT = [{"upper": 0.20, "value": 1}, {"upper": 0.25, "value": 1.5}, {"upper": 0.30, "value": 2}]


def test_amount_value_buckets():
    assert amount_value(0.15, _AVT) == 1
    assert amount_value(0.20, _AVT) == 1      # 端点归下档
    assert amount_value(0.23, _AVT) == 1.5
    assert amount_value(0.30, _AVT) == 2


def test_amount_value_out_of_range_is_none():
    assert amount_value(0.31, _AVT) is None   # 价超最大 upper
    assert amount_value(0.15, []) is None      # 空表
    assert amount_value(0.15, None) is None


def test_amount_value_unsorted_table_ok():
    unsorted = [{"upper": 0.30, "value": 2}, {"upper": 0.20, "value": 1}]
    assert amount_value(0.15, unsorted) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_laddering.py -k amount_value -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现**

在 `engine/laddering.py` 的 `build_ladder` 定义之前加:

```python
def amount_value(price, table):
    """金额数值查表:返回第一个 price <= upper 的 value(按 upper 升序);价超最大 upper -> None。

    table: [{"upper": float, "value": float}](顺序无所谓,内部按 upper 升序取)。
    低端下界由价格区间旗 min_price_cents 兜。表空/无匹配 -> None(该档不挂)。
    """
    if not table:
        return None
    rows = []
    for r in table:
        try:
            rows.append((float(r["upper"]), float(r["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x[0])
    for upper, value in rows:
        if price <= upper:
            return value
    return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_laddering.py -k amount_value -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add engine/laddering.py tests/test_laddering.py
git commit -m "feat(laddering): 加纯函数 amount_value 金额数值查表"
```

---

### Task 3: `build_ladder` 匹配变量抽象(累计厚度 / 风险系数)

**Files:**
- Modify: `engine/laddering.py:8-35`(`build_ladder`)
- Test: `tests/test_laddering.py`(尾部追加)

**Interfaces:**
- Consumes: `amount_value`(Task 2)。
- Produces: `build_ladder(bids, reward_range_min, reward_range_max, min_size, tiers_k, tier_match_var="cumulative_thickness", amount_value_table=None)` → `[{"price","match_value","cumulative_thickness"}]`。累计厚度模式:`match_value=累计厚度`、合格档=在区间且 thickness≥1(不变)。风险系数模式:`match_value=thickness/amount_value(price)`、合格档=在区间且 `amount_value(price)` 有值(>0)。

> **兼容说明**:保留 `cumulative_thickness` 字段(现有测试与预演在用),**新增** `match_value`;不改字段名。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_laddering.py`:

```python
def test_build_ladder_default_still_cumulative():
    # 累计厚度模式(默认):行为与旧版一致,match_value == cumulative_thickness
    bids = [_b(0.35, 300), _b(0.30, 150), _b(0.25, 50), _b(0.22, 200)]
    ladder = build_ladder(bids, 0.20, 0.40, 100, 6)
    assert [r["price"] for r in ladder] == [0.35, 0.30, 0.22]
    for r in ladder:
        assert r["match_value"] == r["cumulative_thickness"]


def test_build_ladder_risk_coefficient_mode():
    # 风险系数 = 本档厚度 / 金额数值(价)。min_size=100。
    # 0.15: thickness 200/100=2, av=1 -> rc=2 ; 0.23: 300/100=3, av=1.5 -> rc=2
    bids = [_b(0.15, 200), _b(0.23, 300)]
    ladder = build_ladder(
        bids, 0.0, 1.0, 100, 6,
        tier_match_var="risk_coefficient", amount_value_table=_AVT,
    )
    by_price = {r["price"]: r["match_value"] for r in ladder}
    assert by_price[0.15] == 2.0
    assert by_price[0.23] == 2.0


def test_build_ladder_risk_mode_skips_price_over_table():
    # 0.40 超金额表(最大 upper 0.30)-> 不挂;0.15 在表内 -> 入档
    bids = [_b(0.40, 500), _b(0.15, 200)]
    ladder = build_ladder(
        bids, 0.0, 1.0, 100, 6,
        tier_match_var="risk_coefficient", amount_value_table=_AVT,
    )
    assert [r["price"] for r in ladder] == [0.15]


def test_build_ladder_risk_mode_ignores_thickness_gate():
    # 薄档 thickness=0.5<1,累计厚度模式会跳过;风险系数模式仍入档(交给 tier_rules 兜)
    bids = [_b(0.15, 50)]
    ladder = build_ladder(
        bids, 0.0, 1.0, 100, 6,
        tier_match_var="risk_coefficient", amount_value_table=_AVT,
    )
    assert [r["price"] for r in ladder] == [0.15]
    assert ladder[0]["match_value"] == 0.5  # 0.5/1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_laddering.py -k build_ladder -v`
Expected: FAIL(新测试 TypeError/KeyError)

- [ ] **Step 3: 实现**

把 `engine/laddering.py:8-35` 的 `build_ladder` 整体替换为:

```python
def build_ladder(
    bids,
    reward_range_min,
    reward_range_max,
    min_size,
    tiers_k,
    tier_match_var="cumulative_thickness",
    amount_value_table=None,
):
    """构建单边档价梯。

    tier_match_var:
      - "cumulative_thickness"(默认):match_value=累计厚度;合格档=区间内且本档厚度>=1。
      - "risk_coefficient":match_value=本档厚度/金额数值(价);合格档=区间内且金额数值有值(>0)。
        价超金额表 -> 该档不挂;厚度门槛交由 tier_rules 区间表兜。
    返回 [{"price","match_value","cumulative_thickness"}],档1=最高合格价位。
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
        if not (reward_range_min <= price <= reward_range_max):
            continue
        if tier_match_var == "risk_coefficient":
            av = amount_value(price, amount_value_table)
            if not av or av <= 0:
                continue
            match_value = thickness / av
        else:
            if thickness < 1:
                continue
            match_value = running_ct
        tiers.append(
            {
                "price": price,
                "match_value": match_value,
                "cumulative_thickness": running_ct,
            }
        )
        if len(tiers) >= tiers_k:
            break
    return tiers
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_laddering.py -q`
Expected: PASS(含旧的 cumulative_thickness 测试保持绿)

- [ ] **Step 5: 提交**

```bash
git add engine/laddering.py tests/test_laddering.py
git commit -m "feat(laddering): build_ladder 支持风险系数匹配变量(默认仍累计厚度)"
```

---

### Task 4: `resolve_tier_share` 泛化 + 市场级/预演透传

**Files:**
- Modify: `engine/laddering.py`(`resolve_tier_share` 首参名、`compute_market_ladders`、`preview_market_ladders`、`_verbose_levels`)
- Test: `tests/test_laddering.py`、`tests/test_laddering_preview.py`

**Interfaces:**
- Consumes: `build_ladder`(Task 3)。
- Produces:
  - `resolve_tier_share(match_value, tier_rule, price, min_size, remaining_budget_usd)`(首参更名,行为不变)。
  - `compute_market_ladders(side_a, side_b, tier_rules, market_budget_usd, max_exposure_shares, tier_match_var="cumulative_thickness", amount_value_table=None)`。
  - `preview_market_ladders(side_a, side_b, tier_rules, budget_usd, max_shares, tier_match_var="cumulative_thickness", amount_value_table=None)`;预演每档 level 增 `match_value` 字段,风险系数模式 `skip_reason` 增「价超金额表」。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_laddering.py`:

```python
from engine.laddering import compute_market_ladders


def test_compute_ladders_risk_mode_places_by_risk_table():
    # tier_rule:风险系数>1.5 -> min_size,否则不挂。两档 rc 都=2 -> 都挂 min_size=100。
    rule = [{"upper": 1.5, "action": {"type": "min_size"}}, {"upper": None, "action": {"type": "skip"}}]
    side = {
        "bids": [{"price": "0.15", "size": "200"}, {"price": "0.23", "size": "300"}],
        "reward_range_min": 0.0, "reward_range_max": 1.0, "min_size": 100,
    }
    out = compute_market_ladders(
        side, None, [rule, rule], 100000.0, 100000,
        tier_match_var="risk_coefficient", amount_value_table=_AVT,
    )
    assert out["a"] == [(0.15, 100), (0.23, 100)]


def test_compute_ladders_risk_mode_skips_low_risk():
    # 薄档 rc=0.5 < 1.5 -> 命中「其余→skip」-> 不挂
    rule = [{"upper": 1.5, "action": {"type": "min_size"}}, {"upper": None, "action": {"type": "skip"}}]
    side = {
        "bids": [{"price": "0.15", "size": "50"}],
        "reward_range_min": 0.0, "reward_range_max": 1.0, "min_size": 100,
    }
    out = compute_market_ladders(
        side, None, [rule, rule], 100000.0, 100000,
        tier_match_var="risk_coefficient", amount_value_table=_AVT,
    )
    assert out["a"] == []
```

追加到 `tests/test_laddering_preview.py`(顶部已 import `preview_market_ladders`):

```python
_AVT = [{"upper": 0.20, "value": 1}, {"upper": 0.25, "value": 1.5}, {"upper": 0.30, "value": 2}]


def test_preview_risk_mode_marks_price_over_table():
    side = {
        "outcome": "YES", "token_id": "t", "min_size": 100,
        "best_bid": 0.4, "best_ask": 0.45, "spread_cents": 5,
        "reward_range_min": 0.0, "reward_range_max": 1.0,
        "bids": [{"price": "0.40", "size": "500"}, {"price": "0.15", "size": "200"}],
    }
    rule = [{"upper": None, "action": {"type": "min_size"}}]
    out = preview_market_ladders(
        side, None, [rule, rule], 100000.0, 100000,
        tier_match_var="risk_coefficient", amount_value_table=_AVT,
    )
    levels = {L["price"]: L for L in out["a"]["levels"]}
    assert levels[0.40]["skip_reason"] == "价超金额表"
    assert levels[0.15]["match_value"] == 2.0  # 200/100 / 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_laddering.py tests/test_laddering_preview.py -k "risk" -v`
Expected: FAIL(TypeError:未知关键字参数)

- [ ] **Step 3: 实现**

3a. `resolve_tier_share` 首参更名(`engine/laddering.py:52-69`),把 `cumulative_thickness` 改为 `match_value`,函数体内 `_interval_action(tier_rule, cumulative_thickness)` 改成 `_interval_action(tier_rule, match_value)`。其余不变。

3b. `compute_market_ladders`(`engine/laddering.py:72-123`)签名加两参并透传:

```python
def compute_market_ladders(
    side_a,
    side_b,
    tier_rules,
    market_budget_usd,
    max_exposure_shares,
    tier_match_var="cumulative_thickness",
    amount_value_table=None,
):
```

把内部 `build_ladder(...)` 调用改为传 `tier_match_var, amount_value_table`(接在 `tiers_k` 之后);把 `ct = rung["cumulative_thickness"]` 改为 `mv = rung["match_value"]`,`resolve_tier_share(ct, ...)` 改为 `resolve_tier_share(mv, ...)`。

3c. `_verbose_levels`(`engine/laddering.py:174-206`)整体替换:

```python
def _verbose_levels(
    side, tiers_k, tier_match_var="cumulative_thickness", amount_value_table=None
):
    """每个 bid 价位 -> 标注 dict;合格档按序给 tier_index(<tiers_k)。"""
    min_size = side["min_size"]
    rmin, rmax = side["reward_range_min"], side["reward_range_max"]
    levels, running, tier_no = [], 0.0, 0
    for lvl in side["bids"]:
        price, size = float(lvl["price"]), float(lvl["size"])
        thickness = size / min_size if min_size > 0 else 0.0
        running += thickness
        in_range = rmin <= price <= rmax
        if tier_match_var == "risk_coefficient":
            av = amount_value(price, amount_value_table)
            qualifies = in_range and bool(av) and av > 0
            match_value = (thickness / av) if (av and av > 0) else running
            oor_reason = "超出奖励范围" if not in_range else "价超金额表"
        else:
            qualifies = in_range and thickness >= 1
            match_value = running
            oor_reason = "超出奖励范围" if not in_range else "厚度<1"
        tier_index, skip_reason = None, None
        if not qualifies:
            skip_reason = oor_reason
        elif tier_no < tiers_k:
            tier_index, tier_no = tier_no, tier_no + 1
        else:
            skip_reason = "超过最大档数"
        levels.append(
            {
                "price": price,
                "size": size,
                "thickness": thickness,
                "cumulative_thickness": running,
                "match_value": match_value,
                "in_range": in_range,
                "qualifies": qualifies,
                "tier_index": tier_index,
                "shares": 0,
                "amount": 0.0,
                "skip_reason": skip_reason,
            }
        )
    return levels
```

3d. `preview_market_ladders`(`engine/laddering.py:209-281`)签名加两参:

```python
def preview_market_ladders(
    side_a,
    side_b,
    tier_rules,
    budget_usd,
    max_shares,
    tier_match_var="cumulative_thickness",
    amount_value_table=None,
):
```

把内部 `lv[key] = _verbose_levels(side, tiers_k)` 改为 `_verbose_levels(side, tiers_k, tier_match_var, amount_value_table)`;把 `price, ct = L["price"], L["cumulative_thickness"]` 改为 `price, mv = L["price"], L["match_value"]`,`resolve_tier_share(ct, ...)` 改为 `resolve_tier_share(mv, ...)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_laddering.py tests/test_laddering_preview.py -q`
Expected: PASS(旧测试全绿 + 新风险系数测试绿)

- [ ] **Step 5: 提交**

```bash
git add engine/laddering.py tests/test_laddering.py tests/test_laddering_preview.py
git commit -m "feat(laddering): 市场级/预演透传匹配变量,resolve_tier_share 首参泛化为 match_value"
```

---

### Task 5: scanner 奖励最低份额范围筛选

**Files:**
- Modify: `engine/scanner.py:210-213`(`filter_for_template` 内)
- Test: `tests/test_scanner.py`(尾部追加)

**Interfaces:**
- Consumes: `template["rewards_min_size_min"]`、`template["rewards_min_size_max"]`(Task 1)。
- Produces: 只放行 `max(1,min) <= rewards_min_size <= min(250,max)` 的市场。

- [ ] **Step 1: 写失败测试**

先看 `tests/test_scanner.py` 现有 `filter_for_template` 的构造/夹具(市场 dict 形状、db stub),仿照追加。核心断言:

```python
def test_filter_rewards_min_size_exact_20(scanner_with_pool):
    # 详见文件内现有夹具:构造两个市场 rewards_min_size=20 与 =50,模板 min=max=20
    scanner, pool = scanner_with_pool(sizes=[20, 50])
    tmpl = _template(rewards_min_size_min=20, rewards_min_size_max=20)
    out = scanner.filter_for_template(pool, tmpl, "0xW")
    assert {m["rewards_min_size"] for m in out} == {20}


def test_filter_rewards_min_size_default_range_passes_all(scanner_with_pool):
    scanner, pool = scanner_with_pool(sizes=[20, 50])
    tmpl = _template()  # 默认 1/250
    out = scanner.filter_for_template(pool, tmpl, "0xW")
    assert {m["rewards_min_size"] for m in out} == {20, 50}
```

> 执行时:先读 `tests/test_scanner.py` 顶部的夹具/构造工具,用它现有的市场构造方式(而非上面伪造的 `scanner_with_pool`/`_template`);若无现成夹具,仿照文件内已有的 `filter_for_template` 测试写最小构造(带 `rewards_config`/`market_reward`/`end_date`/`rewards_min_size`/`_orderbooks` 的市场 dict + `min_reward_usd` 等低门槛模板,使目标市场只在 size 上区分)。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scanner.py -k rewards_min_size -v`
Expected: FAIL(默认 1/250 已放行,但 exact=20 用例失败——现状不看模板范围)

- [ ] **Step 3: 实现**

把 `engine/scanner.py:210-213`:

```python
            min_size = int(market.get("rewards_min_size", 0) or 0)
            # v4 §3:最低份额 0 < ≤ 250;超档无取档 -> 不做该市场
            if not (0 < min_size <= 250):
                continue
```

改为:

```python
            min_size = int(market.get("rewards_min_size", 0) or 0)
            # 最低份额范围筛选(可配);硬顶 250(超档无取档)。默认 1/250 = 放行全部合法档。
            size_lo = max(1, int(template.get("rewards_min_size_min", 1) or 1))
            size_hi = min(250, int(template.get("rewards_min_size_max", 250) or 250))
            if not (size_lo <= min_size <= size_hi):
                continue
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_scanner.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add engine/scanner.py tests/test_scanner.py
git commit -m "feat(scanner): 奖励最低份额范围筛选(rewards_min_size_min/max)"
```

---

### Task 6: 调用点透传新旗(manager + routes)

**Files:**
- Modify: `engine/manager.py:148-149` 附近(读旗)+ `:290-292`(调用)
- Modify: `web/routes.py:974-976` 附近(读旗)+ `:1021`(调用)

**Interfaces:**
- Consumes: `compute_market_ladders` / `preview_market_ladders` 的新签名(Task 4)、模板新键(Task 1)。

- [ ] **Step 1: manager 读旗 + 透传**

在 `engine/manager.py` `place_orders`/`_place_round` 里已有 `tmpl = self.db.get_template_for(...)` 与 `tier_rules = tmpl.get("tier_rules") or []`(约 148-149)之后加:

```python
            tier_match_var = tmpl.get("tier_match_var", "cumulative_thickness")
            amount_value_table = tmpl.get("amount_value_table") or None
```

把 `:290-292` 的调用改为:

```python
                ladders = compute_market_ladders(
                    ca, cb, tier_rules, budget, shares_budget,
                    tier_match_var, amount_value_table,
                )
```

- [ ] **Step 2: routes 读旗 + 透传**

在 `web/routes.py` `api_market_ladder` 里 `tier_rules = tmpl.get("tier_rules") or []`(约 974)之后加:

```python
    tier_match_var = tmpl.get("tier_match_var", "cumulative_thickness")
    amount_value_table = tmpl.get("amount_value_table") or None
```

把 `:1021` 的调用改为:

```python
    preview = preview_market_ladders(
        a, b, tier_rules, budget, shares_budget, tier_match_var, amount_value_table
    )
```

- [ ] **Step 3: 全量回归**

Run: `python -m pytest -q`
Expected: PASS(全绿;默认累计厚度模式,行为不变)

- [ ] **Step 4: 静态自检调用点**

Run: `python -c "import ast,sys; ast.parse(open('engine/manager.py',encoding='utf-8').read()); ast.parse(open('web/routes.py',encoding='utf-8').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 5: 提交**

```bash
git add engine/manager.py web/routes.py
git commit -m "feat: 下单/预演调用点透传 tier_match_var + amount_value_table"
```

---

### Task 7: 配置页 UI(区间变量下拉 + 金额表编辑器 + 份额范围)

**Files:**
- Modify: `web/templates/config.html`(策略表单 HTML + `loadStrategy` 回填 + submit 收集)

> **由主 agent 直接 Write/Edit**(含中文,避免 subagent 别字/BOM)。参照现有 `stop_loss_mode` 下拉、`updateStopMode()` 显隐、`#per-share-thresholds` 表的写法。

**Interfaces:**
- Consumes: `/api/templates/<id>` 返回的新键(Task 1);`PUT /api/templates/<id>` 保存(routes 白名单已含新键,无需改后端)。

- [ ] **Step 1: 加 HTML 表单块**

在策略表单里(参照份额区间字段位置)加:

```html
<div class="form-group">
    <label>奖励最低份额下限</label>
    <input type="number" name="rewards_min_size_min" step="1">
</div>
<div class="form-group">
    <label>奖励最低份额上限</label>
    <input type="number" name="rewards_min_size_max" step="1">
</div>
<div class="form-group">
    <label>区间变量（tier_rules 阈值按此匹配）</label>
    <select name="tier_match_var" id="tier-match-var" onchange="updateTierMatchVar()">
        <option value="cumulative_thickness">累计厚度</option>
        <option value="risk_coefficient">风险系数</option>
    </select>
</div>
<div class="form-group" id="amount-value-group">
    <label>金额数值表（价格档[美分] → 数值；价超表则该档不挂）</label>
    <div id="amount-value-rows"></div>
    <button type="button" class="btn btn-sm" onclick="addAmountRow()">+ 价格档</button>
</div>
```

- [ ] **Step 2: 加 JS(显隐 + 表编辑 + 收集/回填)**

在 `<script>` 内加(参照 `renderTierEditor`/`serializeTierRules`):

```javascript
function updateTierMatchVar() {
    const v = (document.getElementById('tier-match-var') || {}).value || 'cumulative_thickness';
    const g = document.getElementById('amount-value-group');
    if (g) g.style.display = (v === 'risk_coefficient') ? '' : 'none';
}

function makeAmountRow(cents, value) {
    const row = document.createElement('div');
    row.className = 'amount-row';
    row.innerHTML =
        '价 ≤ <input type="number" class="av-cents" step="1" style="width:70px" value="' + cents + '"> ¢ ' +
        '→ <input type="number" class="av-value" step="0.1" style="width:70px" value="' + value + '"> ' +
        '<button type="button" class="btn btn-sm btn-danger" onclick="this.closest(\'.amount-row\').remove()">删</button>';
    return row;
}

function addAmountRow() {
    document.getElementById('amount-value-rows').appendChild(makeAmountRow(30, 1));
}

function renderAmountTable(table) {
    const box = document.getElementById('amount-value-rows');
    box.innerHTML = '';
    (table && table.length ? table : []).forEach(r => {
        box.appendChild(makeAmountRow(Math.round(r.upper * 100), r.value));
    });
}

function serializeAmountTable() {
    const rows = [];
    document.querySelectorAll('#amount-value-rows .amount-row').forEach(row => {
        const cents = parseFloat(row.querySelector('.av-cents').value);
        const value = parseFloat(row.querySelector('.av-value').value);
        if (!isNaN(cents) && !isNaN(value)) rows.push({upper: cents / 100, value: value});
    });
    return rows;
}
```

在 `loadStrategy` 的 `.then(data => {...})` 里(现有回填之后)加:

```javascript
        document.getElementById('tier-match-var').value =
            data.tier_match_var || 'cumulative_thickness';
        renderAmountTable(data.amount_value_table || []);
        updateTierMatchVar();
```

在策略 `submit` 处理器里(现有 `data.stop_loss_mode = ...` 附近)加:

```javascript
    data.tier_match_var = (document.getElementById('tier-match-var') || {}).value || 'cumulative_thickness';
    data.amount_value_table = serializeAmountTable();
```

（`rewards_min_size_min/max` 是 number 输入,已被现有 `input[type=number][name]` 收集/回填循环覆盖,无需单独处理。）

- [ ] **Step 3: 语法自检**

抽取 `<script>` 段跑 `node --check`,并查 BOM/中文别字:

Run: `python -c "import re;s=open('web/templates/config.html',encoding='utf-8').read();open('/tmp/cfg.js','w',encoding='utf-8').write('\n'.join(re.findall(r'<script>(.*?)</script>',s,re.S)))"` 然后 `node --check /tmp/cfg.js`
Expected: 无输出(通过)

- [ ] **Step 4: 手工走查(启动 app)**

启动后打开配置页:切「区间变量」下拉→金额表显隐正常;金额表增删行、保存后重开回填正常;份额上下限保存回填正常。参照 `/run` 或 `python app.py`。

- [ ] **Step 5: 提交**

```bash
git add web/templates/config.html
git commit -m "feat(config-ui): 区间变量下拉 + 金额数值表编辑器 + 奖励份额范围字段"
```

---

## Self-Review(计划自检)

- **Spec 覆盖**:① `min_reward_usd`②`min_settlement_days` 现有字段(无需任务,配置页已支持);③ 份额范围=Task 5+1;④⑤ tier_rules 现有;风险系数=Task 2/3/4;金额表=Task 2/3/4/7;配置字段=Task 1;UI=Task 7;调用点=Task 6。无遗漏。
- **占位符**:纯函数任务(1-5)含完整代码与测试;Task 5 测试因依赖现有夹具,给了断言+「执行时读现有夹具」指引(非占位,是防止臆造夹具形状)。
- **类型一致**:`match_value` 贯穿 `build_ladder` 返回 / `resolve_tier_share` 首参 / `compute_market_ladders` / `preview_market_ladders` / `_verbose_levels`;`amount_value(price, table)->float|None` 全程一致;新键名 `rewards_min_size_min/max`、`tier_match_var`、`amount_value_table` 全程一致。

## 复现目标策略(交付后自验)

建模板:`min_reward_usd=30`、`min_settlement_days=1`、`rewards_min_size_min=rewards_min_size_max=20`、区间变量=风险系数、金额表 `20¢→1/25¢→1.5/30¢→2`、各档 tier_rules=`[{阈值→min_size},{其余→skip}]`。跑 `pytest` 全绿 + 配置页走查。
