# 发现阶段只查用得上的品类 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 发现阶段（`discover_candidates`）只查各启用模板实际用得上的品类 tag，而非固定全 14 个，砍掉无关网络、加快「启动 → 市场发现」。

**Architecture:** 在 `discover_candidates` 里按 `include_other` 算 `slugs_needed`（有模板收「其他」→ 全 14；否则 → included 并集），tag 查询与 `tag_pool` 只用它；只有查了全 14 时才刷新配置页计数快照 `last_catalog`。下单/eligible 结果逐条不变。

**Tech Stack:** Python 3、pytest、`unittest.mock.MagicMock`。

## Global Constraints

- **不改交易行为**：`filter_for_template`、`engine/categories.py` 纯函数、`manager.py` 一律不动；改动只缩小发现阶段的 tag 查询集合。
- **正确性铁律**（`engine/categories.py`）：`market_wanted` 对**空 tags** 且 `include_other=True` 判为「其他」放行。故任一启用模板 `include_other=True` 时**必须查全 14**，否则未查品类塌缩成空 tags 被误纳。
- curated 品类共 14 个，`CATALOG_SLUGS = frozenset(...)`（`config.py`）：politics, geopolitics, world, elections, economy, finance, crypto, sports, esports, games, weather, ai, tech, culture。
- 测试基线：改动前 `pytest` = **552 passed**。本计划净增 4 个用例 → 完成后应 **556 passed**。
- 单文件改动：仅 `engine/scanner.py` 的 `discover_candidates`；测试加到 `tests/test_scanner.py`。

---

### Task 1: `discover_candidates` 只查 `slugs_needed`

**Files:**
- Modify: `engine/scanner.py`（`discover_candidates`，第 118-144 行一带）
- Test: `tests/test_scanner.py`（新增测试类 `TestDiscoverNeededSlugsOnly`）

**Interfaces:**
- Consumes（已存在，签名不变）：
  - `included_union(templates) -> set`、`any_include_other(templates) -> bool`（`engine/categories.py`）
  - `tag_pool(full_markets, category_ids, catalog_slugs) -> list[dict]`
  - `_parallel_map(func, items)`（`items` 可为空 → 返回 `[]`）
  - `MarketScanner._catalog_payload(full, category_ids) -> dict`
  - `CATALOG_SLUGS`（`config.py`，已在 `engine/scanner.py` 顶部导入）
- Produces：`discover_candidates` 行为——tag 查询次数 = `len(slugs_needed)`；`slugs_needed != 全 14` 时不设 `self.last_catalog`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_scanner.py` 末尾追加：

```python
class TestDiscoverNeededSlugsOnly:
    def _api_db(self, fake_rewards):
        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        return api, db

    def test_only_needed_slugs_queried_without_include_other(self):
        # 不收「其他」时:只查各模板 included 并集,不再固定查全 14。
        queried = []

        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [
                    {"condition_id": "W", "tokens": [], "rewards_config": []},
                    {"condition_id": "S", "tokens": [], "rewards_config": []},
                ]
            queried.append(tag_slug)
            return {"weather": [{"condition_id": "W"}]}.get(tag_slug, [])

        api, db = self._api_db(fake_rewards)
        scanner = MarketScanner(api, db, "")
        templates = [
            {"included_categories": ["weather"], "include_other": False, "min_reward_usd": 0}
        ]
        pool = scanner.discover_candidates(templates)
        assert set(queried) == {"weather"}  # 只查 weather,其余 13 个没查
        assert {m["condition_id"] for m in pool} == {"W"}  # S 无标签、无人收其他 -> 丢

    def test_multi_template_union_queried(self):
        # 多模板:查各自 included 的并集。
        queried = []

        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [
                    {"condition_id": "W", "tokens": [], "rewards_config": []},
                    {"condition_id": "G", "tokens": [], "rewards_config": []},
                ]
            queried.append(tag_slug)
            return {
                "weather": [{"condition_id": "W"}],
                "games": [{"condition_id": "G"}],
            }.get(tag_slug, [])

        api, db = self._api_db(fake_rewards)
        scanner = MarketScanner(api, db, "")
        templates = [
            {"included_categories": ["weather"], "include_other": False, "min_reward_usd": 0},
            {"included_categories": ["games"], "include_other": False, "min_reward_usd": 0},
        ]
        pool = scanner.discover_candidates(templates)
        assert set(queried) == {"weather", "games"}
        assert {m["condition_id"] for m in pool} == {"W", "G"}

    def test_include_other_queries_all_slugs(self):
        # 收「其他」时:判其他绕不开,必须查全 14(回归护栏)。
        from config import CATALOG_SLUGS

        queried = set()

        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [{"condition_id": "A", "tokens": [], "rewards_config": []}]
            queried.add(tag_slug)
            return []

        api, db = self._api_db(fake_rewards)
        scanner = MarketScanner(api, db, "")
        templates = [
            {"included_categories": ["weather"], "include_other": True, "min_reward_usd": 0}
        ]
        scanner.discover_candidates(templates)
        assert queried == set(CATALOG_SLUGS)

    def test_subset_does_not_set_last_catalog(self):
        # 只查子集算不出全 14 计数 -> 不覆盖 last_catalog(保留旧缓存)。
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [{"condition_id": "W", "tokens": [], "rewards_config": []}]
            return {"weather": [{"condition_id": "W"}]}.get(tag_slug, [])

        api, db = self._api_db(fake_rewards)
        scanner = MarketScanner(api, db, "")
        templates = [
            {"included_categories": ["weather"], "include_other": False, "min_reward_usd": 0}
        ]
        scanner.discover_candidates(templates)
        assert getattr(scanner, "last_catalog", None) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scanner.py::TestDiscoverNeededSlugsOnly -v`
Expected: `test_only_needed_slugs_queried_without_include_other`、`test_multi_template_union_queried`、`test_subset_does_not_set_last_catalog` **FAIL**（当前 `discover_candidates` 固定查全 14 → `queried` 含 14 个、`last_catalog` 被无条件设置）；`test_include_other_queries_all_slugs` PASS（护栏，改前改后都该绿）。

- [ ] **Step 3: 改 `discover_candidates`**

在 `engine/scanner.py` 的 `discover_candidates` 中做四处改动。

(3a) 在 `inc_other = any_include_other(templates)` 那行**之后**插入 `slugs_needed`：

```python
        union = included_union(templates)
        inc_other = any_include_other(templates)
        # 只查用得上的品类:收「其他」时判其他绕不开、必须查全 14;否则只需 included 并集
        # (没被任何模板勾、又没人收其他的市场,market_wanted 本就丢弃,精确 tags 无所谓)。
        slugs_needed = set(CATALOG_SLUGS) if inc_other else (union & set(CATALOG_SLUGS))
```

(3b) 把 tag 查询那行（原 `category_ids = dict(_parallel_map(_tag_slug, CATALOG_SLUGS))`）连同其上方注释改为：

```python
        # 只查 slugs_needed 各一次奖励端点:并发拉。原固定全 14 是历史包袱——只勾少数品类
        # 且不收「其他」时,其余品类查了也会被 market_wanted 丢掉(2026-07-05 提速)。
        category_ids = dict(_parallel_map(_tag_slug, slugs_needed))
```

(3c) 把 `self.last_catalog = self._catalog_payload(full, category_ids)` 连同其上方注释改为**有条件**：

```python
        # 顺带算一份品类计数快照(配置页勾选用),直接复用刚查到的 tag 数据。仅当查了全 14
        # 才算得出正确的全量计数/「其他」数;只查子集时不覆盖(保留上一份缓存,配置页走
        # 缓存/手动刷新)。
        if slugs_needed == set(CATALOG_SLUGS):
            self.last_catalog = self._catalog_payload(full, category_ids)
```

(3d) 把 `pool = tag_pool(full, category_ids, CATALOG_SLUGS)` 改为用 `slugs_needed`：

```python
        pool = tag_pool(full, category_ids, slugs_needed)
```

- [ ] **Step 4: 跑新测试确认通过**

Run: `python -m pytest tests/test_scanner.py::TestDiscoverNeededSlugsOnly -v`
Expected: 4 个全 PASS。

- [ ] **Step 5: 跑全套确认无回归**

Run: `python -m pytest -q`
Expected: `556 passed`（552 基线 + 4 新）。特别确认既有 `TestFetchCandidatesCategoryWiring`、`TestCategoryCounts::test_discover_sets_last_catalog`、`test_discover_reports_progress_incrementally` 仍绿——它们多用 `include_other=True`（查全 14，行为不变）或 `include_other=False` 带具体品类（池结果不变）。

- [ ] **Step 6: 提交**

```bash
git add engine/scanner.py tests/test_scanner.py
git commit -m "perf(scanner): 发现阶段只查用得上的品类(不再固定全14)"
```

---

## Self-Review

**1. Spec coverage：**
- 「只查 `slugs_needed`」→ Step 3a/3b + T1/T2 ✓
- 「include_other → 全 14」→ Step 3a + T3 ✓
- 「子集不覆盖 `last_catalog`、全 14 才刷」→ Step 3c + T4 + 既有 `test_discover_sets_last_catalog` ✓
- 「`tag_pool` 用 `slugs_needed`」→ Step 3d ✓
- 「下单行为零变化」→ 全套回归 Step 5 ✓
- 「不改 `category_counts`/`filter_for_template`/`manager`」→ 计划只碰 `discover_candidates` ✓

**2. Placeholder scan：** 无 TBD/TODO；测试与实现均为完整代码。✓

**3. Type consistency：** `slugs_needed` 为 `set`；`union & set(CATALOG_SLUGS)` 返回 `set`；`slugs_needed == set(CATALOG_SLUGS)` 值比较；`_parallel_map` 接受可迭代（空 → `{}`）；`tag_pool` 第三参接受任意 slug 可迭代。与既有签名一致。✓
