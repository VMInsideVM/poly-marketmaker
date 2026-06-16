# SP6d 死字段代码清理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删掉 v4 接入过程中退役、production 已零调用的三处死代码（`tiers_k` 模板键 / `held_condition_ids` / `engine/strategy_check.py`）及其测试。

**Architecture:** 纯删除 + 对应测试更新。已 grep 确认 `engine/web/models/api/utils` 对三者零调用，唯一引用是各自定义 + tests。**保留** `engine/laddering.py` 的 `tiers_k`（`build_ladder` 形参 = `len(tier_rules)`，活代码，与模板键同名不同物）。

**Tech Stack:** Python 3.12 / pytest。

**执行顺序:** 单任务（一组删除 + 测试更新）。基线:SP6c 合并后 `418 passed`，本任务后 `408 passed`（−4 删 test_strategy_check.py、−6 删 test_positions.py 的 held_condition_ids 测试）。

---

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `config.py` | 删 `tiers_k` 模板键 | 修改 |
| `engine/positions.py` | 删 `held_condition_ids`（留 `held_side_info`） | 修改 |
| `engine/strategy_check.py` | 死模块 | 删除 |
| `tests/test_strategy_check.py` | 死模块测试 | 删除 |
| `tests/test_positions.py` | 删 6 个 held_condition_ids 测试 + 改 import | 修改 |
| `tests/test_database.py` | 删 `tiers_k` 断言 | 修改 |
| `tests/test_place_orders.py` | 删 stub 里 `tiers_k` | 修改 |
| `tests/test_settings_routes.py` | 从断言键元组删 `tiers_k` | 修改 |

---

## Task 1: 删三处死代码 + 更新测试

**Files:** 见上表。

- [ ] **Edit 1 — config.py 删 tiers_k 模板键**

把:
```python
    # 多档挂单(SP2)
    "tiers_k": 6,
    "tier_rules": [[{"upper": None, "action": {"type": "min_size"}}] for _ in range(6)],
```
改为:
```python
    # 多档挂单(SP2)
    "tier_rules": [[{"upper": None, "action": {"type": "min_size"}}] for _ in range(6)],
```

- [ ] **Edit 2 — engine/positions.py 删 held_condition_ids**

把:
```python
"""Pure helpers derived from Polymarket Data API /positions (no network/IO)."""


def held_condition_ids(positions: list[dict]) -> set[str]:
    """返回当前持有仓位(size>0)的 market(condition_id)集合。

    positions 为 Data API /positions 的返回(每项含 conditionId / size)。YES/NO
    任一方向有仓都算该 market 已持仓;缺 conditionId 或 size<=0 的项忽略。size 做
    None/字符串安全转换。
    """
    out: set[str] = set()
    for p in positions:
        cid = p.get("conditionId", "")
        if cid and float(p.get("size", 0) or 0) > 0:
            out.add(cid)
    return out


def held_side_info(positions: list[dict]):
```
改为:
```python
"""Pure helpers derived from Polymarket Data API /positions (no network/IO)."""


def held_side_info(positions: list[dict]):
```

- [ ] **Edit 3 — tests/test_positions.py 删 import 中的 held_condition_ids + 6 个测试**

把从 import 到第一个 held_side_info 测试之间的整段:
```python
from engine.positions import held_condition_ids, held_side_info


def test_includes_condition_with_positive_size():
    assert held_condition_ids([{"conditionId": "c1", "size": 100.0}]) == {"c1"}


def test_excludes_zero_and_negative_size():
    pos = [{"conditionId": "c1", "size": 0}, {"conditionId": "c2", "size": -5}]
    assert held_condition_ids(pos) == set()


def test_excludes_missing_or_empty_condition_id():
    assert held_condition_ids([{"size": 100.0}]) == set()
    assert held_condition_ids([{"conditionId": "", "size": 100.0}]) == set()


def test_empty_list_empty_set():
    assert held_condition_ids([]) == set()


def test_yes_and_no_of_same_condition_collapse_to_one():
    pos = [
        {"conditionId": "c1", "size": 100.0, "asset": "yes"},
        {"conditionId": "c1", "size": 50.0, "asset": "no"},
    ]
    assert held_condition_ids(pos) == {"c1"}


def test_none_and_string_size_are_handled():
    pos = [
        {"conditionId": "c1", "size": None},  # None -> treated as 0 -> excluded
        {"conditionId": "c2", "size": "30"},  # numeric string -> 30 -> included
    ]
    assert held_condition_ids(pos) == {"c2"}


def test_held_side_info_held_assets_only_positive_size():
```
改为:
```python
from engine.positions import held_side_info


def test_held_side_info_held_assets_only_positive_size():
```
（其余 4 个 `held_side_info` 测试保持不变。）

- [ ] **Edit 4 — tests/test_database.py 删 tiers_k 断言**

把:
```python
    assert TEMPLATE_DEFAULTS["tiers_k"] == 6
    assert len(TEMPLATE_DEFAULTS["tier_rules"]) == 6
```
改为:
```python
    assert len(TEMPLATE_DEFAULTS["tier_rules"]) == 6
```

- [ ] **Edit 5 — tests/test_place_orders.py 删 stub 里 tiers_k**

把:
```python
    tmpl = {
        "tiers_k": 6,
        "tier_rules": [
```
改为:
```python
    tmpl = {
        "tier_rules": [
```

- [ ] **Edit 6 — tests/test_settings_routes.py 从断言键元组删 tiers_k**

把:
```python
        "tiers_k", "discovery_interval_sec",
```
改为:
```python
        "discovery_interval_sec",
```

- [ ] **Edit 7 — 删两个死文件**

```bash
git rm engine/strategy_check.py tests/test_strategy_check.py
```

- [ ] **Step 8 — 全套测试 + 计数核对**

Run: `python -m pytest -q`
Expected: `408 passed`（418 − 4 删 test_strategy_check.py − 6 删 held_condition_ids 测试）。若非 408，逐项核对差异（防误删活测试 / 漏改断言）。

- [ ] **Step 9 — grep 确认死符号在 production 已无残留**

Run:
```bash
grep -rnE "held_condition_ids|needs_replace|strategy_check" engine web models api utils 2>/dev/null || echo "production clean"
grep -rnE "\"tiers_k\"|'tiers_k'" config.py engine web models api 2>/dev/null || echo "tiers_k key gone"
```
Expected: 第一个 `production clean`（无匹配）；第二个 `tiers_k key gone`。`engine/laddering.py` 的 `tiers_k`（形参）不在上面 grep 的字符串字面量模式里，仍保留——可单独确认它还在:`grep -n "tiers_k" engine/laddering.py` 应有 6 处。

- [ ] **Step 10 — Commit（不 stage `.claude/settings.local.json`）**

```bash
git add config.py engine/positions.py tests/test_positions.py tests/test_database.py tests/test_place_orders.py tests/test_settings_routes.py
git commit -m "chore: 清理死字段 tiers_k 模板键 / held_condition_ids / strategy_check(均无 production 引用)"
```
（`git rm` 的两个删除已在暂存区,会一并提交。）

---

## 验收 checkpoint（对应 spec §三）

1. 三处死代码删除、production 零残留:Step 9 grep。
2. `laddering.py` 的 `tiers_k` 形参保留(多档挂单行为不变):Step 9 末尾确认 + 全套绿。
3. 测试计数 418 → 408,差额全由删除的死代码测试解释:Step 8。
4. `pytest` 全绿:Step 8。

## 范围之外

至此 v4 接入(SP1-SP6)全部完成。陈旧 worktree `.claude/worktrees/agent-abc14a0dc9e132681` 清理与本计划无关(可选)。
