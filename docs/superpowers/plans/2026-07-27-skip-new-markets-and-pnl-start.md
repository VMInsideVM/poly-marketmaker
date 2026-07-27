# 跳过新建市场 + 台账起点前移 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增模板级开关「跳过最近 N 小时内创建的市场」（默认关，N 默认 24），并把每日盈亏台账的补漏起点从 `2026-07-01` 前移到项目首次提交日 `2026-05-17`。

**Architecture:** 市场创建时间由 CLOB 奖励端点 `/rewards/markets/multi` 的 `created_at` 字段白拿（实测 300/300 全有值），零额外网络请求。两个新纯函数落在 `engine/scanner.py`；判定做两遍——共享的发现阶段用「所有模板都开了才生效、取各模板最小 N」的最宽松门槛排除，`prefilter_for_template` 再按各模板自己的 N 精筛。台账起点只是一个常量。

**Tech Stack:** Python 3.11 / Flask / pytest / SQLite；前端是 Jinja 模板内联 JS，无构建步骤、无前端测试。

## Global Constraints

- **spec 见** `docs/superpowers/specs/2026-07-27-skip-new-markets-and-pnl-start-design.md`，与本计划冲突时以 spec 为准。
- **分支已开好**：`feat/skip-new-markets-and-pnl-start`（spec 已提交在上面）。不要在 `main` 上直接实现。
- **UI 文案一律简体中文**，与现有配置页保持一致。
- **保存的 .py 会被格式化 hook 整文件重排**。每次提交前跑 `git diff --stat`，若无关代码被重新折行，把它们还原，只留本任务的改动。
- **fail-open 铁律**：`created_at` 解析不出时保留市场，绝不因为一个字段格式变动就整池不下单。
- **`new_market_hours = 0` 视同不筛**，与开关关闭等效。两处判定统一写成 `if hrs and age is not None and age < hrs` 的形状，不拿「键是否存在」当条件。
- 跑测试：`pytest`（全量）或 `pytest tests/test_scanner.py -v`（单文件）。当前基线全绿，任务结束时必须仍全绿。

---

### Task 1: 两个纯函数 `market_age_hours` / `loosest_new_market_hours`

**Files:**
- Modify: `engine/scanner.py`（顶部 import + 模块级函数，加在 `_in_settlement_window` 之后、`class MarketScanner` 之前）
- Test: `tests/test_scanner.py`（文件末尾追加两个测试类）

**Interfaces:**
- Consumes: 无
- Produces:
  - `market_age_hours(created_at: str, now: float) -> float | None`
  - `loosest_new_market_hours(templates: list[dict]) -> float`
  - 测试模块级 helper `_created_hours_ago(hours: float) -> str`（Task 3、Task 4 的测试会复用）

- [ ] **Step 1: 写失败的测试**

在 `tests/test_scanner.py` 顶部把 `from datetime import datetime, timedelta, date` 改成
`from datetime import datetime, timedelta, date, timezone`，把第 7 行的 import 改成：

```python
from engine.scanner import (
    MarketScanner,
    ScanSuperseded,
    market_age_hours,
    loosest_new_market_hours,
)
```

在模块级 helper 区（`_tier` 下方、`_future_date` 上方）加：

```python
def _created_hours_ago(hours: float) -> str:
    """构造「hours 小时前创建」的 created_at（UTC，奖励端点的格式）。"""
    return datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
```

在文件**末尾**追加：

```python
class TestMarketAgeHours:
    """市场创建至今的小时数。created_at 是真正的 UTC 时刻，解析口径与 end_date（日历日）不同。"""

    def _utc(self, y, mo, d, h=0, mi=0, s=0):
        return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()

    def test_six_digit_fraction(self):
        now = self._utc(2026, 7, 23, 23, 10, 3)
        assert market_age_hours("2026-07-22T23:10:03.086269Z", now) == pytest.approx(
            24.0
        )

    def test_two_digit_fraction(self):
        # fromisoformat 只认 3/6 位微秒，实测存在 2 位的样本 -> 必须靠正则丢掉小数秒
        now = self._utc(2026, 7, 23, 23, 10, 3)
        assert market_age_hours("2026-07-22T23:10:03.08Z", now) == pytest.approx(24.0)

    def test_no_fraction(self):
        now = self._utc(2026, 7, 23, 23, 10, 3)
        assert market_age_hours("2026-07-22T23:10:03Z", now) == pytest.approx(24.0)

    def test_missing_or_malformed_returns_none(self):
        now = self._utc(2026, 7, 23)
        assert market_age_hours("", now) is None
        assert market_age_hours(None, now) is None
        assert market_age_hours("not-a-date", now) is None

    def test_parsed_as_utc_not_local(self):
        """按 UTC 解析。套 _parse_end_date（naive 本地还原）会在北京机器上差 8 小时。"""
        now = self._utc(2026, 7, 23, 0, 0, 0)
        assert market_age_hours("2026-07-23T00:00:00Z", now) == pytest.approx(0.0)

    def test_space_separator(self):
        now = self._utc(2026, 7, 23, 0, 0, 0)
        assert market_age_hours("2026-07-22 00:00:00Z", now) == pytest.approx(24.0)


class TestLoosestNewMarketHours:
    """发现阶段是钱包无关的共享阶段，只能用「所有模板都开了」的最宽松门槛排除。"""

    def _t(self, on, hrs):
        return {"skip_new_markets": on, "new_market_hours": hrs}

    def test_all_on_takes_min(self):
        assert loosest_new_market_hours([self._t(True, 48), self._t(True, 24)]) == 24

    def test_any_off_returns_zero(self):
        assert loosest_new_market_hours([self._t(True, 48), self._t(False, 24)]) == 0

    def test_empty_returns_zero(self):
        assert loosest_new_market_hours([]) == 0

    def test_hours_none_treated_as_zero(self):
        assert loosest_new_market_hours([self._t(True, None)]) == 0

    def test_missing_keys_treated_as_off(self):
        assert loosest_new_market_hours([{}]) == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_scanner.py::TestMarketAgeHours tests/test_scanner.py::TestLoosestNewMarketHours -v`
Expected: FAIL，collection 阶段就报 `ImportError: cannot import name 'market_age_hours' from 'engine.scanner'`。

- [ ] **Step 3: 实现两个纯函数**

`engine/scanner.py` 顶部 import 改成：

```python
from datetime import datetime, date, timezone
```

在 `_in_settlement_window` 函数之后、`class MarketScanner` 之前加：

```python
_CREATED_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")


def market_age_hours(created_at: str, now: float):
    """市场创建至今的小时数；created_at 缺失/解析不出返回 None（调用方 fail-open 保留）。

    created_at 是真正的 UTC 时刻（奖励端点给的形如 '2026-07-22T23:10:03.086269Z'），
    **不能**套 _parse_end_date —— 那个是刻意按 naive 本地时区还原的（end_date 的语义是
    「日历日」，一去一回时区抵消），拿来解析这里会平白差一个时区，把「25 小时前创建」
    算成「17 小时前」。小数秒直接丢掉（最多差 1 秒，无意义），顺带绕开
    datetime.fromisoformat 只认 3 位或 6 位微秒的限制（实测存在 2 位的样本）。
    非 Z 结尾的时区偏移不处理、一律按 UTC：实测样本 100% 带 Z，真出现别的格式时正则
    仍匹配得上，误差最大一个时区。
    """
    m = _CREATED_RE.match((created_at or "").strip())
    if not m:
        return None
    ts = datetime(*map(int, m.groups()), tzinfo=timezone.utc).timestamp()
    return (now - ts) / 3600.0


def loosest_new_market_hours(templates) -> float:
    """发现阶段可安全排除的「新市场」门槛（小时）。

    发现阶段是钱包无关的共享阶段，只有**每个**模板都开了 skip_new_markets 才能在这里
    排除（否则会把没开该开关的模板要的市场也一起剔掉）；此时取各模板 N 的最小值（最
    宽松）。任一模板没开 -> 0（不排除任何市场）；空列表 -> 0。
    """
    hours = []
    for t in templates:
        if not t.get("skip_new_markets"):
            return 0.0
        hours.append(float(t.get("new_market_hours") or 0))
    return min(hours) if hours else 0.0
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_scanner.py::TestMarketAgeHours tests/test_scanner.py::TestLoosestNewMarketHours -v`
Expected: PASS，11 项全绿。

- [ ] **Step 5: 提交**

```bash
git diff --stat
git add engine/scanner.py tests/test_scanner.py
git commit -m "feat(scanner): market_age_hours / loosest_new_market_hours 纯函数"
```

---

### Task 2: 两个模板级配置键 + `_active_templates` 去重键

**Files:**
- Modify: `config.py`（`TEMPLATE_DEFAULTS`，加在 `max_settlement_days` 之后）
- Modify: `engine/manager.py:983-990`（`_active_templates` 的 key 元组）
- Test: `tests/test_database.py`（追加契约测试）、`tests/test_manager.py`（`TestActiveTemplatesDedupKey` 类内追加）

**Interfaces:**
- Consumes: 无
- Produces: 模板字典的两个键 `skip_new_markets`（bool，默认 `False`）、`new_market_hours`（float，默认 `24.0`）；Task 3/4/5 都读它们。

- [ ] **Step 1: 写失败的测试**

`tests/test_database.py` 文件末尾追加：

```python
def test_skip_new_markets_defaults():
    """跳过新建市场：默认关闭（升级零行为变化），保护期默认 24 小时。"""
    from config import TEMPLATE_DEFAULTS

    assert TEMPLATE_DEFAULTS["skip_new_markets"] is False
    assert TEMPLATE_DEFAULTS["new_market_hours"] == 24.0
```

`tests/test_manager.py` 的 `TestActiveTemplatesDedupKey` 类内（`test_identical_templates_still_deduped` 之前）追加：

```python
    def test_skip_new_markets_variants_not_deduped(self):
        def tmpl_for(addr):
            return self._base(skip_new_markets=(addr == "0xA"))

        assert len(self._mgr_with(tmpl_for)._active_templates()) == 2

    def test_new_market_hours_variants_not_deduped(self):
        def tmpl_for(addr):
            return self._base(
                skip_new_markets=True,
                new_market_hours=24 if addr == "0xA" else 72,
            )

        assert len(self._mgr_with(tmpl_for)._active_templates()) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_database.py::test_skip_new_markets_defaults "tests/test_manager.py::TestActiveTemplatesDedupKey" -v`
Expected: FAIL。`test_skip_new_markets_defaults` 报 `KeyError: 'skip_new_markets'`；两个去重测试报 `assert 1 == 2`（两个模板被去重成了一个）。

- [ ] **Step 3: 加配置键**

`config.py` 的 `TEMPLATE_DEFAULTS` 里，`"max_settlement_days": None,` 那一项之后插入：

```python
    # 跳过新建市场:创建不足 new_market_hours 小时的市场不做。默认关(升级零行为变化);
    # new_market_hours=0 视同不筛。判定在 scanner 的发现阶段与 prefilter 各做一次。
    "skip_new_markets": False,
    "new_market_hours": 24.0,
```

- [ ] **Step 4: 扩去重键**

`engine/manager.py` 的 `_active_templates`，把 key 元组改成：

```python
            key = (
                tuple(sorted(tmpl.get("included_categories", []) or [])),
                bool(tmpl.get("include_other", False)),
                tmpl.get("min_reward_usd", 0),
                tmpl.get("min_settlement_days"),
                tmpl.get("max_settlement_days"),
                tuple(sorted(enabled_sizes(tmpl.get("size_tiers") or []))),
                bool(tmpl.get("skip_new_markets", False)),
                tmpl.get("new_market_hours"),
            )
```

并把该方法上方注释的末句补成（发现阶段现在多读了两个维度）：

```python
            # 去重键须含采集器实际用到的每个维度:品类包含集 + 是否含其他 + 奖励下限
            # (决定预筛 min_floor) + 结算窗口 + 档位 sizes + 新市场开关/小时数(后三者
            # 决定发现阶段的并集门控:窗口/档位/新市场门槛不同的模板不能被去重成一个,
            # 否则另一个的门槛没进并集就会误剔)。
```

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_database.py tests/test_manager.py -v`
Expected: PASS，全绿（新增 3 项 + 原有用例不受影响，`test_identical_templates_still_deduped` 仍为 1）。

- [ ] **Step 6: 提交**

```bash
git diff --stat
git add config.py engine/manager.py tests/test_database.py tests/test_manager.py
git commit -m "feat(config): skip_new_markets/new_market_hours 模板键 + 扩发现阶段去重键"
```

---

### Task 3: 发现阶段按最宽松门槛排除新市场

**Files:**
- Modify: `engine/scanner.py`（`MarketScanner.discover_candidates`）
- Test: `tests/test_scanner.py`（末尾追加一个测试类）

**Interfaces:**
- Consumes: Task 1 的 `loosest_new_market_hours(templates)` 与 `market_age_hours(created_at, now)`；Task 1 的测试 helper `_created_hours_ago(hours)`；Task 2 的两个模板键。
- Produces: 被排除的市场不进候选池，因此也不写 `eligible_markets`、不抓订单簿、市场发现页不显示。

- [ ] **Step 1: 写失败的测试**

`tests/test_scanner.py` 末尾追加：

```python
class TestDiscoverySkipNewMarkets:
    """发现阶段跳过新建市场：模板全开时按最宽松门槛排除；任一模板没开则一个都不排。

    发现阶段是钱包无关的共享阶段，排早了会把别的模板要的市场也剔掉（与奖励地板用
    min_floor 兜底同一模式）。created_at 由奖励端点白拿，判定不发任何网络请求。
    """

    def _api(self, markets):
        api = MagicMock()

        def fake_rewards(tag_slug=None, **kw):
            return list(markets) if tag_slug is None else []

        api.get_rewards_markets.side_effect = fake_rewards
        api.get_rewards_for_market.return_value = []
        return api

    def _mkt(self, cid, age_hours):
        return {
            "condition_id": cid,
            "question": cid,
            "tokens": [{"token_id": cid + "-y"}],
            "rewards_config": [{"rate_per_day": 50}],
            "rewards_min_size": 100,
            "end_date": (date.today() + timedelta(days=10)).strftime("%Y-%m-%d"),
            "created_at": _created_hours_ago(age_hours),
        }

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
        }
        t.update(over)
        return t

    def _db(self):
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        return db

    def _pool_ids(self, api, templates):
        sc = MarketScanner(api, self._db(), "")
        return {m["condition_id"] for m in sc.discover_candidates(templates)}

    def test_new_market_excluded_from_pool(self):
        api = self._api([self._mkt("NEW", 5), self._mkt("OLD", 100)])
        assert self._pool_ids(api, [self._tmpl()]) == {"OLD"}

    def test_exactly_at_threshold_kept(self):
        # 门槛是「不足 N 小时才跳」，刚满 N 小时要留下
        api = self._api([self._mkt("AT", 24.01)])
        assert self._pool_ids(api, [self._tmpl()]) == {"AT"}

    def test_any_template_off_keeps_new_market(self):
        api = self._api([self._mkt("NEW", 5), self._mkt("OLD", 100)])
        tmpls = [self._tmpl(), self._tmpl(skip_new_markets=False)]
        assert self._pool_ids(api, tmpls) == {"NEW", "OLD"}

    def test_loosest_hours_used(self):
        # A 要 24h、B 要 72h -> 共享阶段只能按 24h 排，48h 龄的市场得留给 A
        api = self._api([self._mkt("M48", 48)])
        tmpls = [self._tmpl(new_market_hours=24), self._tmpl(new_market_hours=72)]
        assert self._pool_ids(api, tmpls) == {"M48"}

    def test_missing_created_at_kept(self):
        m = self._mkt("A", 1)
        del m["created_at"]
        assert self._pool_ids(self._api([m]), [self._tmpl()]) == {"A"}

    def test_switch_off_keeps_everything(self):
        api = self._api([self._mkt("NEW", 1)])
        assert self._pool_ids(api, [self._tmpl(skip_new_markets=False)]) == {"NEW"}

    def test_zero_hours_keeps_everything(self):
        api = self._api([self._mkt("NEW", 0.1)])
        assert self._pool_ids(api, [self._tmpl(new_market_hours=0)]) == {"NEW"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_scanner.py::TestDiscoverySkipNewMarkets -v`
Expected: FAIL，`test_new_market_excluded_from_pool` 报 `assert {'NEW', 'OLD'} == {'OLD'}`（新市场还在池里）；其余用例此时已经 PASS（它们断言的是「保留」）。

- [ ] **Step 3: 接进 `discover_candidates`**

在 `blacklist = self.db.get_blacklist_ids()` 那一行下面加：

```python
        # 「新市场」门槛:发现阶段只能用最宽松值(全模板都开才生效),各模板自己的 N 由
        # prefilter_for_template 精筛。created_at 由奖励端点白拿,判定不发网络请求。
        min_age_hours = loosest_new_market_hours(templates)
        now = time.time()
```

在候选池循环里，`if _batch_rate(market) < min_floor: continue` 之后、
`(priced if _should_price(market) else extra).append(market)` 之前插入：

```python
            if min_age_hours:
                age = market_age_hours(market.get("created_at", ""), now)
                if age is not None and age < min_age_hours:
                    continue  # 太新;created_at 取不到 -> fail-open 保留
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_scanner.py -v`
Expected: PASS，整个 test_scanner.py 全绿（新增 7 项，既有发现阶段用例的模板不含开关键，`loosest_new_market_hours` 返回 0，行为不变）。

- [ ] **Step 5: 提交**

```bash
git diff --stat
git add engine/scanner.py tests/test_scanner.py
git commit -m "feat(scanner): 发现阶段按最宽松门槛排除新建市场"
```

---

### Task 4: `prefilter_for_template` 按各模板自己的 N 精筛

**Files:**
- Modify: `engine/scanner.py`（`MarketScanner.prefilter_for_template`）
- Test: `tests/test_scanner.py`（`TestPrefilterForTemplate` 类内追加）

**Interfaces:**
- Consumes: Task 1 的 `market_age_hours` 与 `_created_hours_ago`；Task 2 的两个模板键。
- Produces: 多模板时各模板按自己的 N 精筛；`filter_for_template` 复用 `prefilter_for_template`，自动跟着生效。

- [ ] **Step 1: 写失败的测试**

`tests/test_scanner.py` 的 `TestPrefilterForTemplate` 类内，
`test_drops_market_with_no_token_in_price_band` 之后追加：

```python
    def _aged(self, cid, hours, **over):
        m = self._candidate(cid, **over)
        m["created_at"] = _created_hours_ago(hours)
        return m

    def test_drops_new_market(self):
        scanner = self._scanner()
        pool = [self._aged("NEW", 5), self._aged("OLD", 100)]
        tmpl = self._template(skip_new_markets=True, new_market_hours=24)
        assert self._ids(scanner, pool, tmpl) == {"OLD"}

    def test_keeps_new_market_when_switch_off(self):
        scanner = self._scanner()
        pool = [self._aged("NEW", 5)]
        tmpl = self._template(skip_new_markets=False, new_market_hours=24)
        assert self._ids(scanner, pool, tmpl) == {"NEW"}

    def test_keeps_new_market_when_hours_zero(self):
        scanner = self._scanner()
        pool = [self._aged("NEW", 0.1)]
        tmpl = self._template(skip_new_markets=True, new_market_hours=0)
        assert self._ids(scanner, pool, tmpl) == {"NEW"}

    def test_keeps_market_without_created_at(self):
        # fail-open:created_at 取不到就保留（与结算日解析不出即保留同口径）
        scanner = self._scanner()
        tmpl = self._template(skip_new_markets=True, new_market_hours=24)
        assert self._ids(scanner, [self._candidate("A")], tmpl) == {"A"}

    def test_each_template_uses_own_hours(self):
        scanner = self._scanner()
        pool = [self._aged("M48", 48)]
        strict = self._template(skip_new_markets=True, new_market_hours=72)
        loose = self._template(skip_new_markets=True, new_market_hours=24)
        assert self._ids(scanner, pool, strict) == set()
        assert self._ids(scanner, pool, loose) == {"M48"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_scanner.py::TestPrefilterForTemplate -v`
Expected: FAIL，`test_drops_new_market` 报 `assert {'NEW', 'OLD'} == {'OLD'}`，`test_each_template_uses_own_hours` 报 `assert {'M48'} == set()`。

- [ ] **Step 3: 接进 `prefilter_for_template`**

在方法开头的参数解包区（`tier_sizes = enabled_sizes(...)` 那一行之后）加：

```python
        skip_new = bool(template.get("skip_new_markets"))
        new_hours = float(template.get("new_market_hours") or 0)
        now = time.time()
```

在市场循环里，结算窗口那段之后、冷却检查之前插入：

```python
            if skip_new and new_hours:
                # 创建不足 new_hours 小时的市场不做;created_at 取不到 -> fail-open 保留。
                age = market_age_hours(market.get("created_at", ""), now)
                if age is not None and age < new_hours:
                    continue
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_scanner.py -v`
Expected: PASS，全绿（新增 5 项）。

- [ ] **Step 5: 提交**

```bash
git diff --stat
git add engine/scanner.py tests/test_scanner.py
git commit -m "feat(scanner): prefilter 按模板自身门槛跳过新建市场"
```

---

### Task 5: 配置页表单（开关 + 小时数）

**Files:**
- Modify: `web/templates/config.html`（策略参数 form-grid、`loadStrategy` 回填、`strategy-form` submit 收值）

**Interfaces:**
- Consumes: Task 2 的两个模板键；`/api/templates/<id>` 的 GET/PUT 已按 `TEMPLATE_DEFAULTS` 白名单自动存取，**后端不需要任何改动**。
- Produces: 用户可在配置页勾选开关并填小时数。

**配置链路的坑（务必三处都改）：** 白名单只负责过滤，不会替你读 DOM。
`input[type=number][name]` 是自动收的，checkbox 既不会被自动收（submit 处），
也不会被自动回填（`loadStrategy` 里 `input.value = data[key]` 对 checkbox 无效，
只会设 value 属性、不动 checked）。

- [ ] **Step 1: 加表单字段**

在「最长结算天数」那个 `form-group`（`config.html:77-80`）之后插入：

```html
            <div class="form-group">
                <label><input type="checkbox" id="skip-new-markets"> 跳过新建市场</label>
                <span class="hint">勾选后，创建时间不足下方小时数的市场不做市</span>
            </div>
            <div class="form-group">
                <label>新市场保护期（小时）</label>
                <input type="number" name="new_market_hours" step="1" min="0">
                <span class="hint">市场创建满这么多小时之后才做；0=不筛。市场发现每 4 小时一轮，实际生效可能晚最多 4 小时</span>
            </div>
```

- [ ] **Step 2: 加回填**

`loadStrategy` 里 `updateStopMode();` 那一行之前插入：

```javascript
        // checkbox 不能靠上面的 input.value 回填（那只会设 value 属性，不动 checked）
        const skipNew = document.getElementById('skip-new-markets');
        if (skipNew) skipNew.checked = !!data.skip_new_markets;
```

- [ ] **Step 3: 加收值**

`strategy-form` 的 submit 里，`data.liquidate_target_mode = ...` 那一行之后插入：

```javascript
    // checkbox 同 select，不在 input[type=number] 里，单独收。
    data.skip_new_markets = !!(document.getElementById('skip-new-markets') || {}).checked;
    // 小时数留空 -> parseFloat('')=NaN，归 0（= 不筛），别让 NaN 变成 JSON null。
    if (isNaN(data.new_market_hours)) data.new_market_hours = 0;
```

- [ ] **Step 4: 确认文件没被写坏**

Run: `python -c "p=r'web/templates/config.html'; b=open(p,'rb').read(); print('BOM' if b[:3]==b'\xef\xbb\xbf' else 'no BOM'); print(b.decode('utf-8')[:0] or 'utf-8 ok')"`
Expected: 输出 `no BOM` 和 `utf-8 ok`（有 BOM 或解码报错就是文件被写坏了，回滚重写）。

- [ ] **Step 5: 手工验证**

Run: `python app.py`，登录后打开配置页：
1. 「策略参数」区能看到「跳过新建市场」勾选框和「新市场保护期（小时）」输入框，默认未勾选、值 24。
2. 勾上、把小时数改成 48、保存，弹出「策略参数已保存」。
3. 刷新页面，勾选状态和 48 都还在（回填生效）。
4. 取消勾选、保存、刷新，确认变回未勾选。

Expected: 四步全部符合。第 3 步失败 = 回填没接上；保存后值丢失 = 收值没接上。

- [ ] **Step 6: 提交**

```bash
git add web/templates/config.html
git commit -m "feat(config-ui): 跳过新建市场开关 + 保护期小时数"
```

---

### Task 6: 台账起点前移 + 文档

**Files:**
- Modify: `engine/manager.py:30`（`PNL_START_DATE`）
- Modify: `README.md:184` 附近（策略参数表）
- Modify: `CLAUDE.md`（Architecture 段里 scanner 的筛选条件那句）
- Test: `tests/test_manager.py`（追加契约测试）

**Interfaces:**
- Consumes: 无（`PNL_START_DATE` 已被 `manager.py:168/909/921/938` 四处引用同一常量，改常量即可）
- Produces: 台账从 2026-05-17 开始补漏。

- [ ] **Step 1: 写失败的测试**

`tests/test_manager.py` 末尾追加：

```python
def test_pnl_start_date_is_project_start():
    """台账补漏起点 = 项目首次提交日（27cc9bc，2026-05-17），不是当初的 2026-07-01。

    rebuild_wallet_pnl 本来就拉全量 activity/trades、不带时间过滤，_date_range 只决定
    往 daily_pnl 写哪些天 —— 前移没有网络代价，只是多写几十行本地记录。
    """
    from engine.manager import PNL_START_DATE

    assert PNL_START_DATE == "2026-05-17"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_manager.py::test_pnl_start_date_is_project_start -v`
Expected: FAIL，`assert '2026-07-01' == '2026-05-17'`。

- [ ] **Step 3: 改常量**

`engine/manager.py:29-30` 改成：

```python
# 盈亏台账补漏起点(北京日)。启动/重启后从这天补到今天;每日跨天再重算。
# = 项目首次提交日(27cc9bc, 2026-05-17):全量 activity/trades 本来就全拉,前移只是多写
# 几十行本地记录,没有网络代价。
PNL_START_DATE = "2026-05-17"
```

同时把 `WalletWorker.__init__` 里 `self._last_pnl_date` 上方注释（`manager.py:70-72`）
和 `_maybe_rebuild_pnl` 的 docstring（`manager.py:143`）里写死的 `2026-07-01` 改成 `2026-05-17`。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_manager.py -v`
Expected: PASS，全绿。

- [ ] **Step 5: 更新 README 参数表**

`README.md` 的策略参数表里，`min_settlement_days / max_settlement_days` 那一行之后插入：

```markdown
| `skip_new_markets` / `new_market_hours` | `false` / 24.0 | 跳过最近 N 小时内创建的新市场（默认关；0 小时 = 不筛）。判定在市场发现阶段，实际生效可能比 N 晚最多 4 小时（发现轮间隔） |
```

- [ ] **Step 6: 更新 CLAUDE.md**

`CLAUDE.md` 的 Architecture 段里，描述 `engine/scanner.py` 过滤条件的那句
（"filters reward markets (reward ≥ threshold, settlement days, price band, spread, cooldown, and an exact size-tier match…"）
把过滤项列表改成：

```
filters reward markets (reward ≥ threshold, settlement days, price band, spread, cooldown,
an optional market-age floor, and an exact size-tier match: …)
```

并在同段末尾追加一句：

```
The optional market-age floor (`skip_new_markets` / `new_market_hours`, template-level,
default off) skips markets created within the last N hours, using the `created_at` the
CLOB rewards endpoint already returns (no extra request). It is judged twice: once in the
shared `discover_candidates` using the loosest threshold (only when *every* template has
the switch on, taking the smallest N — otherwise a template that wants those markets
would lose them), and once per template in `prefilter_for_template`. Both fail open when
`created_at` is missing or unparseable. Because the market never re-enters the pool until
a later discovery round (4 h apart), the effective protection window is N to N+4 hours,
and turning the switch on cancels resting buys in now-excluded markets via the usual
dropout pass.
```

- [ ] **Step 7: 跑全量测试**

Run: `pytest`
Expected: PASS，全绿（基线 + 本次新增约 26 项）。

- [ ] **Step 8: 提交**

```bash
git diff --stat
git add engine/manager.py tests/test_manager.py README.md CLAUDE.md
git commit -m "feat(pnl): 台账起点前移到 2026-05-17 + 文档补新市场门槛"
```

---

## 收尾（不属于任何 Task，由主会话决定）

- 版本号与发版：本次是**行为可改变**的新功能（开关默认关，但开启后会撤已挂新市场买单），
  按 `docs/版本号规范.md` 属于次版本（向后兼容的新功能，默认关 = 升级零行为变化）。
  发版走 `version.py` + `release.ps1`，由用户决定是否现在发。
- 合并方式（直接合 main / PR / 保留分支）由用户选，不要自行合并。

## Self-Review

**1. Spec coverage**

| spec 条目 | 落点 |
|---|---|
| 1.2 `created_at` 白拿、零额外网络 | Task 1（解析）+ Task 3/4（判定不发请求，测试用 MagicMock 断言池内容） |
| 1.3 两个模板键 + 前端单独收 checkbox | Task 2（键）+ Task 5（三处前端改动） |
| 1.4 `market_age_hours` 不复用 `_parse_end_date`、按 UTC、丢小数秒 | Task 1 Step 3 + `test_parsed_as_utc_not_local` / `test_two_digit_fraction` |
| 1.5 两处判定 + `loosest_new_market_hours` + N=0 视同不筛 | Task 1、Task 3、Task 4；`test_zero_hours_keeps_everything` / `test_keeps_new_market_when_hours_zero` |
| 1.5 两处 fail-open | `test_missing_created_at_kept` / `test_keeps_market_without_created_at` |
| 1.6 扩 `_active_templates` 去重键 | Task 2 Step 4 + 两个去重测试 |
| 1.7 两个副作用 | 无代码改动，写进 README / CLAUDE.md（Task 6 Step 5/6）与配置页 hint（Task 5） |
| 二、台账起点 | Task 6 Step 3 + 契约测试 |
| 三、答疑记录 | spec 已记录，无代码改动 |
| 四、测试清单 | Task 1/2/3/4/6 的测试步骤逐条覆盖 |
| 五、不改的东西 | 计划未触碰 monitor / take_profit / laddering |

**2. Placeholder scan** — 无 TBD/TODO；每个代码步骤都给了可直接粘贴的完整代码块；前端无自动化测试，故 Task 5 给了可执行的手工验证清单而非「自行验证」。

**3. Type consistency** — `market_age_hours(created_at, now)` 在 Task 1 定义、Task 3/4 调用，参数顺序与名称一致；`loosest_new_market_hours(templates)` 只在 Task 3 调用；测试 helper `_created_hours_ago(hours)` 在 Task 1 定义、Task 3/4 复用（已在各 Interfaces 块声明）；模板键名 `skip_new_markets` / `new_market_hours` 在 config.py、manager.py、scanner.py 两处、config.html 三处拼写一致。
