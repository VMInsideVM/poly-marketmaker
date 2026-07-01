# 品类白名单勾选 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把品类筛选从"写死 3 项的黑名单"改成"配置页动态勾选的白名单"——只做勾中的 curated 品类，另有「其他/未分类」兜底开关。

**Architecture:** 沿用现有 `partition_candidates` 的按 `tag_slug` 打标签机制（CLOB 奖励端点，已实测可靠），把"排除并集"翻成"包含并集"。发现阶段对**整份 curated 名单**打标签（这样"无 curated 标签"才能准确判为「其他」），按各模板 `included_categories` 的并集 + `include_other` 预筛；每模板精筛做白名单判定。配置页新增 `/api/categories` 返回各 curated 品类的实时市场数，动态渲染勾选框。

**Tech Stack:** Python 3 / Flask / SQLite（key-value 模板）/ pytest；纯前端 JS（无框架）。

## Global Constraints

- UI 文案一律简体中文。
- 模板参数走 `TEMPLATE_DEFAULTS` 的 key/value merge：新键加进 `TEMPLATE_DEFAULTS` 即自动持久化（`web/routes.py` 按 `k in TEMPLATE_DEFAULTS` 过滤保存），**不改保存路由**。
- 白名单判定唯一口径（两处复用）：`market_wanted(tags, included, include_other) = bool(set(included) & set(tags)) or (include_other and not tags)`。`tags` 恒为"该市场命中的 curated slug 列表"，且**对整份 catalog 打标签**（否则空 tags 会把"未勾选品类"误判成「其他」）。
- 默认值延续升级前行为：`included_categories` = curated 名单去掉 `sports/esports/weather`；`include_other = True`。
- 版本号：过滤语义翻转 + 配置格式变更 → MAJOR，`version.py` 从 `2.4.0` → `3.0.0`。
- 中文文件（`config.html`、本计划、spec）由主 agent 直接 Write，写后核对无 BOM。
- 每个 Task 独立可测、独立提交；提交只 stage 本 Task 触及的文件（勿卷入仓库里其他未提交 WIP）。

---

### Task 1: config.py — curated 名单常量 + 模板字段翻转

**Files:**
- Modify: `config.py`（`TEMPLATE_DEFAULTS` 第 16-53 行；新增常量）
- Test: `tests/test_database.py`（第 40、492 行断言更新）

**Interfaces:**
- Produces:
  - `CATEGORY_CATALOG: list[dict]` — 有序，每项 `{"slug": str, "label": str}`。
  - `CATALOG_SLUGS: frozenset[str]`
  - `DEFAULT_INCLUDED_CATEGORIES: list[str]`
  - `TEMPLATE_DEFAULTS["included_categories"]`（list[str]）、`TEMPLATE_DEFAULTS["include_other"]`（bool）；**移除** `TEMPLATE_DEFAULTS["excluded_categories"]`。

- [ ] **Step 1: 更新默认值断言测试（先失败）**

`tests/test_database.py` 第 40 行整行替换：
```python
    assert "excluded_categories" not in TEMPLATE_DEFAULTS
    assert TEMPLATE_DEFAULTS["include_other"] is True
    assert "sports" not in TEMPLATE_DEFAULTS["included_categories"]
    assert "politics" in TEMPLATE_DEFAULTS["included_categories"]
```
`tests/test_database.py` 第 492 行整行替换：
```python
        assert t["included_categories"] == TEMPLATE_DEFAULTS["included_categories"]
```
（该测试文件顶部已 `from config import ... TEMPLATE_DEFAULTS`；如仅导入 `DEFAULTS` 则补导 `TEMPLATE_DEFAULTS`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_database.py -k "defaults or merges_defaults" -q`
Expected: FAIL（`KeyError: 'included_categories'` 或断言失败）

- [ ] **Step 3: 在 config.py 加常量并翻转字段**

在 `config.py` `TEMPLATE_DEFAULTS` **之前**插入：
```python
# curated 品类名单(白名单勾选来源)。slug 须是 CLOB /rewards/markets/multi 的 tag_slug
# 可识别值(已实测 politics/geopolitics/world/elections/economy/finance/crypto/sports/
# esports/games/weather/ai/tech/culture 均返回合理子集)。顺序即配置页展示顺序。
CATEGORY_CATALOG = [
    {"slug": "politics", "label": "政治"},
    {"slug": "geopolitics", "label": "地缘政治"},
    {"slug": "world", "label": "国际"},
    {"slug": "elections", "label": "选举"},
    {"slug": "economy", "label": "经济"},
    {"slug": "finance", "label": "金融"},
    {"slug": "crypto", "label": "加密货币"},
    {"slug": "sports", "label": "体育"},
    {"slug": "esports", "label": "电竞"},
    {"slug": "games", "label": "游戏"},
    {"slug": "weather", "label": "天气"},
    {"slug": "ai", "label": "人工智能"},
    {"slug": "tech", "label": "科技"},
    {"slug": "culture", "label": "文化"},
]
CATALOG_SLUGS = frozenset(c["slug"] for c in CATEGORY_CATALOG)
# 默认延续升级前行为:体育/电竞/天气不做,其余(含未分类)都做。
_DEFAULT_EXCLUDED = {"sports", "esports", "weather"}
DEFAULT_INCLUDED_CATEGORIES = [
    c["slug"] for c in CATEGORY_CATALOG if c["slug"] not in _DEFAULT_EXCLUDED
]
```
`TEMPLATE_DEFAULTS` 里删掉第 28 行 `"excluded_categories": ["sports", "esports", "weather"],`，改为：
```python
    "included_categories": DEFAULT_INCLUDED_CATEGORIES,
    "include_other": True,
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_database.py -k "defaults or merges_defaults" -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add config.py tests/test_database.py
git commit -m "feat(config): curated 品类名单 + 模板字段 excluded_categories→included_categories/include_other"
```

---

### Task 2: engine/categories.py — 白名单纯函数

**Files:**
- Rewrite: `engine/categories.py`
- Rewrite: `tests/test_categories.py`

**Interfaces:**
- Consumes: 无（纯函数）。
- Produces:
  - `included_union(templates: list[dict]) -> set` — ∪ 各模板 `included_categories`。
  - `any_include_other(templates: list[dict]) -> bool` — 是否有模板 `include_other` 为真。
  - `tag_pool(full_markets: list[dict], category_ids: dict, catalog_slugs) -> list[dict]` — 给每条市场加 `tags`（命中的 catalog slug 有序列表），不删除。
  - `market_wanted(tags, included, include_other: bool) -> bool` — 白名单判定。
  - `count_by_category(full_ids, category_ids: dict, catalog_slugs) -> tuple[dict, int]` — `({slug: count}, other_count)`。

- [ ] **Step 1: 重写测试（先失败）**

`tests/test_categories.py` 整文件替换为：
```python
"""tests/test_categories.py — 品类白名单纯函数(不触网)。"""

from engine.categories import (
    included_union,
    any_include_other,
    tag_pool,
    market_wanted,
    count_by_category,
)


def test_included_union_is_union():
    templates = [
        {"included_categories": ["politics", "economy"]},
        {"included_categories": ["economy", "ai"]},
    ]
    assert included_union(templates) == {"politics", "economy", "ai"}


def test_any_include_other():
    assert any_include_other([{"include_other": False}, {"include_other": True}]) is True
    assert any_include_other([{"include_other": False}]) is False
    assert any_include_other([]) is False


def test_tag_pool_attaches_curated_tags():
    full = [{"condition_id": "A"}, {"condition_id": "B"}, {"condition_id": "C"}]
    category_ids = {"politics": {"A"}, "economy": {"A", "B"}, "ai": set()}
    pool = tag_pool(full, category_ids, ["politics", "economy", "ai"])
    by = {m["condition_id"]: m for m in pool}
    assert set(by["A"]["tags"]) == {"politics", "economy"}
    assert by["B"]["tags"] == ["economy"]
    assert by["C"]["tags"] == []  # 无 curated 标签 -> 其他


def test_market_wanted_whitelist_hit():
    assert market_wanted(["politics"], {"politics", "ai"}, False) is True


def test_market_wanted_whitelist_miss():
    assert market_wanted(["sports"], {"politics"}, False) is False


def test_market_wanted_other_bucket():
    # 空 tags + include_other -> 收
    assert market_wanted([], {"politics"}, True) is True
    # 空 tags + 不收其他 -> 不收
    assert market_wanted([], {"politics"}, False) is False


def test_market_wanted_categorized_but_unselected_is_not_other():
    # 有 curated 标签但没被勾选:即便 include_other 也不收(它不是"其他")
    assert market_wanted(["sports"], {"politics"}, True) is False


def test_count_by_category():
    full_ids = {"A", "B", "C", "D"}
    category_ids = {"politics": {"A"}, "economy": {"A", "B"}, "ai": set()}
    counts, other = count_by_category(full_ids, category_ids, ["politics", "economy", "ai"])
    assert counts == {"politics": 1, "economy": 2, "ai": 0}
    assert other == 2  # C、D 未命中任何 curated
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_categories.py -q`
Expected: FAIL（`ImportError: cannot import name 'included_union'`）

- [ ] **Step 3: 重写 engine/categories.py**

整文件替换为：
```python
"""engine/categories.py — 品类白名单纯函数(不触网)。

采集器对整份 curated 名单给市场打标签(tag_pool),再按白名单判定(market_wanted):
- included_union: 所有模板 included_categories 的并集(发现阶段预筛用的"想要"集)。
- any_include_other: 是否有模板收「其他/未分类」(决定预筛是否保留无 curated 标签者)。
- market_wanted: 命中 included,或(空 tags 且 include_other)。空 tags 恒指"无 curated
  标签"——故打标签必须覆盖整份 catalog,否则未勾选品类会被误判成「其他」。
"""


def included_union(templates: list[dict]) -> set:
    out = set()
    for t in templates:
        out.update(t.get("included_categories", []) or [])
    return out


def any_include_other(templates: list[dict]) -> bool:
    return any(bool(t.get("include_other", False)) for t in templates)


def tag_pool(full_markets: list[dict], category_ids: dict, catalog_slugs) -> list[dict]:
    """给每条市场加 tags = 命中的 catalog slug(有序);不删除任何市场。

    Args:
        full_markets: 全量奖励市场(每条含 condition_id)。
        category_ids: {catalog slug: set(condition_id)},逐 slug 查询所得。
        catalog_slugs: 打标签的 curated slug 全集(决定 tags 与「其他」判定)。
    """
    ordered = list(catalog_slugs)
    pool = []
    for m in full_markets:
        cid = m.get("condition_id", "")
        tags = [s for s in ordered if cid in category_ids.get(s, set())]
        entry = dict(m)
        entry["tags"] = tags
        pool.append(entry)
    return pool


def market_wanted(tags, included, include_other: bool) -> bool:
    tags = tags or []
    if set(included) & set(tags):
        return True
    return bool(include_other) and not tags


def count_by_category(full_ids, category_ids: dict, catalog_slugs):
    """返回 ({slug: 命中数}, 其他数)。计数在整全量集上做,与是否被勾选无关。"""
    counts = {s: len(category_ids.get(s, set())) for s in catalog_slugs}
    covered = set()
    for s in catalog_slugs:
        covered |= category_ids.get(s, set())
    other = len(set(full_ids) - covered)
    return counts, other
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_categories.py -q`
Expected: PASS（8 passed）

- [ ] **Step 5: 提交**

```bash
git add engine/categories.py tests/test_categories.py
git commit -m "feat(categories): 白名单纯函数(included_union/tag_pool/market_wanted/count_by_category)"
```

---

### Task 3: engine/scanner.py — 发现阶段全名单打标签 + 白名单预筛/精筛

**Files:**
- Modify: `engine/scanner.py`（顶部 import 第 22-26 行；`discover_candidates` 第 70-138 行；`filter_for_template` 第 188/191-193 行）
- Test: `tests/test_scanner.py`（改写 `TestFetchCandidatesCategoryWiring`、`TestFilterForTemplate` 相关用例、`_template` fixture）
- Test（连带 fixture 更新）: `tests/test_eligible_fields.py`（第 45 行）

**Interfaces:**
- Consumes: `engine.categories.{included_union, any_include_other, tag_pool, market_wanted}`；`config.CATALOG_SLUGS`。
- Produces: `MarketScanner.discover_candidates` / `filter_for_template` 行为改为白名单；候选池每市场带 `tags`（curated 命中列表）。

- [ ] **Step 1: 改写 scanner 分类连线测试（先失败）**

`tests/test_scanner.py` 把 `TestFetchCandidatesCategoryWiring.test_queries_full_plus_each_category_and_subtracts` 整方法替换为：
```python
    def test_whitelist_keeps_only_included_union(self):
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [
                    {"condition_id": "A", "tokens": [], "rewards_config": []},
                    {"condition_id": "B", "tokens": [], "rewards_config": []},
                    {"condition_id": "C", "tokens": [], "rewards_config": []},
                ]
            return {"politics": [{"condition_id": "A"}], "economy": [{"condition_id": "B"}]}.get(
                tag_slug, []
            )

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        templates = [
            {"included_categories": ["politics"], "include_other": False, "min_reward_usd": 0},
            {"included_categories": ["economy"], "include_other": False, "min_reward_usd": 0},
        ]
        pool = scanner.fetch_candidates(templates, skip_orderbook=True)
        # A(politics)+B(economy) 收;C 无 curated 标签且无人收其他 -> 丢
        assert {m["condition_id"] for m in pool} == {"A", "B"}

    def test_include_other_keeps_untagged(self):
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [
                    {"condition_id": "A", "tokens": [], "rewards_config": []},
                    {"condition_id": "C", "tokens": [], "rewards_config": []},
                ]
            return {"politics": [{"condition_id": "A"}]}.get(tag_slug, [])

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        templates = [
            {"included_categories": ["politics"], "include_other": True, "min_reward_usd": 0}
        ]
        pool = scanner.fetch_candidates(templates, skip_orderbook=True)
        assert {m["condition_id"] for m in pool} == {"A", "C"}  # C 落入其他
```
把该类里 `test_no_price_computed` 及 `TestDiscoverAndRefreshSplit` 中所有 `{"excluded_categories": [], "min_reward_usd": 0}` 模板字面量，替换为 `{"included_categories": [], "include_other": True, "min_reward_usd": 0}`（这些用例只验证"有候选/无价格/订单簿"，用 `include_other=True` 放行全部）。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_scanner.py -k "Category or DiscoverAndRefresh" -q`
Expected: FAIL（`included_union` 未被调用 / 断言不符）

- [ ] **Step 3: 改 scanner 实现**

`engine/scanner.py` 顶部 import（第 22-26 行）替换为：
```python
from engine.categories import (
    included_union,
    any_include_other,
    tag_pool,
    market_wanted,
)
from config import CATALOG_SLUGS
from engine.strategy import reward_price_range
```
`discover_candidates` 第 75-99 行（从 `inter = ...` 到 `for market in pool:` 循环体开头的 blacklist 判断）替换为：
```python
        union = included_union(templates)
        inc_other = any_include_other(templates)
        floors = [t.get("min_reward_usd", 0) for t in templates]
        min_floor = min(floors) if floors else 0

        full = self.api.get_rewards_markets()
        category_ids = {}
        for slug in CATALOG_SLUGS:  # 对整份 catalog 打标签(否则"其他"判定不准)
            rows = self.api.get_rewards_markets(tag_slug=slug)
            category_ids[slug] = {m.get("condition_id", "") for m in rows}

        pool = tag_pool(full, category_ids, CATALOG_SLUGS)
        blacklist = self.db.get_blacklist_ids()

        out = []
        checked = 0
        for market in pool:
            cid = market.get("condition_id", "")
            if cid in blacklist:
                continue
            if not market_wanted(market.get("tags", []), union, inc_other):
                continue  # 没被任何模板 include(且非其他)-> 不做昂贵的精确奖励拉取
```
把 `discover_candidates` 末尾日志（第 133-137 行）里的 `len(queried)` 改为 `len(union)`，文案改 `"queried %d categories"` → `"included union %d categories"`。

`filter_for_template` 第 188 行替换：
```python
        included = set(template.get("included_categories", []) or [])
        include_other = bool(template.get("include_other", False))
```
第 191-193 行（`for market in candidate_pool:` 后的 `if excluded & set(market.get("tags", [])): continue`）替换为：
```python
        for market in candidate_pool:
            if not market_wanted(market.get("tags", []), included, include_other):
                continue
```

- [ ] **Step 4: 改 filter 白名单测试 + eligible_fields fixture**

`tests/test_scanner.py` `TestFilterForTemplate`：
- `_template`（第 180-190 行）里 `"excluded_categories": [],` 替换为 `"included_categories": ["esports", "politics"], "include_other": True,`（默认放行 tags 为 esports/politics 及空 tags 的候选，保持其余用例语义）。
- `test_category_narrow_drops_excluded_tag`（第 197-204 行）整方法替换为：
```python
    def test_category_whitelist_keeps_only_included(self):
        scanner = self._scanner()
        pool = [self._candidate("A", ["esports"]), self._candidate("B", ["politics"])]
        out = scanner.filter_for_template(
            pool, self._template(included_categories=["politics"], include_other=False), "0xW"
        )
        ids = {e["market_id"] for e in out}
        assert "A" not in ids and "B" in ids
```
- `test_two_templates_yield_different_lists`（第 218-233 行）整方法替换为：
```python
    def test_two_templates_yield_different_lists(self):
        scanner = self._scanner()
        pool = [self._candidate("A", ["esports"]), self._candidate("B", ["politics"])]
        strict = {
            e["market_id"]
            for e in scanner.filter_for_template(
                pool, self._template(included_categories=["politics"], include_other=False), "0xW"
            )
        }
        loose = {
            e["market_id"]
            for e in scanner.filter_for_template(
                pool,
                self._template(included_categories=["politics", "esports"], include_other=False),
                "0xW",
            )
        }
        assert strict != loose and "A" in loose and "A" not in strict
```
- 该类其余用例（reward_floor / cooldown / per_share / min_size 等）候选 tags 多为 `[]`：靠 `_template` 默认的 `include_other=True` 放行，无需逐个改。但 `test_cooldown_market_skipped` 用的是**独立** MagicMock template（`self._template()` 之外？核对：它用 `self._template()`，OK）。

`tests/test_eligible_fields.py` 第 45 行 `"excluded_categories": [],` 替换为 `"included_categories": [], "include_other": True,`。

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_scanner.py tests/test_eligible_fields.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add engine/scanner.py tests/test_scanner.py tests/test_eligible_fields.py
git commit -m "feat(scanner): 白名单打标签(整名单)+并集预筛+每模板精筛"
```

---

### Task 4: engine/manager.py — 去重键随字段翻转

**Files:**
- Modify: `engine/manager.py`（`_active_templates` 第 616-631 行）
- Test: `tests/test_manager.py`（fixture 第 46-53 行）

**Interfaces:**
- Consumes: 模板的 `included_categories`、`include_other`、`min_reward_usd`。
- Produces: `_active_templates` 按新三元组去重。

- [ ] **Step 1: 更新 manager fixture（先失败/连带）**

`tests/test_manager.py` 第 46-53 行两处 `"excluded_categories": []` 替换：
- 第 47 行 `"excluded_categories": [],` → `"included_categories": ["politics"], "include_other": True,`
- 第 53 行 `db.get_template.return_value = {"excluded_categories": [], "min_reward_usd": 100.0}` → `db.get_template.return_value = {"included_categories": ["politics"], "include_other": True, "min_reward_usd": 100.0}`

- [ ] **Step 2: 跑测试看现状**

Run: `pytest tests/test_manager.py -q`
Expected: 现有用例应仍 PASS（fixture 只是换键；`_active_templates` 旧代码 `.get("excluded_categories", [])` 拿到 `[]` 不报错）。若某用例断言 `_active_templates` 结果则可能 FAIL——记录之。

- [ ] **Step 3: 改 `_active_templates` 去重键**

`engine/manager.py` 第 616 行 docstring 里 `excluded_categories` 改 `included_categories`；第 628-631 行 `key = (...)` 替换为：
```python
            key = (
                tuple(sorted(tmpl.get("included_categories", []) or [])),
                bool(tmpl.get("include_other", False)),
                tmpl.get("min_reward_usd", 0),
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_manager.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add engine/manager.py tests/test_manager.py
git commit -m "feat(manager): _active_templates 去重键改用 included_categories/include_other"
```

---

### Task 5: /api/categories — 实时品类计数端点

**Files:**
- Modify: `engine/scanner.py`（新增 `category_counts` 方法）
- Modify: `engine/manager.py`（`__init__` 加缓存字段；抽 `_ensure_scanner_api`；新增 `category_catalog`）
- Modify: `web/routes.py`（新增 `GET /api/categories`）
- Test: `tests/test_scanner.py`（新增 `category_counts` 用例）、`tests/test_categories_route.py`（新建）

**Interfaces:**
- Consumes: `engine.categories.count_by_category`；`config.CATEGORY_CATALOG`；`api.proxy.use_proxy`。
- Produces:
  - `MarketScanner.category_counts(catalog: list[dict]) -> dict`：`{"categories": [{"slug","label","count"}], "other_count": int}`。
  - `EngineManager.category_catalog() -> dict`：同上再加 `"ready": bool`；带 600s TTL 内存缓存；未就绪返回 `{"ready": False, "categories": [], "other_count": 0}`。
  - `GET /api/categories` → `manager.category_catalog()` 的 JSON。

- [ ] **Step 1: 写 scanner.category_counts 测试（先失败）**

`tests/test_scanner.py` 末尾追加：
```python
class TestCategoryCounts:
    def test_counts_and_other(self):
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [{"condition_id": c} for c in ("A", "B", "C", "D")]
            return {"politics": [{"condition_id": "A"}], "economy": [{"condition_id": "A"}, {"condition_id": "B"}]}.get(
                tag_slug, []
            )

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        scanner = MarketScanner(api, MagicMock(), "")
        catalog = [{"slug": "politics", "label": "政治"}, {"slug": "economy", "label": "经济"}]
        out = scanner.category_counts(catalog)
        counts = {c["slug"]: c["count"] for c in out["categories"]}
        assert counts == {"politics": 1, "economy": 2}
        assert [c["label"] for c in out["categories"]] == ["政治", "经济"]
        assert out["other_count"] == 2  # C、D
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_scanner.py::TestCategoryCounts -q`
Expected: FAIL（`AttributeError: category_counts`）

- [ ] **Step 3: 实现 scanner.category_counts**

`engine/scanner.py` 顶部 import 补 `count_by_category`：
```python
from engine.categories import (
    included_union,
    any_include_other,
    tag_pool,
    market_wanted,
    count_by_category,
)
```
在 `MarketScanner` 类内（`fetch_candidates` 附近）新增：
```python
    def category_counts(self, catalog) -> dict:
        """catalog: [{'slug','label'}]. 返回各 curated 品类在当前奖励市场的市场数 +
        「其他」数。钱包无关;逐 slug 查 CLOB 奖励端点,与全量取交集计数。"""
        full = self.api.get_rewards_markets()
        full_ids = {m.get("condition_id", "") for m in full if m.get("condition_id")}
        category_ids = {}
        for c in catalog:
            slug = c["slug"]
            try:
                rows = self.api.get_rewards_markets(tag_slug=slug)
                category_ids[slug] = {m.get("condition_id", "") for m in rows} & full_ids
            except Exception as e:
                logger.warning("category_counts slug %s failed: %s", slug, e)
                category_ids[slug] = set()
        slugs = [c["slug"] for c in catalog]
        counts, other = count_by_category(full_ids, category_ids, slugs)
        cats = [
            {"slug": c["slug"], "label": c["label"], "count": counts.get(c["slug"], 0)}
            for c in catalog
        ]
        return {"categories": cats, "other_count": other}
```

- [ ] **Step 4: 跑 scanner 计数测试确认通过**

Run: `pytest tests/test_scanner.py::TestCategoryCounts -q`
Expected: PASS

- [ ] **Step 5: manager 抽 `_ensure_scanner_api` + `category_catalog`**

`engine/manager.py`：顶部补 `from config import CATEGORY_CATALOG`（与现有 config 导入合并）。`__init__` 里加缓存初值（找到 `self._scanner_api` 初始化处附近，若无则加）：
```python
        self._catalog_cache = None
        self._catalog_cache_ts = 0.0
```
把 `scan_markets` 第 460-477 行的"确保 `_scanner_api`"块抽成方法（`scan_markets` 改为调用它）：
```python
    def _ensure_scanner_api(self):
        """确保 _scanner_api 就绪:优先复用运行中 worker 的 API,否则用首个启用钱包新建。
        无可用钱包时保持 None。"""
        if self._scanner_api:
            return
        if self.engines:
            self._scanner_api = next(iter(self.engines.values())).api
            return
        wallets = self.db.list_wallets()
        enabled = [w for w in wallets if w["enabled"]]
        if not enabled:
            logger.error("No wallets available for scanning")
            return
        pk = decrypt(enabled[0]["encrypted_key"], self.encryption_key)
        self._scanner_api = PolymarketAPI(
            pk,
            signature_type=enabled[0].get("signature_type", 2),
            funder=enabled[0].get("funder") or None,
            proxy=enabled[0].get("proxy") or None,
        )
        logger.info("Created scanner API from wallet %s", enabled[0]["address"])
```
`scan_markets` 开头第 460-477 行整块替换为：
```python
        self._ensure_scanner_api()
```
（其后 `eligible = self._scan_with_status()` 等不变。）
在 manager 内新增：
```python
    def category_catalog(self) -> dict:
        """供配置页勾选:各 curated 品类实时市场数 + 「其他」数。600s 内存缓存。"""
        now = _time.time()
        if self._catalog_cache and (now - self._catalog_cache_ts) < 600:
            return self._catalog_cache
        self._ensure_scanner_api()
        if not self._scanner_api:
            return {"ready": False, "categories": [], "other_count": 0}
        scanner = MarketScanner(self._scanner_api, self.db, "")
        with use_proxy(getattr(self._scanner_api, "proxy_url", None)):
            result = scanner.category_counts(CATEGORY_CATALOG)
        result["ready"] = True
        self._catalog_cache = result
        self._catalog_cache_ts = now
        return result
```
（`_time` 是 manager 现有的 `import time as _time` 别名——核对文件顶部；若为 `import time` 则用 `time.time()`。）

- [ ] **Step 6: 写路由 + 路由测试**

`web/routes.py` 在 `/api/settings` 附近新增：
```python
@app.route("/api/categories", methods=["GET"])
@login_required
def api_categories():
    if manager is None:
        return jsonify({"ready": False, "categories": [], "other_count": 0})
    try:
        return jsonify(manager.category_catalog())
    except Exception as e:
        return jsonify(
            {"ready": False, "categories": [], "other_count": 0, "error": str(e)}
        )
```
新建 `tests/test_categories_route.py`：
```python
"""tests/test_categories_route.py — GET /api/categories 契约。"""

from unittest.mock import MagicMock
import web.routes as routes
from models.database import Database


def _client(tmp_path, monkeypatch, manager):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", manager)
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client


def test_categories_no_manager(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, None)
    data = client.get("/api/categories").get_json()
    assert data == {"ready": False, "categories": [], "other_count": 0}


def test_categories_from_manager(tmp_path, monkeypatch):
    mgr = MagicMock()
    mgr.category_catalog.return_value = {
        "ready": True,
        "categories": [{"slug": "politics", "label": "政治", "count": 3}],
        "other_count": 1,
    }
    client = _client(tmp_path, monkeypatch, mgr)
    data = client.get("/api/categories").get_json()
    assert data["ready"] is True
    assert data["categories"][0]["slug"] == "politics"
    assert data["other_count"] == 1
```

- [ ] **Step 7: 跑测试确认通过**

Run: `pytest tests/test_scanner.py::TestCategoryCounts tests/test_categories_route.py tests/test_manager.py -q`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add engine/scanner.py engine/manager.py web/routes.py tests/test_scanner.py tests/test_categories_route.py
git commit -m "feat(api): /api/categories 实时品类计数(scanner.category_counts + manager 缓存)"
```

---

### Task 6: config.html — 动态勾选 UI + 保存契约

**Files:**
- Modify: `web/templates/config.html`（第 99-104 行 HTML；`loadStrategy` 第 206-228 行；保存段第 477-479 行；新增 `renderCategories`）
- Test: `tests/test_settings_routes.py`（第 31、46、64 行）

**Interfaces:**
- Consumes: `GET /api/categories`、`GET/PUT /api/templates/<id>` 的 `included_categories`/`include_other`。
- Produces: 表单保存 `data.included_categories`（勾中项）、`data.include_other`（布尔）。

- [ ] **Step 1: 更新保存契约测试（先失败）**

`tests/test_settings_routes.py`：
- 第 31 行 `"excluded_categories",` → `"included_categories",` 并在其下补一行 `"include_other",`。
- 第 46 行 `"excluded_categories": ["sports"],` → `"included_categories": ["politics"],` 并补一行 `"include_other": False,`。
- 第 64 行 `assert tmpl["excluded_categories"] == ["sports"]` → 两行：
```python
    assert tmpl["included_categories"] == ["politics"]
    assert tmpl["include_other"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_settings_routes.py -q`
Expected: FAIL（GET 缺 `included_categories` / POST round-trip 不符）

> 注：本契约测试实际由 Task 1 的 `TEMPLATE_DEFAULTS` 改动即可通过（路由未改）。此处先更新测试把契约钉死，Step 4 验证。

- [ ] **Step 3: 改 config.html HTML 块**

第 99-104 行整块替换为：
```html
        <h3>做市品类（只做勾选的）</h3>
        <div id="included-categories" class="form-inline"></div>
        <label class="cat-other"><input type="checkbox" id="include-other"> 其他/未分类<span id="other-count"></span></label>
        <p class="hint" style="color:#888;font-size:12px">数量为当前奖励市场中该品类的市场数（每次扫描后刷新）；勾中的品类才做市，「其他/未分类」= 不属于以上任何品类的市场。</p>
```

- [ ] **Step 4: 加 renderCategories + 改 loadStrategy + 改保存**

在 `<script>` 内合适处（`loadStrategy` 之前）新增：
```javascript
function renderCategories(catalog, selected, includeOther) {
    const box = document.getElementById('included-categories');
    box.innerHTML = '';
    const sel = new Set(selected || []);
    const cats = catalog.categories || [];
    const known = new Set(cats.map(c => c.slug));
    // 有市场(count>=1)、或未就绪(无池子)、或已选 —— 保证已选不丢、扫描前也能配
    const shown = cats.filter(c => !catalog.ready || (c.count || 0) >= 1 || sel.has(c.slug));
    // 已选但目录里没有的(极少)补上
    (selected || []).forEach(s => { if (!known.has(s)) shown.push({slug: s, label: s, count: 0}); });
    shown.forEach(c => {
        const cnt = catalog.ready ? ' (' + (c.count || 0) + ')' : '';
        const lab = document.createElement('label');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = c.slug;
        cb.checked = sel.has(c.slug);
        lab.appendChild(cb);
        lab.appendChild(document.createTextNode(' ' + c.label + cnt));
        box.appendChild(lab);
    });
    const other = document.getElementById('include-other');
    if (other) other.checked = !!includeOther;
    const oc = document.getElementById('other-count');
    if (oc) oc.textContent = catalog.ready ? ' (' + (catalog.other_count || 0) + ')' : '';
}
```
`loadStrategy`（第 206-228 行）改为并行取模板 + 目录：
```javascript
function loadStrategy(tid) {
    Promise.all([
        fetch(`/api/templates/${tid}`).then(r => r.json()),
        fetch('/api/categories').then(r => r.json()).catch(() => ({ready: false, categories: [], other_count: 0})),
    ]).then(([data, catalog]) => {
        const form = document.getElementById('strategy-form');
        Object.keys(data).forEach(key => {
            const input = form.querySelector(`[name="${key}"]`);
            if (input) input.value = data[key];
        });
        renderCategories(catalog, data.included_categories || [], data.include_other);
        const ps = data.per_share_reward_thresholds || {};
        document.querySelectorAll('#per-share-thresholds input[data-bracket]').forEach(inp => {
            const b = inp.getAttribute('data-bracket');
            inp.value = (ps[b] !== undefined) ? ps[b] : 0.30;
        });
        renderTierEditor(data.tier_rules || []);
        document.getElementById('tier-match-var').value =
            data.tier_match_var || 'cumulative_thickness';
        renderAmountTable(data.amount_value_table || []);
        updateTierMatchVar();
        updateStopMode();
    });
}
```
保存段第 477-479 行整块替换为：
```javascript
    data.included_categories = Array.from(
        document.querySelectorAll('#included-categories input[type=checkbox]:checked')
    ).map(cb => cb.value);
    data.include_other = !!document.getElementById('include-other').checked;
```

- [ ] **Step 5: 校验前端语法 + 无 BOM + 跑契约测试**

Run:
```bash
node --check web/templates/config.html 2>/dev/null || echo "含 HTML,node --check 不适用,改人工核对 <script> 段"
python - <<'PY'
b = open("web/templates/config.html","rb").read(3)
print("BOM" if b == b"\xef\xbb\xbf" else "no-BOM")
PY
pytest tests/test_settings_routes.py -q
```
Expected: no-BOM；`test_settings_routes.py` PASS。

> HTML 里嵌 `<script>`，`node --check` 对整文件会报（HTML 非 JS）。改为人工核对新增 JS 段无语法错，或把 `<script>` 内容抽临时 .js 跑 `node --check`。

- [ ] **Step 6: 提交**

```bash
git add web/templates/config.html tests/test_settings_routes.py
git commit -m "feat(config-ui): 品类改为动态白名单勾选(/api/categories + 其他兜底)"
```

---

### Task 7: 版本号 + 发版说明

**Files:**
- Modify: `version.py`（第 7 行）
- 全量回归

**Interfaces:** 无。

- [ ] **Step 1: 全量测试**

Run: `pytest -q`
Expected: 全绿（若有遗漏的 `excluded_categories` fixture 报错，按报错逐一改为 `included_categories`/`include_other` 后重跑）。

- [ ] **Step 2: 排查残留旧字段**

Run: `git grep -n "excluded_categories"`
Expected: 仅出现在历史 spec/plan 文档（`docs/`）；生产代码（`config.py`/`engine/`/`web/`/`tests/`）应为 0 处。若仍有生产代码引用，回到对应 Task 修掉。

- [ ] **Step 3: 版本号 → 3.0.0**

`version.py` 第 7 行 `__version__ = "2.4.0"` → `__version__ = "3.0.0"`。

- [ ] **Step 4: 提交**

```bash
git add version.py
git commit -m "chore(release): v3.0.0 — 品类白名单勾选(黑名单→白名单+其他兜底)"
```

- [ ] **Step 5: 发版说明要点（交给用户/发版流程，勿自动发版）**

在发版 RELEASE_NOTES 里点明：**品类筛选已从"排除黑名单"改为"白名单勾选"**——升级后请到「配置」页确认所选做市品类；此前自定义过排除品类的用户，其旧设置不再生效，模板回落到默认（体育/电竞/天气不做、其余含未分类都做）。

---

## Self-Review

**1. Spec coverage：**
- 白名单语义 → Task 2/3（`market_wanted`）✓
- curated 名单 × 实时交集 → Task 1（`CATEGORY_CATALOG`）+ Task 5（`category_counts`）+ Task 6（渲染）✓
- 「其他/未分类」兜底 → Task 2（`market_wanted` 空 tags 分支）+ Task 3（整名单打标签保证"其他"判定准）+ Task 6（`include_other` 勾选）✓
- 默认延续行为 → Task 1（`DEFAULT_INCLUDED_CATEGORIES`）✓
- 去重键 → Task 4 ✓
- 配置页 UI → Task 6 ✓
- 版本 MAJOR → Task 7 ✓
- 迁移说明（旧 `excluded_categories` 死行无害、发版提示）→ Task 7 Step 5 ✓

**2. Placeholder scan：** 无 TBD/TODO；每步含实际代码与命令。

**3. Type consistency：** `market_wanted(tags, included, include_other)` 在 Task 2 定义、Task 3 两处调用签名一致；`category_counts(catalog)` 返回 `{"categories":[{slug,label,count}], "other_count"}` 与 Task 5 manager/route、Task 6 渲染一致；`included_categories: list[str]`、`include_other: bool` 全程一致。

**已知取舍（写入代码注释/发版说明即可，不阻断）：** 白名单 OR 语义下，同时带多标签的市场（如既 `games` 又 `esports`）只要命中任一勾选品类即收——无法 100% 复刻旧"命中任一排除即弃"，属可接受近似。
