# SP4 单份奖励阈值 + 取档 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 v4 §2/§3 给 `filter_for_template` 加两道筛选闸——最低份额 `0<≤250` 固定闸 + 单份奖励(每日LP奖励÷最低份数)≥ 取档阈值;`reward_bracket` 向上取档(20/50/100/200/250);模板键 `per_share_reward_thresholds`(5 档各自可调,默认 0.30)。

**Architecture:** 纯函数 `reward_bracket`(scanner.py 模块级)+ `filter_for_template` 按市场段两道闸 + 一个策略级模板键。取档仅服务筛选(不碰挂单/离场)。全程附加,无退役。

**Tech Stack:** Python 3.12 / pytest(临时库、MagicMock 桩 API)。

**执行顺序:** T1 纯函数(附加)→ T2 config 键(附加)→ T3 filter 两道闸(用 T1 函数 + 读 T2 键,缺省回退 0.30)。每提交都绿。

---

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `engine/scanner.py` | 加 `reward_bracket` 模块级纯函数 + `filter_for_template` 两道闸 | 修改 |
| `config.py` | 加 `per_share_reward_thresholds` 模板键 | 修改 |
| `tests/test_scanner.py` | `reward_bracket` 单测 + filter 单份奖励/取档单测 | 修改 |
| `tests/test_database.py` | 模板键默认值单测 | 修改 |

---

## Task 1: reward_bracket 纯函数

**Files:** Modify `engine/scanner.py`(模块级,放在 `_parse_end_date` 附近)。Test: `tests/test_scanner.py`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_scanner.py` 顶部 import 区下方(或文件末尾)新增:

```python
class TestRewardBracket:
    def test_upward_bracket_mapping(self):
        from engine.scanner import reward_bracket
        assert reward_bracket(20) == 20
        assert reward_bracket(21) == 50
        assert reward_bracket(50) == 50
        assert reward_bracket(100) == 100
        assert reward_bracket(101) == 200
        assert reward_bracket(200) == 200
        assert reward_bracket(250) == 250

    def test_over_250_or_nonpositive_is_none(self):
        from engine.scanner import reward_bracket
        assert reward_bracket(251) is None
        assert reward_bracket(0) is None
        assert reward_bracket(-5) is None
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_scanner.py::TestRewardBracket -v`
Expected: FAIL（`cannot import name 'reward_bracket'`）

- [ ] **Step 3: 实现**

在 `engine/scanner.py` 的 `_parse_end_date` 函数之后(模块级,`MarketScanner` 类之前)新增:

```python
def reward_bracket(min_size):
    """向上取档(更保守):返回 20/50/100/200/250;超 250 或 <=0 返回 None。

    v4 §2:仅服务市场筛选,不参与挂单/离场。
    """
    if min_size <= 0:
        return None
    for b in (20, 50, 100, 200, 250):
        if min_size <= b:
            return b
    return None
```

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_scanner.py::TestRewardBracket -v`
Expected: PASS

- [ ] **Step 5: Commit（不 stage .claude/settings.local.json）**

```bash
git add engine/scanner.py tests/test_scanner.py
git commit -m "feat(scanner): reward_bracket 向上取档纯函数(20/50/100/200/250)"
```

---

## Task 2: config 模板键 per_share_reward_thresholds

**Files:** Modify `config.py`(`TEMPLATE_DEFAULTS`)。Test: `tests/test_database.py`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_database.py` 加:

```python
def test_template_defaults_has_per_share_thresholds():
    from config import TEMPLATE_DEFAULTS
    t = TEMPLATE_DEFAULTS["per_share_reward_thresholds"]
    assert t == {"20": 0.30, "50": 0.30, "100": 0.30, "200": 0.30, "250": 0.30}
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_database.py::test_template_defaults_has_per_share_thresholds -v`
Expected: FAIL（KeyError）

- [ ] **Step 3: 实现**

在 `config.py` 的 `TEMPLATE_DEFAULTS` 里加一项(放在已有策略键之后、闭合 `}` 之前):

```python
    "per_share_reward_thresholds": {
        "20": 0.30, "50": 0.30, "100": 0.30, "200": 0.30, "250": 0.30
    },
```

不改其它键 / `ENGINE_DEFAULTS` / `DEFAULTS`。

- [ ] **Step 4: 运行确认 PASS + 无回归**

Run: `python -m pytest tests/test_database.py -q`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_database.py
git commit -m "feat(config): 模板键 per_share_reward_thresholds(5 档,默认 0.30)"
```

---

## Task 3: filter_for_template 两道闸（最低份额 + 单份奖励）

**Files:** Modify `engine/scanner.py`(`filter_for_template`)。Modify `tests/test_scanner.py`(`TestFilterForTemplate`)。

- [ ] **Step 1: 给 `_candidate` 加 min_size 形参 + 写新测试**

(a) 在 `tests/test_scanner.py` 的 `TestFilterForTemplate._candidate` 签名加 `min_size=100` 形参,并把字典里 `"rewards_min_size": 100,` 改成用形参。即把:
```python
    def _candidate(self, cid, tags, daily_reward=50, bid=0.30, ask=0.31):
```
改为:
```python
    def _candidate(self, cid, tags, daily_reward=50, bid=0.30, ask=0.31, min_size=100):
```
并把同方法里的 `"rewards_min_size": 100,` 改为 `"rewards_min_size": min_size,`。（默认 100,既有调用不受影响。）

(b) 在 `TestFilterForTemplate` 类内追加四个测试:

```python
    def test_per_share_below_threshold_excluded(self):
        scanner = self._scanner()
        # market_reward 20 / min_size 100 = 0.20 < 0.30 默认 -> 剔除
        pool = [self._candidate("A", [], daily_reward=20)]
        assert scanner.filter_for_template(pool, self._template(), "0xW") == []

    def test_per_share_at_or_above_threshold_passes(self):
        scanner = self._scanner()
        # 50 / 100 = 0.50 >= 0.30 -> 通过
        pool = [self._candidate("B", [], daily_reward=50)]
        out = scanner.filter_for_template(pool, self._template(), "0xW")
        assert any(e["market_id"] == "B" for e in out)

    def test_min_size_over_250_excluded(self):
        scanner = self._scanner()
        # 单份奖励够高(200/300≈0.67)且总额够,但 min_size 300 > 250 -> 剔除
        pool = [self._candidate("C", [], daily_reward=200, min_size=300)]
        assert scanner.filter_for_template(pool, self._template(), "0xW") == []

    def test_per_bracket_thresholds_independent(self):
        scanner = self._scanner()
        # P: min_size 100(档100) 单份 0.40;Q: min_size 250(档250) 单份 0.40
        # 档100 阈值调 0.50(剔 P),档250 留 0.30(过 Q)
        c_100 = self._candidate("P", [], daily_reward=40, min_size=100)
        c_250 = self._candidate("Q", [], daily_reward=100, min_size=250)
        tmpl = self._template(
            per_share_reward_thresholds={"100": 0.50, "250": 0.30}
        )
        out = scanner.filter_for_template([c_100, c_250], tmpl, "0xW")
        ids = {e["market_id"] for e in out}
        assert "P" not in ids and "Q" in ids
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_scanner.py::TestFilterForTemplate::test_per_share_below_threshold_excluded -v`
Expected: FAIL（当前无单份奖励闸,低单份市场仍通过）

- [ ] **Step 3: 实现两道闸**

在 `engine/scanner.py` `filter_for_template` 的按市场段。找到:
```python
            max_spread_reward = float(market.get("rewards_max_spread", 2))
            min_size = int(market.get("rewards_min_size", 0))
            neg_risk = market.get("neg_risk", False)
```
改为(在 `min_size = ...` 之后、`neg_risk = ...` 之前插入两道闸):
```python
            max_spread_reward = float(market.get("rewards_max_spread", 2))
            min_size = int(market.get("rewards_min_size", 0))
            # v4 §3:最低份额 0 < ≤ 250;超档无取档 -> 不做该市场
            if not (0 < min_size <= 250):
                continue
            # v4 §3:单份奖励(每日LP奖励÷最低份数) >= 该取档阈值(向上取档) -> 通过
            bracket = reward_bracket(min_size)
            per_share = market_reward / min_size
            thresholds = template.get("per_share_reward_thresholds", {})
            if per_share < float(thresholds.get(str(bracket), 0.30)):
                continue
            neg_risk = market.get("neg_risk", False)
```

（`reward_bracket` 是同模块 Task 1 的模块级函数,直接调用;`market_reward` 已在该方法上文取得。）

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_scanner.py::TestFilterForTemplate -v`
Expected: PASS（新增 4 个 + 既有 filter 用例。既有 `_candidate` 默认 50/100=0.50 ≥ 0.30,不被新闸剔除。）

- [ ] **Step 5: 全套测试无回归**

Run: `python -m pytest -q`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add engine/scanner.py tests/test_scanner.py
git commit -m "feat(scanner): filter_for_template 加最低份额闸 + 单份奖励取档阈值闸"
```

---

## 验收 checkpoint（对应 spec §五）

1. 单份奖励 ≥ 取档阈值通过、低于剔除：`test_per_share_at_or_above_threshold_passes` / `test_per_share_below_threshold_excluded`。
2. 最低份数 > 250 剔除：`test_min_size_over_250_excluded`（≤0 由 `reward_bracket` 单测覆盖 + 闸 `0<min_size`）。
3. 五档阈值分别可调：`test_per_bracket_thresholds_independent`。
4. `min_reward_usd` 总额闸仍生效（既有 `test_reward_floor_filters` 不变）。
5. `pytest -q` 全绿。

## 范围之外

SP5 三档节奏 + 观察名单 + 成交后单侧暂停 + 撤改收敛 · SP6 模板 UI（5 档阈值编辑、退役死字段收口）。
