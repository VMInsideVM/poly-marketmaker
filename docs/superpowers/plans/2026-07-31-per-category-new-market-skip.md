# 新市场保护按品类勾选 + 配置页钱包余额列 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「跳过新建市场」从整模板一刀切改成按做市品类勾选启用，并在配置页钱包表加一列余额。

**Architecture:** 新增两个模板级键 `skip_new_categories` / `skip_new_other`（默认全部品类，升级零行为变化）。品类归属判定复用做市白名单已有的那套逻辑，抽成 `market_in_categories`，由 `market_wanted` 一行封装，两处口径不会漂。判定接入点仍是原有的两处：共享发现阶段的 `loosest_new_market_hours`（改成按市场算门槛）和每模板的 `prefilter_for_template`。配置页把品类勾选从一排 inline label 改成四列表格，「跳过新建市场」开关与保护期小时数一并挪进来。钱包余额是纯前端，`/api/wallets` 早就在返回 `balance`。

**Tech Stack:** Python 3 / Flask / SQLite / pytest；前端是无框架的原生 JS + Jinja 模板。

## Global Constraints

- 分支已建好：`feat/per-category-new-market-skip`。不在 main 上实现。
- **升级零行为变化**：`skip_new_categories` 默认是全部 14 个 curated slug，`skip_new_other` 默认 `True`。老模板没存这两个键，`get_template` 合并 `TEMPLATE_DEFAULTS` 后判定结果与改动前逐字相同。
- **缺键 fail-open 方向 = 不保护**：模板字典里没有这两个键时按「该市场不受保护」处理（不排除任何市场），与 `created_at` 解析不出即保留同方向。生产路径上 `get_template` 保证键存在，这条只兜测试和手改 DB。
- **判定口径 = 任一命中**：市场的任意一个 tag 在保护名单里即保护；空 tags（无 curated 标签，即「其他/未分类」）由 `skip_new_other` 单独管。
- `new_market_hours = 0` 视同不筛的既有语义不变。
- **前端任务（Task 5、Task 6）不要派给 subagent**：本仓库有过 subagent 把中文写成形近别字、并给文件加 BOM 的前科。由主会话直接写，写完 `grep` 检查 BOM。
- **本仓库有 .py 自动格式化 hook，会 reflow 整个文件**。每个 Python 任务提交前跑 `git diff --stat`，把与本任务无关的重新折行还原掉。
- 用户可见字符串一律简体中文。

---

### Task 1: `market_in_categories` 纯函数 + `market_wanted` 改成一行封装

**Files:**
- Modify: `engine/categories.py`（模块 docstring + 新增函数 + 改 `market_wanted`）
- Test: `tests/test_categories.py`

**Interfaces:**
- Consumes: 无（本任务是最底层）。
- Produces: `market_in_categories(tags, slugs, include_untagged: bool) -> bool`。`tags` 是市场命中的 curated slug 列表（可为 `None`/空），`slugs` 是任意可迭代的 slug 集合，`include_untagged` 是「空 tags 算不算落在集合内」。Task 3、Task 4 都调用它。`market_wanted(tags, included, include_other) -> bool` 签名与行为不变。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_categories.py`。同时把顶部 import 块里加上 `market_in_categories`：

```python
from engine.categories import (
    included_union,
    any_include_other,
    tag_pool,
    market_in_categories,
    market_wanted,
    count_by_category,
)
```

测试追加在 `test_market_wanted_categorized_but_unselected_is_not_other` 之后、`test_count_by_category` 之前：

```python
def test_market_in_categories_hit():
    assert market_in_categories(["politics"], {"politics", "ai"}, False) is True


def test_market_in_categories_any_tag_hits():
    # 多标签市场:任一 tag 命中即算落在集合内(新市场保护的口径)
    assert (
        market_in_categories(["politics", "geopolitics"], {"geopolitics"}, False) is True
    )


def test_market_in_categories_miss():
    assert market_in_categories(["sports"], {"politics"}, False) is False


def test_market_in_categories_untagged_follows_flag():
    # 空 tags = 无 curated 标签(即「其他/未分类」),由 include_untagged 单独管
    assert market_in_categories([], {"politics"}, True) is True
    assert market_in_categories([], {"politics"}, False) is False


def test_market_in_categories_tagged_but_unlisted_is_not_untagged():
    # 有 curated 标签但不在名单里:即便 include_untagged 也不算(它不是「其他」)
    assert market_in_categories(["sports"], {"politics"}, True) is False


def test_market_in_categories_empty_slugs():
    # 空名单 + 不收未分类 -> 恒 False。这是「模板缺键按不保护处理」的依据。
    assert market_in_categories(["politics"], [], False) is False
    assert market_in_categories([], [], False) is False


def test_market_in_categories_none_tags():
    # tags 为 None(市场记录还没打过标签)不能抛
    assert market_in_categories(None, {"politics"}, False) is False
    assert market_in_categories(None, {"politics"}, True) is True
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `pytest tests/test_categories.py -v`
Expected: FAIL，`ImportError: cannot import name 'market_in_categories' from 'engine.categories'`，整个文件收集失败。

- [ ] **Step 3: 写实现**

`engine/categories.py`，把现有的 `market_wanted` 替换成下面两个函数（位置不变，仍在 `tag_pool` 与 `count_by_category` 之间）：

```python
def market_in_categories(tags, slugs, include_untagged: bool) -> bool:
    """市场是否落在给定品类集合内。

    命中任一 slug -> True;空 tags(= 无 curated 标签,即「其他/未分类」)且
    include_untagged -> True;其余 False。

    做市白名单(market_wanted)与新市场保护名单共用这一份口径,两处判定不会漂
    (同 has_cliff_below 被下单与 Step3 共用)。
    """
    tags = tags or []
    if set(slugs) & set(tags):
        return True
    return bool(include_untagged) and not tags


def market_wanted(tags, included, include_other: bool) -> bool:
    return market_in_categories(tags, included, include_other)
```

同时把模块 docstring 里描述 `market_wanted` 的那一条改成：

```
- market_in_categories: 市场是否落在某个品类集合内(命中任一 slug,或空 tags 且收未分类)。
  做市白名单 market_wanted 与新市场保护名单共用它。空 tags 恒指"无 curated
  标签"——故打标签必须覆盖整份 catalog,否则未勾选品类会被误判成「其他」。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_categories.py -v`
Expected: PASS，包括 5 个既有的 `test_market_wanted_*`（验证一行封装没改口径）。

- [ ] **Step 5: 提交**

```bash
git diff --stat   # 确认没有被格式化 hook 卷进无关 reflow
git add engine/categories.py tests/test_categories.py
git commit -m "refactor(categories): 抽出 market_in_categories,market_wanted 改为封装"
```

---

### Task 2: 两个模板键 + `_active_templates` 去重键

**Files:**
- Modify: `config.py`（`ALL_CATEGORY_SLUGS` 常量 + `TEMPLATE_DEFAULTS` 两个键）
- Modify: `engine/manager.py:1041-1054`（去重键与其上方注释）
- Test: `tests/test_database.py`、`tests/test_manager.py`

**Interfaces:**
- Consumes: 无。
- Produces: 模板字典的两个键 `skip_new_categories`（`list[str]`，默认全部 14 个 curated slug）与 `skip_new_other`（`bool`，默认 `True`）；Task 3、Task 4、Task 5 都读它们。`config.ALL_CATEGORY_SLUGS`（`list[str]`，`CATEGORY_CATALOG` 顺序）。

- [ ] **Step 1: 写失败的测试**

`tests/test_database.py`，追加到文件末尾的 `test_skip_new_markets_defaults` 之后：

```python
def test_skip_new_categories_defaults():
    """新市场保护默认覆盖全部 curated 品类 + 「其他」——升级后行为与一刀切时期一致。"""
    from config import TEMPLATE_DEFAULTS, CATALOG_SLUGS

    assert set(TEMPLATE_DEFAULTS["skip_new_categories"]) == set(CATALOG_SLUGS)
    assert TEMPLATE_DEFAULTS["skip_new_other"] is True


def test_skip_new_categories_default_is_ordered_list():
    """默认值必须是有确定顺序的 list(不是 frozenset):要存进 DB、要进去重键。"""
    from config import TEMPLATE_DEFAULTS, CATEGORY_CATALOG

    assert TEMPLATE_DEFAULTS["skip_new_categories"] == [
        c["slug"] for c in CATEGORY_CATALOG
    ]
```

`tests/test_manager.py`，追加到 `TestActiveTemplatesDedupKey` 类里、`test_new_market_hours_variants_not_deduped` 之后：

```python
    def test_skip_new_categories_variants_not_deduped(self):
        def tmpl_for(addr):
            return self._base(
                skip_new_markets=True,
                new_market_hours=24,
                skip_new_categories=["politics"] if addr == "0xA" else ["crypto"],
            )

        assert len(self._mgr_with(tmpl_for)._active_templates()) == 2

    def test_skip_new_other_variants_not_deduped(self):
        def tmpl_for(addr):
            return self._base(
                skip_new_markets=True,
                new_market_hours=24,
                skip_new_other=(addr == "0xA"),
            )

        assert len(self._mgr_with(tmpl_for)._active_templates()) == 2
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `pytest tests/test_database.py::test_skip_new_categories_defaults tests/test_database.py::test_skip_new_categories_default_is_ordered_list "tests/test_manager.py::TestActiveTemplatesDedupKey" -v`
Expected: FAIL。两个 database 测试报 `KeyError: 'skip_new_categories'`；两个新的去重测试报 `assert 1 == 2`（两个模板被去重成了一个）。既有的 3 个去重测试仍 PASS。

- [ ] **Step 3: 写实现**

`config.py`，在 `DEFAULT_INCLUDED_CATEGORIES` 定义之后（第 30 行下面）加：

```python
# 新市场保护的默认名单:全部 curated 品类。默认全保护 = 升级后与「整模板一刀切」时期
# 逐字同行为。取自 CATEGORY_CATALOG 而非 frozenset CATALOG_SLUGS:要存进 DB、要进
# _active_templates 去重键,顺序必须确定。
ALL_CATEGORY_SLUGS = [c["slug"] for c in CATEGORY_CATALOG]
```

`config.py`，`TEMPLATE_DEFAULTS` 里紧接 `"new_market_hours": 24.0,` 之后加：

```python
    # 新市场保护只对勾选的品类生效(默认全部,升级零行为变化)。判定口径=任一 tag 命中;
    # skip_new_other 管「其他/未分类」,与 include_other 同形。
    "skip_new_categories": ALL_CATEGORY_SLUGS,
    "skip_new_other": True,
```

`engine/manager.py`，把 `_active_templates` 里去重键上方的注释与键本身改成：

```python
            # 去重键须含采集器实际用到的每个维度:品类包含集 + 是否含其他 + 奖励下限
            # (决定预筛 min_floor) + 结算窗口 + 档位 sizes + 新市场开关/小时数/保护品类
            # (后四者决定发现阶段的并集门控:窗口/档位/新市场门槛不同的模板不能被去重
            # 成一个,否则另一个的门槛没进并集就会误剔)。
            key = (
                tuple(sorted(tmpl.get("included_categories", []) or [])),
                bool(tmpl.get("include_other", False)),
                tmpl.get("min_reward_usd", 0),
                tmpl.get("min_settlement_days"),
                tmpl.get("max_settlement_days"),
                tuple(sorted(enabled_sizes(tmpl.get("size_tiers") or []))),
                bool(tmpl.get("skip_new_markets", False)),
                tmpl.get("new_market_hours"),
                tuple(sorted(tmpl.get("skip_new_categories", []) or [])),
                bool(tmpl.get("skip_new_other", False)),
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_database.py tests/test_manager.py -v`
Expected: PASS，两个文件全绿。

- [ ] **Step 5: 提交**

```bash
git diff --stat
git add config.py engine/manager.py tests/test_database.py tests/test_manager.py
git commit -m "feat(config): skip_new_categories/skip_new_other 模板键 + 扩去重键"
```

---

### Task 3: 发现阶段 `loosest_new_market_hours` 改成按市场算门槛

**Files:**
- Modify: `engine/scanner.py:25-31`（import）、`engine/scanner.py:162-174`（函数）、`engine/scanner.py:246-294`（`discover_candidates` 调用点）
- Test: `tests/test_scanner.py`（`TestLoosestNewMarketHours`、`TestDiscoverySkipNewMarkets`）

**Interfaces:**
- Consumes: Task 1 的 `market_in_categories(tags, slugs, include_untagged)`；Task 2 的两个模板键。
- Produces: `loosest_new_market_hours(templates, tags) -> float`（**签名变了，多一个 `tags` 位置参数**）。除 `discover_candidates` 外无生产调用点。

- [ ] **Step 1: 改既有测试 + 写新的失败测试**

`tests/test_scanner.py`，`TestLoosestNewMarketHours` 整个类替换成：

```python
class TestLoosestNewMarketHours:
    """发现阶段是钱包无关的共享阶段，只能用「所有模板都会因太新排除它」的最宽松门槛。

    「会排除」现在是两个条件的合取：模板开了开关，且该市场的品类在这个模板的保护名单里。
    """

    def _t(self, on, hrs, cats=None, other=True):
        return {
            "skip_new_markets": on,
            "new_market_hours": hrs,
            "skip_new_categories": ["politics"] if cats is None else cats,
            "skip_new_other": other,
        }

    def test_all_on_takes_min(self):
        tmpls = [self._t(True, 48), self._t(True, 24)]
        assert loosest_new_market_hours(tmpls, ["politics"]) == 24

    def test_any_off_returns_zero(self):
        tmpls = [self._t(True, 48), self._t(False, 24)]
        assert loosest_new_market_hours(tmpls, ["politics"]) == 0

    def test_empty_returns_zero(self):
        assert loosest_new_market_hours([], ["politics"]) == 0

    def test_hours_none_treated_as_zero(self):
        assert loosest_new_market_hours([self._t(True, None)], ["politics"]) == 0

    def test_malformed_hours_treated_as_zero(self):
        """非数字/负数的保护期归 0(= 不筛),不抛——DB 值可能被手改。"""
        assert loosest_new_market_hours([self._t(True, "abc")], ["politics"]) == 0
        assert loosest_new_market_hours([self._t(True, -5)], ["politics"]) == 0

    def test_missing_keys_treated_as_off(self):
        assert loosest_new_market_hours([{}], ["politics"]) == 0

    def test_unprotected_category_returns_zero(self):
        tmpls = [self._t(True, 24, cats=["crypto"])]
        assert loosest_new_market_hours(tmpls, ["politics"]) == 0

    def test_any_tag_hit_protects(self):
        # 多标签市场:命中保护名单里任一个就够
        tmpls = [self._t(True, 24, cats=["crypto"])]
        assert loosest_new_market_hours(tmpls, ["politics", "crypto"]) == 24

    def test_untagged_market_follows_skip_new_other(self):
        assert loosest_new_market_hours([self._t(True, 24, other=True)], []) == 24
        assert loosest_new_market_hours([self._t(True, 24, other=False)], []) == 0

    def test_one_template_not_protecting_returns_zero(self):
        # A 保护 politics、B 只保护 crypto -> 共享阶段不能排除 politics 的新市场
        tmpls = [self._t(True, 24, cats=["politics"]), self._t(True, 24, cats=["crypto"])]
        assert loosest_new_market_hours(tmpls, ["politics"]) == 0

    def test_missing_category_keys_treated_as_unprotected(self):
        # 缺 skip_new_categories/skip_new_other 时 fail-open 到「不保护」(= 不排除)
        tmpls = [{"skip_new_markets": True, "new_market_hours": 24}]
        assert loosest_new_market_hours(tmpls, ["politics"]) == 0
```

`tests/test_scanner.py`，`TestDiscoverySkipNewMarkets` 的 `_tmpl` 加两个键：

```python
    def _tmpl(self, **over):
        t = {
            "included_categories": [],
            "include_other": True,
            "min_reward_usd": 0,
            "size_tiers": [_tier(100)],
            "min_settlement_days": 0,
            "max_settlement_days": None,
            "skip_new_markets": True,
            "new_market_hours": 24,
            "skip_new_categories": [],
            "skip_new_other": True,
        }
        t.update(over)
        return t
```

（这个类里的市场都没有 curated 标签：假 api 只在 `tag_slug is None` 时返回数据，逐 slug 查询全空，`tag_pool` 给每条打出 `tags == []`。所以既有 7 个用例靠 `skip_new_other=True` 维持原语义。）

同一个类里追加带标签的用例：

```python
    def _api_tagged(self, markets, slug):
        """让指定 slug 的品类查询也返回这些市场,使 tag_pool 给它们打上该标签。"""
        api = MagicMock()

        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None or tag_slug == slug:
                return list(markets)
            return []

        api.get_rewards_markets.side_effect = fake_rewards
        api.get_rewards_for_market.return_value = []
        return api

    def _cat_tmpl(self, **over):
        # 只做 politics、不收其他:市场带上 politics 标签才进得了候选池
        return self._tmpl(
            included_categories=["politics"], include_other=False, **over
        )

    def test_protected_category_new_market_excluded(self):
        api = self._api_tagged([self._mkt("NEW", 5), self._mkt("OLD", 100)], "politics")
        tmpl = self._cat_tmpl(skip_new_categories=["politics"], skip_new_other=False)
        assert self._pool_ids(api, [tmpl]) == {"OLD"}

    def test_unprotected_category_new_market_kept(self):
        api = self._api_tagged([self._mkt("NEW", 5)], "politics")
        tmpl = self._cat_tmpl(skip_new_categories=["crypto"], skip_new_other=False)
        assert self._pool_ids(api, [tmpl]) == {"NEW"}

    def test_one_template_not_protecting_keeps_new_market(self):
        # A 保护 politics、B 不保护 -> 共享阶段一个都不排,留给 prefilter 各自精筛
        api = self._api_tagged([self._mkt("NEW", 5)], "politics")
        tmpls = [
            self._cat_tmpl(skip_new_categories=["politics"], skip_new_other=False),
            self._cat_tmpl(skip_new_categories=["crypto"], skip_new_other=False),
        ]
        assert self._pool_ids(api, tmpls) == {"NEW"}
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `pytest tests/test_scanner.py::TestLoosestNewMarketHours tests/test_scanner.py::TestDiscoverySkipNewMarkets -v`

Expected，逐条核对（不核对就等于没验证测试真的在测东西）：

- `TestLoosestNewMarketHours` 11 条**全部** FAIL，报 `TypeError: loosest_new_market_hours() takes 1 positional argument but 2 were given`。
- `test_unprotected_category_new_market_kept` FAIL：`assert set() == {'NEW'}`。旧实现不看品类，把 NEW 排掉了。
- `test_one_template_not_protecting_keeps_new_market` FAIL：`assert set() == {'NEW'}`，同上。
- `test_protected_category_new_market_excluded` 在旧实现下就 **PASS**（旧实现模板全开就按 24h 排，恰好得到同样结果）。它是防回归的守卫，不是红灯用例，PASS 属预期。
- `TestDiscoverySkipNewMarkets` 既有 7 条仍全部 PASS（`_tmpl` 新增的两个键旧代码不读）。

- [ ] **Step 3: 写实现**

`engine/scanner.py`，import 块加 `market_in_categories`：

```python
from engine.categories import (
    included_union,
    any_include_other,
    tag_pool,
    market_in_categories,
    market_wanted,
    count_by_category,
)
```

`engine/scanner.py`，`loosest_new_market_hours` 整个替换：

```python
def loosest_new_market_hours(templates, tags) -> float:
    """发现阶段可安全排除该市场的门槛(小时);tags 是该市场命中的 curated 品类。

    发现阶段是钱包无关的共享阶段,只有**每个**模板都会因「太新」排除这个市场才能在这里
    排除(否则会把没排除它的模板要的市场也一起剔掉);此时取各模板 N 的最小值(最宽松)。
    任一模板没开开关、或没把该市场的品类列进自己的保护名单 -> 0(不排除)。空列表 -> 0。
    缺保护名单的键按「不保护」处理(fail-open,与 created_at 解析不出即保留同方向)。
    """
    hours = []
    for t in templates:
        if not t.get("skip_new_markets"):
            return 0.0
        if not market_in_categories(
            tags,
            t.get("skip_new_categories") or [],
            bool(t.get("skip_new_other")),
        ):
            return 0.0
        hours.append(_new_market_hours(t))
    return min(hours) if hours else 0.0
```

`engine/scanner.py`，`discover_candidates` 里删掉循环外那两行的第一行，注释同步改（原第 246-249 行）：

```python
        # 「新市场」门槛:发现阶段只能排除「每个模板都会因太新排除」的市场,门槛按市场的
        # 品类逐条算(见 loosest_new_market_hours);各模板自己的 N 由 prefilter_for_template
        # 精筛。created_at 由奖励端点白拿,判定不发网络请求。
        now = time.time()
```

`engine/scanner.py`，候选池循环里把原来的 `if min_age_hours:` 块（原第 290-293 行）替换成：

```python
            min_age_hours = loosest_new_market_hours(templates, market.get("tags", []))
            if min_age_hours:
                age = market_age_hours(market.get("created_at", ""), now)
                if age is not None and age < min_age_hours:
                    continue  # 太新;created_at 取不到 -> fail-open 保留
```

放在原位置不变：仍在 `_batch_rate(market) < min_floor` 判断之后、`(priced if _should_price(market) else extra).append(market)` 之前。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_scanner.py -v`
Expected: PASS，整个文件全绿。

- [ ] **Step 5: 提交**

```bash
git diff --stat
git add engine/scanner.py tests/test_scanner.py
git commit -m "feat(scanner): 发现阶段新市场门槛按市场品类算"
```

---

### Task 4: `prefilter_for_template` 按品类判定

**Files:**
- Modify: `engine/scanner.py:505-550`（`prefilter_for_template`）
- Test: `tests/test_scanner.py`（`TestPrefilterForTemplate`）

**Interfaces:**
- Consumes: Task 1 的 `market_in_categories`（Task 3 已在 `engine/scanner.py` 顶部 import 好）；Task 2 的两个模板键。
- Produces: 无新符号，`prefilter_for_template(candidate_pool, template, wallet_address)` 签名不变。

- [ ] **Step 1: 改既有测试 helper + 写新的失败测试**

`tests/test_scanner.py`，`TestPrefilterForTemplate._template` 加两个键：

```python
    def _template(self, **over):
        t = {
            "min_reward_usd": 6,
            "min_price_cents": 10,
            "max_price_cents": 90,
            "max_spread_cents": 6,
            "min_settlement_days": 0,
            "included_categories": ["politics"],
            "include_other": True,
            "size_tiers": [_tier(100)],
            "skip_new_categories": ["politics"],
            "skip_new_other": True,
        }
        t.update(over)
        return t
```

（既有 5 个新市场用例的候选都不带 tags，靠 `skip_new_other=True` 维持原语义。）

同一个类里，追加到 `test_each_template_uses_own_hours` 之后：

```python
    def test_drops_new_market_in_protected_category(self):
        scanner = self._scanner()
        pool = [self._aged("NEW", 5, tags=["politics"])]
        tmpl = self._template(
            skip_new_markets=True,
            new_market_hours=24,
            skip_new_categories=["politics"],
        )
        assert self._ids(scanner, pool, tmpl) == set()

    def test_keeps_new_market_in_unprotected_category(self):
        scanner = self._scanner()
        pool = [self._aged("NEW", 5, tags=["politics"])]
        tmpl = self._template(
            skip_new_markets=True,
            new_market_hours=24,
            skip_new_categories=["crypto"],
        )
        assert self._ids(scanner, pool, tmpl) == {"NEW"}

    def test_any_tag_hit_protects(self):
        # 多标签市场:命中保护名单里任一个就跳过
        scanner = self._scanner()
        pool = [self._aged("NEW", 5, tags=["politics", "crypto"])]
        tmpl = self._template(
            skip_new_markets=True,
            new_market_hours=24,
            skip_new_categories=["crypto"],
        )
        assert self._ids(scanner, pool, tmpl) == set()

    def test_untagged_new_market_follows_skip_new_other(self):
        scanner = self._scanner()
        pool = [self._aged("NEW", 5)]
        on = self._template(
            skip_new_markets=True,
            new_market_hours=24,
            skip_new_categories=[],
            skip_new_other=True,
        )
        off = self._template(
            skip_new_markets=True,
            new_market_hours=24,
            skip_new_categories=[],
            skip_new_other=False,
        )
        assert self._ids(scanner, pool, on) == set()
        assert self._ids(scanner, pool, off) == {"NEW"}
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `pytest tests/test_scanner.py::TestPrefilterForTemplate -v`

Expected，逐条核对：

- `test_keeps_new_market_in_unprotected_category` FAIL：`assert set() == {'NEW'}`。旧实现不看品类。
- `test_untagged_new_market_follows_skip_new_other` FAIL：`off` 那一半断言报 `assert set() == {'NEW'}`。
- `test_drops_new_market_in_protected_category` 与 `test_any_tag_hit_protects` 在旧实现下就 **PASS**（旧实现照排新市场，结果相同）。它们是防回归守卫，PASS 属预期。
- 既有 5 条 `test_*new_market*` 用例仍全部 PASS（靠 `_template` 新增的 `skip_new_other: True` 维持原语义）。

- [ ] **Step 3: 写实现**

`engine/scanner.py` `prefilter_for_template`，在 `new_hours = _new_market_hours(template)` 之后加两行取值：

```python
        skip_new = bool(template.get("skip_new_markets"))
        new_hours = _new_market_hours(template)
        skip_cats = set(template.get("skip_new_categories", []) or [])
        skip_other = bool(template.get("skip_new_other", False))
        now = time.time()
```

循环里把新市场那一段改成（`tags` 从 market 取一次，白名单那行仍用 `market_wanted`，只是改读同一个变量）：

```python
        survivors = []
        for market in candidate_pool:
            tags = market.get("tags", [])
            if not market_wanted(tags, included, include_other):
                continue
            total_rate = _batch_rate(market)
            market_reward = market.get("market_reward", total_rate)
            if total_rate < min_reward or market_reward < min_reward:
                continue
            end_ts = _parse_end_date(market.get("end_date", ""))
            # 结算窗口 [min_days, max_days](整天)。无法解析结算日 -> 保留(fail-open)。
            if end_ts and not _in_settlement_window(end_ts, min_days, max_days):
                continue
            if skip_new and new_hours and market_in_categories(tags, skip_cats, skip_other):
                # 该品类开了保护:创建不足 new_hours 小时的市场不做;
                # created_at 取不到 -> fail-open 保留。
                age = market_age_hours(market.get("created_at", ""), now)
                if age is not None and age < new_hours:
                    continue
```

循环后半段（冷却、档位、价带、`survivors.append`）一个字不动。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_scanner.py -v`
Expected: PASS，整个文件全绿。

- [ ] **Step 5: 全量回归 + 提交**

```bash
pytest
git diff --stat
git add engine/scanner.py tests/test_scanner.py
git commit -m "feat(scanner): prefilter 的新市场保护按品类生效"
```

Expected: `pytest` 全绿。

---

### Task 5: 配置页品类区改四列表格（**主会话直接写，不派 subagent**）

**Files:**
- Modify: `web/templates/config.html`

**Interfaces:**
- Consumes: Task 2 的两个模板键（`/api/templates/<id>` 已按 `TEMPLATE_DEFAULTS` 白名单自动读写，后端路由无需改）。
- Produces: 无 Python 符号。DOM 契约：`#included-categories` 是 `<tbody>`；做市勾选是 `input.cat-make[value=slug]`，保护勾选是 `input.cat-skip[value=slug]`；「其他」行两个框的 id 分别是 `include-other` 与 `skip-new-other`。

- [ ] **Step 1: 换掉品类区的 HTML**

删掉 `web/templates/config.html` 第 82-90 行那两个 form-group（「跳过新建市场」checkbox 与「新市场保护期（小时）」输入框），它们要挪到品类区。删完之后 `form-grid` 里紧邻的是「最长结算天数」和「止损方式」。

然后把第 166-172 行（`<h3>做市品类…` 到那条 hint `<p>`）整段替换成：

```html
        <h3>做市品类（只做勾选的）
            <a href="#" onclick="refreshCategories(event)" style="font-size:13px;font-weight:normal">刷新计数</a>
            <span id="cat-updated" class="hint" style="color:#888;font-size:12px;font-weight:normal"></span>
        </h3>
        <div class="form-inline" style="align-items:center;flex-wrap:wrap">
            <label style="display:flex;align-items:center;gap:6px;white-space:nowrap">
                <input type="checkbox" id="skip-new-markets" onchange="updateSkipNewCol()" style="flex:none;width:auto"> 跳过新建市场
            </label>
            <label style="display:flex;align-items:center;gap:6px;white-space:nowrap">
                保护期 <input type="number" name="new_market_hours" step="1" min="0" style="flex:none;width:80px"> 小时
            </label>
        </div>
        <div class="table-scroll">
            <table class="data-table">
                <thead><tr><th>品类</th><th>计数</th><th>做市</th><th>跳过新市场</th></tr></thead>
                <tbody id="included-categories"></tbody>
            </table>
        </div>
        <p class="hint" style="color:#888;font-size:12px">「做市」勾中的品类才做市，「其他/未分类」= 不属于以上任何品类的市场。「跳过新市场」只在上面的总开关打开时生效：勾中的品类里，创建时间不足保护期的市场不做；没勾的品类照常做新市场。计数是当前该品类的市场数，每次「扫描市场」后自动更新，也可点上方「刷新计数」手动拉取；没有计数不影响勾选。</p>
```

注意「跳过新建市场」开关与「保护期」输入框仍在 `#strategy-form` 内部（品类区本来就在 form 里，第 180 行才 `</form>`），所以 `new_market_hours` 照旧被表单的 `input[type=number][name]` 循环收走。

- [ ] **Step 2: 换掉 `renderCategories` 与相关 JS**

把第 256-290 行的 `renderCategories` 整个函数替换成下面三个函数（`relTimeCat` 保持原样不动）：

```js
function makeCatRow(slug, label, count, make, skip, isOther) {
    const tr = document.createElement('tr');
    const nameTd = document.createElement('td');
    nameTd.textContent = label;
    const cntTd = document.createElement('td');
    cntTd.textContent = (count == null) ? '—' : count;
    const makeTd = document.createElement('td');
    const makeCb = document.createElement('input');
    makeCb.type = 'checkbox';
    makeCb.checked = !!make;
    makeCb.onchange = updateSkipNewCol;
    if (isOther) { makeCb.id = 'include-other'; }
    else { makeCb.className = 'cat-make'; makeCb.value = slug; }
    makeTd.appendChild(makeCb);
    const skipTd = document.createElement('td');
    const skipCb = document.createElement('input');
    skipCb.type = 'checkbox';
    skipCb.checked = !!skip;
    if (isOther) { skipCb.id = 'skip-new-other'; }
    else { skipCb.className = 'cat-skip'; skipCb.value = slug; }
    skipTd.appendChild(skipCb);
    tr.append(nameTd, cntTd, makeTd, skipTd);
    return tr;
}

function renderCategories(catalog, selected, includeOther, skipSelected, skipOther) {
    const box = document.getElementById('included-categories');
    box.innerHTML = '';
    const sel = new Set(selected || []);
    const skipSel = new Set(skipSelected || []);
    const cats = catalog.categories || [];
    const known = new Set(cats.map(c => c.slug));
    // 全部品类都显示(0 个市场的也在);计数只作点缀
    const shown = cats.slice();
    // 已选但目录里没有的(极少)补上:做市名单与保护名单都要补,否则勾选会在下次保存时丢失
    (selected || []).concat(skipSelected || []).forEach(s => {
        if (!known.has(s) && !shown.some(c => c.slug === s)) {
            shown.push({slug: s, label: s, count: 0});
        }
    });
    shown.forEach(c => box.appendChild(makeCatRow(
        c.slug, c.label, catalog.ready ? (c.count || 0) : null,
        sel.has(c.slug), skipSel.has(c.slug), false
    )));
    // 「其他/未分类」固定作最后一行
    box.appendChild(makeCatRow(
        '', '其他/未分类', catalog.ready ? (catalog.other_count || 0) : null,
        !!includeOther, !!skipOther, true
    ));
    const upd = document.getElementById('cat-updated');
    if (upd) upd.textContent = catalog.updated_at
        ? '· 计数更新于 ' + relTimeCat(catalog.updated_at)
        : (catalog.ready ? '' : '· 计数待扫描或刷新');
    updateSkipNewCol();
}

// 「跳过新市场」列的置灰:总开关关着、或该品类根本不做市时,这个勾选没有意义。
// 只置灰不清值——disabled 的 checkbox 依然会被 :checked 选中并保存,取消再勾回来不丢配置。
function updateSkipNewCol() {
    const on = !!(document.getElementById('skip-new-markets') || {}).checked;
    document.querySelectorAll('#included-categories tr').forEach(tr => {
        const make = tr.querySelector('input.cat-make, #include-other');
        const skip = tr.querySelector('input.cat-skip, #skip-new-other');
        if (!make || !skip) return;
        skip.disabled = !on || !make.checked;
    });
}

// 四组勾选一处读齐:刷新计数与保存两条路径共用,不会漏读某一组。
function collectCategorySelections() {
    const q = s => Array.from(document.querySelectorAll(s)).map(cb => cb.value);
    return {
        included: q('#included-categories input.cat-make:checked'),
        other: !!(document.getElementById('include-other') || {}).checked,
        skip: q('#included-categories input.cat-skip:checked'),
        skipOther: !!(document.getElementById('skip-new-other') || {}).checked,
    };
}
```

- [ ] **Step 3: 改 `refreshCategories` 与 `loadStrategy`**

把第 300-312 行的 `refreshCategories` 替换成（关键：保护勾选也要保住，否则点一次「刷新计数」就被打回默认）：

```js
// 手动刷新:只更新计数,不动用户当前勾选(四组都保住)。联网重算期间显示「刷新中…」。
function refreshCategories(ev) {
    if (ev) ev.preventDefault();
    const cur = collectCategorySelections();
    const empty = {ready: false, categories: [], other_count: 0};
    document.getElementById('included-categories').innerHTML =
        '<tr><td colspan="4" class="hint" style="color:#888">刷新中…</td></tr>';
    const draw = catalog => renderCategories(
        catalog, cur.included, cur.other, cur.skip, cur.skipOther);
    fetch('/api/categories?refresh=1').then(r => r.json()).then(draw).catch(() => draw(empty));
}
```

`loadStrategy` 里改三处：加载占位符改成表格行、`updateSkipNewCol()` 补一次、`renderCategories` 多传两个参数。

```js
function loadStrategy(tid) {
    document.getElementById('included-categories').innerHTML =
        '<tr><td colspan="4" class="hint" style="color:#888">加载中…</td></tr>';

    const tmplP = fetch(`/api/templates/${tid}`).then(r => r.json());
    const catP = fetch('/api/categories').then(r => r.json())
        .catch(() => ({ready: false, categories: [], other_count: 0}));

    // 策略参数只依赖模板数据(本地库,基本瞬时)—— 立刻填好,不等慢的品类接口
    tmplP.then(data => {
        const form = document.getElementById('strategy-form');
        Object.keys(data).forEach(key => {
            const input = form.querySelector(`[name="${key}"]`);
            if (input) input.value = data[key];
        });
        renderTiers(data.size_tiers || []);
        // checkbox 不能靠上面的 input.value 回填（那只会设 value 属性，不动 checked）
        const skipNew = document.getElementById('skip-new-markets');
        if (skipNew) skipNew.checked = !!data.skip_new_markets;
        updateSkipNewCol();
        updateStopMode();
    });

    // 品类勾选要「模板(哪些已选)+ 目录(列表/计数)」都到齐再渲染,期间显示加载中
    Promise.all([tmplP, catP]).then(([data, catalog]) => {
        renderCategories(
            catalog,
            data.included_categories || [],
            data.include_other,
            data.skip_new_categories || [],
            data.skip_new_other
        );
    });
}
```

- [ ] **Step 4: 改保存时的收集**

`strategy-form` 的 submit handler 里，把第 519-522 行那两句替换成：

```js
    const cats = collectCategorySelections();
    data.included_categories = cats.included;
    data.include_other = cats.other;
    data.skip_new_categories = cats.skip;
    data.skip_new_other = cats.skipOther;
```

第 512-515 行（`data.skip_new_markets` 与 `new_market_hours` 的 NaN 归零）保持原样不动 —— 开关和小时数虽然在页面上挪了位置，仍在同一个 form 里，收集逻辑不变。

- [ ] **Step 5: 静态检查**

```bash
python -c "import pathlib; b=pathlib.Path('web/templates/config.html').read_bytes(); print('BOM!' if b[:3]==b'\xef\xbb\xbf' else 'no BOM ok')"
```

Expected: `no BOM ok`。

把新写的这几个 JS 函数（`makeCatRow` / `renderCategories` / `updateSkipNewCol` / `collectCategorySelections` / `refreshCategories` / `loadStrategy`）单独拷进一个临时 `.js` 文件跑 `node --check`，确认没有语法错误，然后删掉临时文件。Jinja 模板整体不能直接喂给 `node --check`。

- [ ] **Step 6: 人工走查**

```bash
python app.py
```

浏览器打开 `http://127.0.0.1:5000`，登录后进配置页，逐条确认：

1. 品类表格显示 14 行 + 「其他/未分类」行，计数列有值或显 `—`。
2. 「跳过新建市场」默认未勾（`TEMPLATE_DEFAULTS` 里是 `False`），此时整个「跳过新市场」列都是灰的。
3. 勾上总开关 → 保护列解灰，且默认全部勾中（`skip_new_categories` 默认全品类、`skip_new_other` 默认 `True`）。
4. 取消某品类的「做市」→ 该行「跳过新市场」立刻置灰；勾回来 → 解灰且原勾选还在。
5. 取消几个品类的保护 → 保存 → 刷新页面 → 勾选状态与保存时一致。
6. 点「刷新计数」→ 计数更新，四组勾选一个都没变。
7. 切到另一个模板再切回来，勾选正确回填。

- [ ] **Step 7: 提交**

```bash
git add web/templates/config.html
git commit -m "feat(config-page): 品类区改四列表格,新市场保护按品类勾选"
```

---

### Task 6: 配置页钱包表加余额列（**主会话直接写，不派 subagent**）

**Files:**
- Modify: `web/templates/config.html:32-37`（表头）、`web/templates/config.html:603-622`（`loadWallets` 的行模板）

**Interfaces:**
- Consumes: `/api/wallets` 返回的 `balance` 字段（`float` 或 `null`，后端已实现，见 `web/routes.py:524-552`）。
- Produces: 无。

- [ ] **Step 1: 加表头**

`web/templates/config.html` 第 34 行改成：

```html
            <tr><th>地址</th><th>余额</th><th>存款地址</th><th>代理</th><th>备注</th><th>模板</th><th>状态</th><th>操作</th></tr>
```

- [ ] **Step 2: 加单元格**

`loadWallets` 的行模板里，紧接地址那个 `<td>` 之后插一行（格式与 `dashboard.html` 的余额列一致）：

```js
            return `
            <tr>
                <td title="${w.address}">${w.address.slice(0,6)}...${w.address.slice(-4)}</td>
                <td>${w.balance != null ? '$' + w.balance.toFixed(2) : '-'}</td>
                <td title="${w.funder || ''}">${w.funder ? w.funder.slice(0,6)+'...'+w.funder.slice(-4) : '-'}</td>
```

其余单元格不动。

- [ ] **Step 3: 人工走查**

```bash
python app.py
```

配置页钱包表能看到余额列：有钱包时显示 `$12.34` 之类，取不到余额（代理挂了 / 未登录态）显示 `-`。表头列数与每行单元格数一致（各 8 个）。

- [ ] **Step 4: 提交**

```bash
grep -c $'\xef\xbb\xbf' web/templates/config.html   # 期望 0
git add web/templates/config.html
git commit -m "feat(config-page): 钱包表显示余额"
```

---

### Task 7: 文档同步

**Files:**
- Modify: `CLAUDE.md`（Architecture 段里描述 market-age floor 的那一大段）
- Modify: `README.md:185`（配置项表格）

**Interfaces:**
- Consumes: Task 1 到 Task 4 的最终行为。
- Produces: 无。

- [ ] **Step 1: 改 README 表格**

`README.md` 第 185 行那一行替换成两行：

```markdown
| `skip_new_markets` / `new_market_hours` | `false` / 24.0 | 跳过最近 N 小时内创建的新市场（默认关；0 小时 = 不筛）。只对下面勾中的品类生效 |
| `skip_new_categories` / `skip_new_other` | 全部品类 / `true` | 新市场保护对哪些做市品类生效（tag slug）；`skip_new_other` 管「其他/未分类」。默认全部，等同升级前的一刀切行为 |
```

- [ ] **Step 2: 改 CLAUDE.md**

`CLAUDE.md` 里以 `The optional market-age floor (\`skip_new_markets\` / \`new_market_hours\`, template-level,` 开头的那一段，整段替换成：

```
The optional market-age floor (`skip_new_markets` / `new_market_hours`, template-level,
default off) skips markets created within the last N hours, using the `created_at` the
CLOB rewards endpoint already returns (no extra request). It applies only to the
categories selected in `skip_new_categories` (+ `skip_new_other` for untagged markets),
both template-level and both defaulting to *everything*, so an upgrade changes no
behavior. The category verdict is **any-hit**: a market whose tags include any protected
slug is protected; an untagged market (no curated tag at all) follows `skip_new_other`.
That verdict is the same pure function the trading whitelist uses — `market_in_categories`
in `engine/categories.py`, of which `market_wanted` is now a one-line wrapper — so the two
cannot drift. The age floor is judged twice: once in the shared `discover_candidates`,
where `loosest_new_market_hours(templates, tags)` returns a per-market threshold (a market
is excluded only if *every* template would exclude it, i.e. every template has the switch
on **and** protects that market's categories; then the smallest N wins), and once per
template in `prefilter_for_template`. Both fail open when `created_at` is missing or
unparseable, and a template missing the two category keys is treated as protecting
nothing (fail-open in the same direction). When the discovery stage does exclude a market,
it does not re-enter the pool until a later discovery round (`discovery_interval_sec`, 4 h
by default), so the effective protection window is N to N + one discovery interval; when
some template does not protect it, the market stays in the pool and each template's
`prefilter_for_template` cuts at exactly N on the 30-second placement round. Narrowing the
protected categories therefore lets more new markets back into the pool, which costs one
precise-reward fetch each per discovery round. Turning the switch on drops now-excluded
markets out of the pool, so the usual dropout pass cancels their resting buys — except in a
market the wallet is still cooling down on, which the dropout pass skips.
```

- [ ] **Step 3: 全量回归 + 提交**

```bash
pytest
git add CLAUDE.md README.md
git commit -m "docs: 新市场保护按品类勾选"
```

Expected: `pytest` 全绿。

---

## 完成后

全量 `pytest` 全绿 + 配置页人工走查通过后，用 `superpowers:finishing-a-development-branch` 决定合并方式（**由用户选**，不要自行合进 main）。

发版时注意：这次是**向后兼容的新功能**（默认值保证零行为变化），按 `docs/版本号规范.md` 属 MINOR。发版公告要写清「新市场保护现在可以按品类勾选，默认对全部品类生效，行为与升级前一致」。
