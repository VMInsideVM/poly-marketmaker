# 档位模块化挂单配置 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 挂单参数（规则1/2/3门槛、高位系数和门槛、金额数值表）与挂单份数改为按「市场最低奖励份额」精确匹配的档位模块（模板键 `size_tiers`）配置；未匹配任何已启用模块的市场不挂单。

**Architecture:** 新增 `engine/tiers.py` 纯函数（匹配/校验）；`engine/laddering.py` 四个纯函数加 `shares` 形参（系数与奖励资格线仍用市场最低份额）；scanner 的最低份额范围筛选替换为档位精确匹配；`place_orders` 与预演按市场档位取模块参数；7 个被取代的模板全局键从 `TEMPLATE_DEFAULTS`/配置页删除并迁移清库；配置页做档位卡片编辑器。

**Tech Stack:** Python + Flask + SQLite + pytest；前端原生 JS。

**Spec:** `docs/superpowers/specs/2026-07-13-size-tier-modules-design.md`

## Global Constraints

- UI 文案一律简体中文。
- **精确匹配**：市场 `rewards_min_size` 必须等于某个已启用模块的 `size` 才挂；无匹配 → 不挂（筛选层挡 + 下单层双点防御，照黑名单多拦截点先例）。
- **系数公式不变**：`coeff = 挂量 ÷ (市场最低份额 × 金额数值)`，与配置挂几份无关。
- 预算/敞口封顶后的放弃线仍是 `< 市场最低份额`（奖励资格线），不是配置份数。
- `gap_wide_cents`/`gap_mid_cents`/`cliff_probe_cents`/敞口/并发/筛选/离场参数保持模板级全局。
- 每模块字段固定为：`size`、`enabled`、`shares`、`rule1_min_coeff`、`rule2_min_coeff`、`rule3_min_coeff`、`gap_high_coeff_sum_min`、`amount_value_table`。
- 含中文的前端文件（Task 5 的 `markets.html` 小改、Task 7 全部）必须由主会话直接 Write（subagent 会写出别字/BOM），写后跑 node --check 与 BOM 检查。
- 提交只 stage 本任务文件。每个 Task 结束 `pytest -q` 全绿。
- 本计划完成 = 行为改变（升级后不配模块不挂单），发版为主版本号；发版动作不在本计划内。

---

### Task 1: `engine/tiers.py` 纯函数 + `TEMPLATE_DEFAULTS` 加 `size_tiers`

**Files:**
- Create: `engine/tiers.py`
- Modify: `config.py`（`TEMPLATE_DEFAULTS` 加一键；本任务不删旧键，删除在 Task 6）
- Test: `tests/test_tiers.py`

**Interfaces:**
- Produces:
  - `enabled_sizes(size_tiers: list | None) -> set[int]` — 已启用模块的档位值集合（Task 3 scanner、Task 6 dashboard 用）。
  - `tier_for(size_tiers: list | None, min_size) -> dict | None` — 精确匹配的已启用模块（Task 4 manager、Task 5 预演用）。
  - `validate_size_tiers(raw) -> tuple[list | None, str | None]` — 归一化列表或中文错误信息（Task 6 routes 校验用）。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_tiers.py`：

```python
"""tests/test_tiers.py — 档位模块解析/校验纯函数(不触网)。"""

from engine.tiers import enabled_sizes, tier_for, validate_size_tiers


def _tier(size, **over):
    t = {
        "size": size,
        "enabled": True,
        "shares": size if isinstance(size, int) else size,
        "rule1_min_coeff": 0,
        "rule2_min_coeff": 0,
        "rule3_min_coeff": 0,
        "gap_high_coeff_sum_min": 20,
        "amount_value_table": [{"upper": 0.31, "value": 1}],
    }
    t.update(over)
    return t


class TestEnabledSizes:
    def test_collects_enabled_only(self):
        assert enabled_sizes([_tier(20), _tier(50, enabled=False)]) == {20}

    def test_empty_and_none(self):
        assert enabled_sizes([]) == set()
        assert enabled_sizes(None) == set()

    def test_bad_entries_skipped(self):
        assert enabled_sizes([{"enabled": True, "size": "abc"}, _tier(20)]) == {20}


class TestTierFor:
    def test_exact_match_returns_module(self):
        t20 = _tier(20)
        assert tier_for([t20, _tier(50)], 20) is t20

    def test_no_match_returns_none(self):
        assert tier_for([_tier(20)], 30) is None

    def test_disabled_not_matched(self):
        assert tier_for([_tier(20, enabled=False)], 20) is None

    def test_bad_min_size_returns_none(self):
        assert tier_for([_tier(20)], None) is None
        assert tier_for(None, 20) is None


class TestValidateSizeTiers:
    def test_ok_normalizes_types(self):
        tiers, err = validate_size_tiers([_tier(20, shares="40")])
        assert err is None
        assert tiers[0]["size"] == 20 and tiers[0]["shares"] == 40
        assert tiers[0]["enabled"] is True

    def test_not_a_list(self):
        tiers, err = validate_size_tiers({"size": 20})
        assert tiers is None and err

    def test_shares_below_size_rejected(self):
        tiers, err = validate_size_tiers([_tier(20, shares=10)])
        assert tiers is None and "挂单份数" in err

    def test_duplicate_size_rejected(self):
        tiers, err = validate_size_tiers([_tier(20), _tier(20)])
        assert tiers is None and "重复" in err

    def test_non_integer_size_rejected(self):
        tiers, err = validate_size_tiers([_tier("abc", shares=20)])
        assert tiers is None and err

    def test_negative_threshold_rejected(self):
        tiers, err = validate_size_tiers([_tier(20, rule1_min_coeff=-1)])
        assert tiers is None and err


def test_template_defaults_has_size_tiers():
    from config import TEMPLATE_DEFAULTS

    assert TEMPLATE_DEFAULTS["size_tiers"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_tiers.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'engine.tiers'`）

- [ ] **Step 3: 实现**

创建 `engine/tiers.py`：

```python
"""engine/tiers.py — 档位模块(size_tiers)解析与校验纯函数(不触网)。

每个模块对「最低奖励份额 = size」的市场精确生效,携带该档的挂单份数与
选档参数(规则1/2/3门槛、高位系数和门槛、金额数值表)。无匹配 -> 不挂。
"""

_TIER_COEFF_KEYS = (
    "rule1_min_coeff",
    "rule2_min_coeff",
    "rule3_min_coeff",
    "gap_high_coeff_sum_min",
)


def enabled_sizes(size_tiers) -> set:
    """已启用模块的档位值集合(int)。非法条目跳过。"""
    out = set()
    for t in size_tiers or []:
        try:
            if t.get("enabled", False):
                out.add(int(t.get("size")))
        except (AttributeError, TypeError, ValueError):
            continue
    return out


def tier_for(size_tiers, min_size):
    """精确匹配:返回 enabled 且 size == min_size 的模块;无 -> None。"""
    try:
        ms = int(min_size)
    except (TypeError, ValueError):
        return None
    for t in size_tiers or []:
        try:
            if t.get("enabled", False) and int(t.get("size", -1)) == ms:
                return t
        except (AttributeError, TypeError, ValueError):
            continue
    return None


def validate_size_tiers(raw):
    """校验并归一化 size_tiers。返回 (归一化列表, None) 或 (None, 中文错误)。"""
    if not isinstance(raw, list):
        return None, "size_tiers 必须是数组"
    out, seen = [], set()
    for i, t in enumerate(raw):
        if not isinstance(t, dict):
            return None, f"第 {i + 1} 个档位不是对象"
        try:
            size = int(t.get("size"))
            shares = int(t.get("shares"))
        except (TypeError, ValueError):
            return None, f"第 {i + 1} 个档位的档位值/挂单份数必须是整数"
        if size <= 0:
            return None, f"第 {i + 1} 个档位的档位值必须为正整数"
        if size in seen:
            return None, f"档位值 {size} 重复"
        seen.add(size)
        if shares < size:
            return None, f"档位 {size} 的挂单份数({shares})不能小于档位值"
        norm = {"size": size, "enabled": bool(t.get("enabled", True)), "shares": shares}
        for k in _TIER_COEFF_KEYS:
            try:
                v = float(t.get(k, 0) or 0)
            except (TypeError, ValueError):
                return None, f"档位 {size} 的 {k} 必须是数字"
            if v < 0:
                return None, f"档位 {size} 的 {k} 不能为负"
            norm[k] = v
        table = t.get("amount_value_table") or []
        if not isinstance(table, list):
            return None, f"档位 {size} 的金额数值表格式错误"
        norm["amount_value_table"] = table
        out.append(norm)
    return out, None
```

`config.py` 的 `TEMPLATE_DEFAULTS` 中 `amount_value_table` 块之后加：

```python
    # 档位模块:按市场最低奖励份额(rewards_min_size)精确匹配的挂单参数组。
    # 每项 {size, enabled, shares, rule1/2/3_min_coeff, gap_high_coeff_sum_min,
    # amount_value_table}。空列表 = 无档可匹配 = 不挂单。
    "size_tiers": [],
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_tiers.py -q`
Expected: PASS（14 项）

- [ ] **Step 5: Commit**

```bash
git add engine/tiers.py config.py tests/test_tiers.py
git commit -m "feat(tiers): 档位模块纯函数(匹配/校验) + size_tiers 模板键"
```

---

### Task 2: laddering 四个纯函数加 `shares` 形参

**Files:**
- Modify: `engine/laddering.py`
- Test: `tests/test_gap_single.py`（追加）

**Interfaces:**
- Consumes: 无。
- Produces: `explain_gap_single_order(..., cliff_probe_cents=0, shares=None)`、`plan_gap_single_order(同)`、`compute_market_single_orders(..., cliff_probe_cents=0, shares=None)`、`preview_gap_single_market(..., cliff_probe_cents=0, shares=None)`；`shares=None` 沿用旧行为（挂 `min_size` 份）；preview 每侧输出新增 `chosen_shares`（跳过时 None）。Task 4/5 传 `shares=模块配置份数`。

- [ ] **Step 1: 写失败测试**

`tests/test_gap_single.py` 的 `_plan` helper 加 `shares=None` 形参并透传（在 `cliff_probe_cents=cliff` 后加 `shares=shares`），然后文件末尾追加：

```python
from engine.laddering import compute_market_single_orders, preview_gap_single_market


def _side(bids, min_size=20):
    return {
        "bids": bids,
        "reward_range_min": 0.10,
        "reward_range_max": 0.31,
        "min_size": min_size,
    }


def test_shares_param_overrides_min_size():
    # 档位模块配 40 份 -> 挂 40 份(不再等于 min_size 20)。
    out = _plan([_b(0.28, 50), _b(0.27, 40)], shares=40)
    assert out == (0.28, 40)


def test_shares_default_none_keeps_min_size():
    out = _plan([_b(0.28, 50), _b(0.27, 40)])
    assert out == (0.28, 20)


def test_coeff_still_uses_min_size_not_shares():
    # 系数按市场最低份额算:50/(20×2)=1.25 > 门槛 1.2 -> 挂。
    # 若误用 shares=40 会算成 0.625 < 1.2 被拒,本测试即失败。
    out = _plan([_b(0.28, 50), _b(0.27, 40)], x3=1.2, shares=40)
    assert out == (0.28, 40)


def test_compute_budget_caps_shares_but_keeps_above_min_size():
    # 配置 40 份、预算只够 30 份(30 ≥ min_size 20)-> 照挂 30(资格线以上不放弃)。
    # 7.6/0.25 = 30.4 -> 封顶 30。
    out = compute_market_single_orders(
        _side([_b(0.25, 50)]), None, 7.6, 500, AV, 10, 5, 20, 0, 0, 0, shares=40
    )
    assert out["a"] == [(0.25, 30)]


def test_compute_budget_below_min_size_drops_side():
    # 2.6/0.25 = 10.4 -> 封顶 10 < min_size 20 -> 放弃该侧。
    out = compute_market_single_orders(
        _side([_b(0.25, 50)]), None, 2.6, 500, AV, 10, 5, 20, 0, 0, 0, shares=40
    )
    assert out["a"] == []


def test_preview_reports_chosen_shares():
    side = dict(
        _side([_b(0.28, 50)]),
        outcome="YES",
        token_id="t",
        best_bid=0.28,
        best_ask=0.31,
        spread_cents=3.0,
    )
    out = preview_gap_single_market(side, None, AV, 10, 5, 20, 0, 0, 0, 0, shares=40)
    assert out["a"]["action"] == "place" and out["a"]["chosen_shares"] == 40


def test_preview_skip_has_none_chosen_shares():
    side = dict(
        _side([]),  # 无买档 -> skip
        outcome="YES",
        token_id="t",
        best_bid=None,
        best_ask=None,
        spread_cents=None,
    )
    out = preview_gap_single_market(side, None, AV, 10, 5, 20, 0, 0, 0, 0, shares=40)
    assert out["a"]["action"] == "skip" and out["a"]["chosen_shares"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_gap_single.py -q`
Expected: FAIL（`TypeError: ... unexpected keyword argument 'shares'`）

- [ ] **Step 3: 实现**

`engine/laddering.py` 四处修改：

1. `explain_gap_single_order` 形参表加 `shares=None`（`cliff_probe_cents=0` 之后）；docstring 的 `price/shares` 行改为 `price/shares: 选中档价 / int(shares)(缺省=min_size,即档位模块的挂单份数);跳过为 None`；选中赋值行改：

```python
            d["shares"] = int(shares) if shares is not None else int(min_size)
```

2. `plan_gap_single_order` 形参表加 `shares=None`，调用 `explain_gap_single_order` 时透传 `shares=shares`（`cliff_probe_cents` 之后改用关键字传参：`cliff_probe_cents=cliff_probe_cents, shares=shares`）。

3. `compute_market_single_orders` 形参表加 `shares=None`，调用 `plan_gap_single_order` 时透传 `shares=shares`（同上改关键字传参）。docstring 首行后补一句：`shares=档位模块配置的挂单份数(None=挂 min_size);封顶后不足 min_size(奖励资格线)才放弃该边。`

4. `preview_gap_single_market` 形参表加 `shares=None`，调用 `explain_gap_single_order` 时透传；`out[key]` 字典的 `"chosen_price": d["price"],` 之后加一行：

```python
            "chosen_shares": d["shares"],
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_gap_single.py -q`
Expected: PASS（既有 + 新增全绿）

- [ ] **Step 5: Commit**

```bash
git add engine/laddering.py tests/test_gap_single.py
git commit -m "feat(laddering): shares 形参与 min_size 解耦(系数/资格线仍用 min_size)"
```

---

### Task 3: scanner 档位精确匹配筛选

**Files:**
- Modify: `engine/scanner.py`（`filter_for_template`，现 402-407 行）
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: Task 1 `enabled_sizes`。
- Produces: `filter_for_template` 只放行 `rewards_min_size ∈ 已启用模块档位值` 的市场；模板无启用模块 → eligible 为空。

- [ ] **Step 1: 更新/新增测试**

`tests/test_scanner.py`：

1. 模块顶部（import 区之后）加 helper：

```python
def _tier(size, shares=None, **over):
    t = {
        "size": size,
        "enabled": True,
        "shares": shares or size,
        "rule1_min_coeff": 0,
        "rule2_min_coeff": 0,
        "rule3_min_coeff": 0,
        "gap_high_coeff_sum_min": 20,
        "amount_value_table": [{"upper": 1.0, "value": 1}],
    }
    t.update(over)
    return t
```

2. `TestFilterForTemplate._template` 的字典加一键（默认候选 `min_size=100`，全体既有测试因此照常通过）：

```python
            "size_tiers": [_tier(100)],
```

3. 替换 `test_min_size_over_250_excluded` 为：

```python
    def test_no_matching_tier_excluded(self):
        scanner = self._scanner()
        # 奖励够高,但 min_size 300 没有任何已启用档位模块能精确对上 -> 剔除。
        pool = [self._candidate("C", [], daily_reward=200, min_size=300)]
        assert scanner.filter_for_template(pool, self._template(), "0xW") == []
```

4. 替换 `test_rewards_min_size_exact_match_only` 与 `test_rewards_min_size_default_range_passes_all` 为：

```python
    def test_tier_exact_match_only(self):
        scanner = self._scanner()
        pool = [
            self._candidate("A", [], daily_reward=20, min_size=20),
            self._candidate("B", [], daily_reward=50, min_size=50),
        ]
        tmpl = self._template(size_tiers=[_tier(20)])
        out = scanner.filter_for_template(pool, tmpl, "0xW")
        assert {e["rewards_min_size"] for e in out} == {20}

    def test_multiple_enabled_tiers_pass_their_sizes(self):
        scanner = self._scanner()
        pool = [
            self._candidate("A", [], daily_reward=20, min_size=20),
            self._candidate("B", [], daily_reward=50, min_size=50),
        ]
        tmpl = self._template(size_tiers=[_tier(20), _tier(50)])
        out = scanner.filter_for_template(pool, tmpl, "0xW")
        assert {e["rewards_min_size"] for e in out} == {20, 50}

    def test_disabled_tier_excluded(self):
        scanner = self._scanner()
        pool = [
            self._candidate("A", [], daily_reward=20, min_size=20),
            self._candidate("B", [], daily_reward=50, min_size=50),
        ]
        tmpl = self._template(size_tiers=[_tier(20), _tier(50, enabled=False)])
        out = scanner.filter_for_template(pool, tmpl, "0xW")
        assert {e["rewards_min_size"] for e in out} == {20}

    def test_no_tiers_yields_empty(self):
        scanner = self._scanner()
        pool = [self._candidate("A", [], daily_reward=20, min_size=20)]
        assert scanner.filter_for_template(pool, self._template(size_tiers=[]), "0xW") == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_scanner.py -q`
Expected: 新测试 FAIL（旧代码按范围筛选，`size_tiers` 不生效）

- [ ] **Step 3: 实现**

`engine/scanner.py`：文件顶部 import 区加 `from engine.tiers import enabled_sizes`；`filter_for_template` 开头（`include_other = ...` 之后）加：

```python
        tier_sizes = enabled_sizes(template.get("size_tiers") or [])
```

把这段（现 403-407 行）：

```python
            # 最低份额范围筛选(可配);硬顶 250(超档无取档)。默认 1/250 = 放行全部合法档。
            size_lo = max(1, int(template.get("rewards_min_size_min", 1) or 1))
            size_hi = min(250, int(template.get("rewards_min_size_max", 250) or 250))
            if not (size_lo <= min_size <= size_hi):
                continue
```

替换为：

```python
            # 档位模块精确匹配:最低份额必须等于某个已启用模块的档位值,否则不做。
            if min_size not in tier_sizes:
                continue
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `pytest tests/test_scanner.py -q && pytest -q`
Expected: PASS。若 `tests/test_manager.py` 因 `db.get_template_for` mock 缺 `size_tiers` 导致「eligible 为空」类断言失败，在其 `_make_manager` 的 `db.get_template_for.return_value` 字典里加：

```python
        "size_tiers": [
            {
                "size": 100, "enabled": True, "shares": 100,
                "rule1_min_coeff": 0, "rule2_min_coeff": 0, "rule3_min_coeff": 0,
                "gap_high_coeff_sum_min": 20,
                "amount_value_table": [{"upper": 1.0, "value": 1}],
            }
        ],
```

- [ ] **Step 5: Commit**

```bash
git add engine/scanner.py tests/test_scanner.py
git commit -m "feat(scanner): 最低份额范围筛选改为档位模块精确匹配"
```

（若改了 `tests/test_manager.py` 一并 stage。）

---

### Task 4: `place_orders` 按档取参数与份数

**Files:**
- Modify: `engine/manager.py`（`WalletWorker.place_orders`）
- Test: `tests/test_place_orders.py`

**Interfaces:**
- Consumes: Task 1 `tier_for`；Task 2 `compute_market_single_orders(..., shares=)`/`explain_gap_single_order(..., shares=)`。
- Produces: 无匹配档位的市场在下单层被 continue（双点防御）；断层判断参数与挂单份数来自模块。

- [ ] **Step 1: 更新/新增测试**

`tests/test_place_orders.py`：

1. 模块顶部加 helper：

```python
def _tier(size, shares=None, av=None):
    return {
        "size": size,
        "enabled": True,
        "shares": shares or size,
        "rule1_min_coeff": 0,
        "rule2_min_coeff": 0,
        "rule3_min_coeff": 0,
        "gap_high_coeff_sum_min": 20,
        "amount_value_table": av or [{"upper": 1.0, "value": 1}],
    }
```

2. `_make_worker` 的 `tmpl` 字典：删掉 `"amount_value_table"`、`"gap_high_coeff_sum_min"`、`"rule1_min_coeff"`、`"rule2_min_coeff"`、`"rule3_min_coeff"` 五键，加：

```python
        "size_tiers": [_tier(100)],
```

（保留 `gap_wide_cents`/`gap_mid_cents`/`cliff_probe_cents` 等其余键。）

3. `_gap_template()` 整体替换为（顺带清掉早已死掉的 `placement_mode`/`tier_rules` 键）：

```python
def _gap_template():
    return {
        "size_tiers": [
            _tier(
                20,
                av=[
                    {"upper": 0.20, "value": 1},
                    {"upper": 0.25, "value": 1.5},
                    {"upper": 0.31, "value": 2},
                ],
            )
        ],
        "gap_wide_cents": 10,
        "gap_mid_cents": 5,
    }
```

同样把 `test_gap_single_places_one_order_highest_qualifying` 里内联的 template 字典替换成 `template=_gap_template()`。

4. 文件末尾追加：

```python
def test_no_matching_tier_market_skipped():
    # 双点防御:eligible 里混进无档市场(配置变更时差)-> 下单层也要跳过。
    worker, api, db = _make_worker()  # 模板只有 100 档
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=50)])
    api.place_limit_buy.assert_not_called()


def test_tier_shares_used_for_order_size():
    # 档位 100 配 150 份 -> 挂 150(预算/敞口都够)。
    worker, api, db = _make_worker(template={"size_tiers": [_tier(100, shares=150)]})
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    assert api.place_limit_buy.call_count == 1
    assert api.place_limit_buy.call_args_list[0].args[2] == 150
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_place_orders.py -q`
Expected: 大量 FAIL（place_orders 仍读模板全局键，mock 模板里已没有）

- [ ] **Step 3: 实现**

`engine/manager.py`：

1. 顶部 import 区加 `from engine.tiers import tier_for`。

2. `place_orders` 里模板参数读取段（现 154-162 行）替换为：

```python
        tmpl = self.db.get_template_for(self.wallet_address)
        size_tiers = tmpl.get("size_tiers") or []
        gap_wide_cents = float(tmpl.get("gap_wide_cents", 10))
        gap_mid_cents = float(tmpl.get("gap_mid_cents", 5))
        cliff_probe_cents = float(tmpl.get("cliff_probe_cents", 2))
```

3. 市场循环里，`if self.db.is_in_cooldown(...): continue` 之后、max_concurrent 检查之前加：

```python
            # 档位模块精确匹配(筛选层已挡;这里双点防御,防配置变更/共享列表时差)。
            tier = tier_for(size_tiers, grouped[mid][0].get("rewards_min_size"))
            if tier is None:
                continue
            amount_value_table = tier.get("amount_value_table") or None
            gap_high_coeff_sum_min = float(tier.get("gap_high_coeff_sum_min", 20))
            rule1_min_coeff = float(tier.get("rule1_min_coeff", 0))
            rule2_min_coeff = float(tier.get("rule2_min_coeff", 0))
            rule3_min_coeff = float(tier.get("rule3_min_coeff", 0))
            tier_shares = int(tier.get("shares", 0) or 0)
```

4. `compute_market_single_orders(...)` 调用在 `cliff_probe_cents,` 之后加 `shares=tier_shares,`；同函数下方 `explain_gap_single_order(...)` 调用同样在 `cliff_probe_cents,` 之后加 `shares=tier_shares,`。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_place_orders.py tests/test_manager.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/manager.py tests/test_place_orders.py
git commit -m "feat(place): 断层判断参数与挂单份数按档位模块取(无档双点防御跳过)"
```

---

### Task 5: 预演按档解析（路由 + markets.html 份数显示）

⚠️ `markets.html` 含中文，由主会话直接改。

**Files:**
- Modify: `web/routes.py`（`_ladder_payload`）
- Modify: `web/templates/markets.html`（预演行加份数）
- Test: `tests/test_markets_route.py`

**Interfaces:**
- Consumes: Task 1 `tier_for`；Task 2 `preview_gap_single_market(..., shares=)` 与 `chosen_shares` 字段。
- Produces: `/api/markets/<id>/ladder` 每侧新增 `chosen_shares`；无匹配档位时每侧返回 `action:"skip"`、`skip_reason:"无匹配档位模块（最低份额 X）"`。

- [ ] **Step 1: 更新/新增测试**

`tests/test_markets_route.py`：

1. `_FakeDB.get_template_for` 返回字典加：

```python
            "size_tiers": [
                {
                    "size": 100, "enabled": True, "shares": 120,
                    "rule1_min_coeff": 0, "rule2_min_coeff": 0, "rule3_min_coeff": 0,
                    "gap_high_coeff_sum_min": 20,
                    "amount_value_table": [{"upper": 1.0, "value": 1}],
                }
            ],
```

（`_FakeDB` 原有的 `max_exposure_usd` 等键保留。）

2. 文件末尾追加：

```python
def test_ladder_uses_tier_shares(client):
    r = client.get("/api/markets/c1/ladder?wallet=0xw")
    side = r.get_json()["sides"][0]
    assert side["action"] == "place" and side["chosen_shares"] == 120


def test_ladder_no_matching_tier_shows_skip(client, monkeypatch):
    class NoTierDB(_FakeDB):
        def get_template_for(self, addr):
            t = dict(_FakeDB.get_template_for(self, addr))
            t["size_tiers"] = []
            return t

    monkeypatch.setattr(routes, "db", NoTierDB())
    r = client.get("/api/markets/c1/ladder?wallet=0xw")
    assert r.status_code == 200
    side = r.get_json()["sides"][0]
    assert side["action"] == "skip"
    assert "无匹配档位模块" in side["skip_reason"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_markets_route.py -q`
Expected: 新测试 FAIL（无 `chosen_shares`；无档时仍按旧全局参数预演）

- [ ] **Step 3: 实现路由**

`web/routes.py` `_ladder_payload`：

1. 函数开头 import 行改为：

```python
    from engine.laddering import preview_gap_single_market
    from engine.strategy import reward_price_range
    from engine.positions import held_side_info
    from engine.tiers import tier_for
```

2. 模板参数读取段（`tmpl = db.get_template_for(addr)` 之后）删掉 `amount_value_table = ...` 行，改为：

```python
    tmpl = db.get_template_for(addr)
    size_tiers = tmpl.get("size_tiers") or []
    max_exposure_usd = float(tmpl.get("max_exposure_usd", 250))
    max_exposure_shares = int(tmpl.get("max_exposure_shares", 500))
```

3. `market = rows[0]` 之后加一行（并把 sides_in 循环里的 `"min_size": int(market.get(...))` 改用该变量）：

```python
    min_size = int(market.get("rewards_min_size", 0) or 0)
```

4. 把 `preview = preview_gap_single_market(...)` 与 `sides = ...` 两句替换为：

```python
    tier = tier_for(size_tiers, min_size)
    if tier is None:
        # 无匹配档位模块:该市场不挂,预演每侧给出跳过原因(与下单层口径一致)。
        sides = [
            {
                "outcome": s.get("outcome", ""),
                "token_id": s.get("token_id", ""),
                "best_bid": s.get("best_bid"),
                "best_ask": s.get("best_ask"),
                "spread_cents": s.get("spread_cents"),
                "reward_range": [s["reward_range_min"], s["reward_range_max"]],
                "rule": None,
                "rule_label": "无匹配档位",
                "max_gap": 0.0,
                "min_coeff": None,
                "high_sum": None,
                "gate_passed": False,
                "action": "skip",
                "chosen_index": None,
                "chosen_price": None,
                "chosen_shares": None,
                "skip_reason": f"无匹配档位模块（最低份额 {min_size}）",
                "cliff": False,
                "levels": [],
            }
            for s in sides_in
        ]
    else:
        preview = preview_gap_single_market(
            a,
            b,
            tier.get("amount_value_table") or None,
            float(tmpl.get("gap_wide_cents", 10)),
            float(tmpl.get("gap_mid_cents", 5)),
            float(tier.get("gap_high_coeff_sum_min", 20)),
            float(tier.get("rule1_min_coeff", 0)),
            float(tier.get("rule2_min_coeff", 0)),
            float(tier.get("rule3_min_coeff", 0)),
            float(tmpl.get("cliff_probe_cents", 2)),
            shares=int(tier.get("shares", 0) or 0) or None,
        )
        sides = [preview[k] for k in ("a", "b") if preview.get(k)]
```

- [ ] **Step 4: markets.html 显示份数（主会话执行）**

`web/templates/markets.html` 现 180 行：

```
? `<span class="profit">✓ 挂一单 @ ${fmtP(s.chosen_price)}（第 ${s.chosen_index + 1} 档，系数 > 门槛 ${minCoeff}）</span>`
```

改为：

```
? `<span class="profit">✓ 挂一单 @ ${fmtP(s.chosen_price)} × ${s.chosen_shares != null ? s.chosen_shares : '?'} 份（第 ${s.chosen_index + 1} 档，系数 > 门槛 ${minCoeff}）</span>`
```

改完检查：

```bash
cd "C:/Users/Hank/PycharmProjects/poly简单做市"
python -c "import re,io;src=io.open('web/templates/markets.html',encoding='utf-8').read();io.open('mk_check.js','w',encoding='utf-8').write('\n'.join(re.findall(r'<script>(.*?)</script>',src,re.S)))"
node --check mk_check.js && rm mk_check.js
python -c "print('BOM' if open('web/templates/markets.html','rb').read(3)==b'\xef\xbb\xbf' else 'OK')"
```

Expected: node --check 通过、输出 `OK`。（若该文件 script 里存在 Jinja 模板语法导致 node --check 误报，退化为只目检本次改动行的语法与中文。）

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_markets_route.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add web/routes.py web/templates/markets.html tests/test_markets_route.py
git commit -m "feat(preview): 预演按档位模块解析参数与份数,无档市场显式跳过"
```

---

### Task 6: 死键清理 + 迁移 + 保存校验 + 仪表盘数据

**Files:**
- Modify: `config.py`（`TEMPLATE_DEFAULTS` 删 7 键）
- Modify: `models/database.py`（`_migrate` 清死键）
- Modify: `web/routes.py`（`api_save_settings`/`api_save_template` 校验 `size_tiers`；`api_dashboard` 加 `templates_without_tiers`）
- Test: `tests/test_database.py`、`tests/test_settings_routes.py`

**Interfaces:**
- Consumes: Task 1 `validate_size_tiers`、`enabled_sizes`。
- Produces: `TEMPLATE_DEFAULTS` 不再含 7 个被取代键（`/api/settings`、`/api/templates/<tid>` 因白名单自动停收）；启动迁移幂等删除 `template_settings`/`settings` 里的存量死键行；非法 `size_tiers` 保存返回 400；`/api/dashboard` 返回 `templates_without_tiers: [模板名]`（Task 7 前端消费）。

- [ ] **Step 1: 更新/新增测试**

1. `tests/test_database.py`：替换 `test_template_defaults_has_size_range_and_amount_table` 为：

```python
def test_template_defaults_size_tiers_replace_global_keys():
    from config import TEMPLATE_DEFAULTS

    assert TEMPLATE_DEFAULTS["size_tiers"] == []
    for dead in (
        "rule1_min_coeff",
        "rule2_min_coeff",
        "rule3_min_coeff",
        "gap_high_coeff_sum_min",
        "amount_value_table",
        "rewards_min_size_min",
        "rewards_min_size_max",
    ):
        assert dead not in TEMPLATE_DEFAULTS, f"被取代键 {dead} 应已删除"


def test_migration_deletes_superseded_template_keys(tmp_path):
    db = Database(str(tmp_path / "mig.db"))
    db.init()
    tid = db.get_default_template_id()
    db.conn.execute(
        "INSERT OR REPLACE INTO template_settings (template_id, key, value)"
        " VALUES (?, ?, ?)",
        (tid, "rule1_min_coeff", "3"),
    )
    db.conn.commit()
    db.close()
    db2 = Database(str(tmp_path / "mig.db"))
    db2.init()  # 再次 init 触发迁移
    try:
        c = db2.conn.cursor()
        c.execute(
            "SELECT COUNT(*) AS n FROM template_settings WHERE key = 'rule1_min_coeff'"
        )
        assert c.fetchone()["n"] == 0
    finally:
        db2.close()
```

2. `tests/test_settings_routes.py`：`test_post_settings_roundtrips_gap_single_keys` 里 payload 与断言删掉 `gap_high_coeff_sum_min`/`rule1_min_coeff`/`rule2_min_coeff`/`rule3_min_coeff` 四键（保留 `gap_wide_cents`/`gap_mid_cents`/`take_profit_mode`/`stop_loss_mode`），并在文件末尾追加：

```python
def _tier_payload(**over):
    t = {
        "size": 20,
        "enabled": True,
        "shares": 40,
        "rule1_min_coeff": 0,
        "rule2_min_coeff": 0,
        "rule3_min_coeff": 0,
        "gap_high_coeff_sum_min": 20,
        "amount_value_table": [{"upper": 0.2, "value": 1}],
    }
    t.update(over)
    return t


def test_size_tiers_roundtrip_via_template_put(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    tid = db.get_default_template_id()
    r = client.put(f"/api/templates/{tid}", json={"size_tiers": [_tier_payload()]})
    assert r.status_code == 200
    saved = db.get_template(tid)["size_tiers"]
    assert saved[0]["size"] == 20 and saved[0]["shares"] == 40


def test_size_tiers_invalid_rejected_400(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    tid = db.get_default_template_id()
    # shares < size
    r = client.put(f"/api/templates/{tid}", json={"size_tiers": [_tier_payload(shares=10)]})
    assert r.status_code == 400 and "挂单份数" in r.get_json()["error"]
    # size 重复
    r = client.put(
        f"/api/templates/{tid}",
        json={"size_tiers": [_tier_payload(), _tier_payload()]},
    )
    assert r.status_code == 400 and "重复" in r.get_json()["error"]
    # /api/settings 同样拦
    r = client.post("/api/settings", json={"size_tiers": [_tier_payload(shares=1)]})
    assert r.status_code == 400


def test_dead_template_keys_no_longer_stored(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    client.post("/api/settings", json={"rule1_min_coeff": 9, "amount_value_table": []})
    tmpl = db.get_template(db.get_default_template_id())
    assert "rule1_min_coeff" not in tmpl and "amount_value_table" not in tmpl
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_database.py tests/test_settings_routes.py -q`
Expected: FAIL（defaults 仍含旧键；无迁移；无校验）

- [ ] **Step 3: 实现**

1. `config.py` `TEMPLATE_DEFAULTS`：删掉 `rule1_min_coeff`/`rule2_min_coeff`/`rule3_min_coeff` 三行及其注释、`gap_high_coeff_sum_min` 行、`rewards_min_size_min`/`rewards_min_size_max` 两行及其注释、`amount_value_table` 整块及其注释（`size_tiers` 键及注释保留，并把注释首行改为说明金额表已入档位模块）。

2. `models/database.py` `_migrate` 末尾（templates 迁移块之后）加：

```python
        # 档位模块(size_tiers)取代 7 个模板级全局键:清掉存量死键行(幂等,SP6d 手法)。
        superseded = (
            "rule1_min_coeff",
            "rule2_min_coeff",
            "rule3_min_coeff",
            "gap_high_coeff_sum_min",
            "amount_value_table",
            "rewards_min_size_min",
            "rewards_min_size_max",
        )
        ph = ",".join("?" * len(superseded))
        c.execute(f"DELETE FROM template_settings WHERE key IN ({ph})", superseded)
        c.execute(f"DELETE FROM settings WHERE key IN ({ph})", superseded)
        self.conn.commit()
```

3. `web/routes.py`：

`api_save_settings` 的 `strategy = {...}` 行之后加：

```python
    if "size_tiers" in strategy:
        from engine.tiers import validate_size_tiers

        tiers, err = validate_size_tiers(strategy["size_tiers"])
        if err:
            return jsonify({"error": err}), 400
        strategy["size_tiers"] = tiers
```

`api_save_template` 的 `strategy = {...}` 行之后加同样的块（`db.save_template(tid, strategy)` 之前）。

`api_dashboard` 的 `return jsonify({...})` 前加：

```python
    # 无启用档位模块的模板:绑定它的钱包一张单都不会挂,仪表盘要醒目提示。
    templates_without_tiers = []
    try:
        from engine.tiers import enabled_sizes

        for t in db.list_templates():
            if not enabled_sizes(db.get_template(t["id"]).get("size_tiers") or []):
                templates_without_tiers.append(t["name"])
    except Exception:
        templates_without_tiers = []
```

并在返回字典里加 `"templates_without_tiers": templates_without_tiers,`。

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `pytest tests/test_database.py tests/test_settings_routes.py -q && pytest -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add config.py models/database.py web/routes.py tests/test_database.py tests/test_settings_routes.py
git commit -m "feat(tiers): 删7个被取代模板键+启动迁移清库+size_tiers 保存校验+仪表盘无档模板提示数据"
```

---

### Task 7: 配置页档位卡片编辑器 + 仪表盘提示条

⚠️ 本任务全部是含中文前端，必须由主会话直接执行，不派 subagent。

**Files:**
- Modify: `web/templates/config.html`
- Modify: `web/templates/dashboard.html`

**Interfaces:**
- Consumes: Task 6 的 `size_tiers` 保存校验、`/api/dashboard` 的 `templates_without_tiers`。
- Produces: 档位模块可视化增删改；被取代的全局表单字段消失。

- [ ] **Step 1: config.html 表单结构**

1. 删掉 form-grid 里「奖励最低份额下限/上限」两个 form-group（现 64-71 行）。

2. `#gap-single-params` 的 form-grid 里删掉「规则1 高位风险系数和门槛」「规则1/2/3 选档系数门槛」四个 form-group（保留宽/中断层阈值与悬崖探测）；其下的说明段 `<p class="hint">` 替换为：

```html
            <p class="hint" style="color:#888;font-size:12px">按买单簿「奖励区间内相邻档最大价差」把整个市场归到三级之一：&gt;宽断层→规则1（再查断层上方各档风险系数之和≥门槛，不过则整市场不挂）；中~宽→规则2；&lt;中→规则3。各级的「选档系数门槛」「高位系数和门槛」「金额数值表」按下方档位模块逐档配置。风险系数＝盘口量÷(最低份数×金额数值)。</p>
```

3. 删掉「金额数值表」整段（`<h3>金额数值表</h3>` 到其 `<p class="hint">` 为止），原位置替换为档位模块编辑器：

```html
        <h3>档位模块（按市场最低奖励份额精确匹配）</h3>
        <div id="size-tiers"></div>
        <button type="button" class="btn" onclick="addTierCard()">+ 添加档位</button>
        <p class="hint" style="color:#888;font-size:12px">每个模块只对「最低奖励份额 = 档位值」的市场生效：勾选启用、设挂单份数（≥档位值）和该档的选档参数。没有任何已启用模块能对上的市场<b>不会挂单</b>。金额数值表按档配置：价格档[美分]→数值，价超表的档不挂。</p>
```

- [ ] **Step 2: config.html JS**

1. `makeAmountRow`/`addAmountRow`/`renderAmountTable`/`serializeAmountTable` 四个函数中，保留 `makeAmountRow` 原样，删掉后三个，新增：

```javascript
function addTierAmountRow(btn) {
    btn.closest('.form-group').querySelector('.st-amount-rows').appendChild(makeAmountRow(30, 1));
}

function makeTierCard(t) {
    const card = document.createElement('div');
    card.className = 'tier-card';
    card.style.cssText = 'border:1px solid #8884;border-radius:6px;padding:10px;margin:8px 0';
    card.innerHTML = `
      <div class="form-inline" style="gap:12px;align-items:center">
        <label><input type="checkbox" class="st-enabled" ${t.enabled !== false ? 'checked' : ''}> 启用</label>
        <label>档位值（最低奖励份额） <input type="number" class="st-size" step="1" style="width:80px" value="${t.size ?? ''}"></label>
        <label>挂单份数（≥档位值） <input type="number" class="st-shares" step="1" style="width:80px" value="${t.shares ?? ''}"></label>
        <button type="button" class="btn btn-sm btn-danger" onclick="this.closest('.tier-card').remove()">删除档位</button>
      </div>
      <div class="form-grid" style="margin-top:6px">
        <div class="form-group"><label>规则1 选档系数门槛（宽断层）</label><input type="number" class="st-r1" step="0.1" value="${t.rule1_min_coeff ?? 0}"></div>
        <div class="form-group"><label>规则2 选档系数门槛（中断层）</label><input type="number" class="st-r2" step="0.1" value="${t.rule2_min_coeff ?? 0}"></div>
        <div class="form-group"><label>规则3 选档系数门槛（密盘）</label><input type="number" class="st-r3" step="0.1" value="${t.rule3_min_coeff ?? 0}"></div>
        <div class="form-group"><label>规则1 高位风险系数和门槛</label><input type="number" class="st-gate" step="0.1" value="${t.gap_high_coeff_sum_min ?? 20}"></div>
      </div>
      <div class="form-group"><label>金额数值表（价格档[美分] → 数值）</label>
        <div class="st-amount-rows"></div>
        <button type="button" class="btn btn-sm" onclick="addTierAmountRow(this)">+ 价格档</button>
      </div>`;
    const box = card.querySelector('.st-amount-rows');
    (t.amount_value_table || []).forEach(r => box.appendChild(makeAmountRow(Math.round(r.upper * 100), r.value)));
    return card;
}

function addTierCard() {
    document.getElementById('size-tiers').appendChild(makeTierCard({
        size: '', shares: '', gap_high_coeff_sum_min: 20,
        amount_value_table: [
            {upper: 0.20, value: 1},
            {upper: 0.25, value: 1.5},
            {upper: 0.31, value: 2},
        ],
    }));
}

function renderTiers(list) {
    const box = document.getElementById('size-tiers');
    box.innerHTML = '';
    if (!list || !list.length) {
        box.innerHTML = '<p class="hint" style="color:#c60">⚠️ 该模板尚未配置档位模块，不会挂单——点「+ 添加档位」创建。</p>';
        return;
    }
    list.forEach(t => box.appendChild(makeTierCard(t)));
}

function serializeTiers() {
    const tiers = [], seen = new Set();
    let error = null;
    document.querySelectorAll('#size-tiers .tier-card').forEach(card => {
        if (error) return;
        const size = parseInt(card.querySelector('.st-size').value, 10);
        const shares = parseInt(card.querySelector('.st-shares').value, 10);
        if (isNaN(size) || size <= 0) { error = '档位值必须是正整数'; return; }
        if (seen.has(size)) { error = '档位值 ' + size + ' 重复'; return; }
        seen.add(size);
        if (isNaN(shares) || shares < size) { error = '档位 ' + size + ' 的挂单份数必须 ≥ 档位值'; return; }
        const rows = [];
        card.querySelectorAll('.st-amount-rows .amount-row').forEach(row => {
            const c = parseFloat(row.querySelector('.av-cents').value);
            const v = parseFloat(row.querySelector('.av-value').value);
            if (!isNaN(c) && !isNaN(v)) rows.push({upper: c / 100, value: v});
        });
        tiers.push({
            size: size,
            enabled: card.querySelector('.st-enabled').checked,
            shares: shares,
            rule1_min_coeff: parseFloat(card.querySelector('.st-r1').value) || 0,
            rule2_min_coeff: parseFloat(card.querySelector('.st-r2').value) || 0,
            rule3_min_coeff: parseFloat(card.querySelector('.st-r3').value) || 0,
            gap_high_coeff_sum_min: parseFloat(card.querySelector('.st-gate').value) || 0,
            amount_value_table: rows,
        });
    });
    return {tiers: tiers, error: error};
}
```

2. `loadStrategy` 里把 `renderAmountTable(data.amount_value_table || []);` 替换为 `renderTiers(data.size_tiers || []);`。

3. strategy-form submit 处理里删掉 `data.amount_value_table = serializeAmountTable();`，在 `data.take_profit_mode = ...` 之后加：

```javascript
    const st = serializeTiers();
    if (st.error) { alert(st.error); return; }
    data.size_tiers = st.tiers;
```

并把保存的 `.then(r => r.json()).then(() => alert('策略参数已保存（下次引擎启动生效）'));` 改为对 400 报错可见：

```javascript
    }).then(r => r.json()).then(resp => {
        if (resp && resp.error) { alert(resp.error); return; }
        alert('策略参数已保存（下次引擎启动生效）');
    });
```

- [ ] **Step 3: dashboard.html 提示条**

`web/templates/dashboard.html` 的 `<div class="page-header">...</div>` 之后加：

```html
<div id="tier-warning" class="flash" style="display:none"></div>
```

`refreshDashboard()` 里 `document.getElementById('total-orders')...` 之前加：

```javascript
        const tw = data.templates_without_tiers || [];
        const warn = document.getElementById('tier-warning');
        if (tw.length) {
            warn.style.display = '';
            warn.textContent = '⚠️ 模板「' + tw.join('」「') + '」未配置档位模块，绑定这些模板的钱包不会挂单（去 配置 → 档位模块 添加）';
        } else {
            warn.style.display = 'none';
        }
```

- [ ] **Step 4: JS 语法与编码检查**

```bash
cd "C:/Users/Hank/PycharmProjects/poly简单做市"
for f in config dashboard; do
  python -c "import re,io;src=io.open('web/templates/$f.html',encoding='utf-8').read();io.open('${f}_check.js','w',encoding='utf-8').write('\n'.join(re.findall(r'<script>(.*?)</script>',src,re.S)))"
  node --check ${f}_check.js && rm ${f}_check.js
  python -c "print('BOM' if open('web/templates/$f.html','rb').read(3)==b'\xef\xbb\xbf' else 'OK')"
done
```

Expected: 两个文件 node --check 通过、各输出 `OK`。再目检中文无别字。

- [ ] **Step 5: 全量回归 + Commit**

Run: `pytest -q`
Expected: PASS

```bash
git add web/templates/config.html web/templates/dashboard.html
git commit -m "feat(config): 档位模块卡片编辑器替代全局挂单参数;仪表盘无档模板提示"
```

---

### Task 8: 文档更新（README + CLAUDE.md）

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: README**

1. 参数表（现 191/193 行附近）：删掉 `rewards_min_size_min / rewards_min_size_max` 行与 `amount_value_table` 行，原位置加：

```markdown
| `size_tiers` | （空） | 档位模块：按市场最低奖励份额**精确匹配**；每档配 启用 / 挂单份数(≥档位值) / 规则1-3选档门槛 / 高位系数和门槛 / 金额数值表。没有任何已启用模块能对上的市场不挂单 |
```

2. 现 199 行「风险系数」说明句尾补一句：`金额数值表与各门槛按档位模块逐档配置。`

- [ ] **Step 2: CLAUDE.md**

1. Architecture 的 scanner 段：把 `filters reward markets (reward ≥ threshold, settlement days, price band, spread, cooldown)` 改为 `filters reward markets (reward ≥ threshold, settlement days, price band, spread, cooldown, and an exact size-tier match: rewards_min_size must equal an enabled size_tiers module)`。

2. 同段末尾加一句：

```
Placement parameters (rule1/2/3 coeff thresholds, 高位系数和门槛, amount_value_table) and the order share count live in per-template `size_tiers` modules keyed by the market's `rewards_min_size` (exact match; a market with no matching enabled tier is never placed, and its resting buys are removed by the dropout-cancel pass on the next placement round). Gap-tier thresholds (gap_wide/mid_cents), cliff probe, exposure and exit parameters stay template-global.
```

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: 档位模块化挂单(size_tiers)说明,替换最低份额范围/全局金额表描述"
```

---

## 收尾

- 全量 `pytest -q` 全绿后本计划完成。
- 发版：主版本号（行为改变——升级后不配档位模块不挂单），与「账户净值历史」合并发布；更新公告须写明「升级后须到 配置 → 档位模块 为每个模板添加档位」。发版动作由用户触发 `release.ps1`，不在本计划内。
- 人工走查（建议）：配置页建 20/50 两档 → 扫描 → 市场发现里确认非 20/50 档市场消失、预演显示「× N 份」；停用一档确认下一轮 dropout 撤单。
