# 按品类分页采集 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让候选池由「各需要品类的 tag 抓取结果」并集组成(而非全品类前 500 交集),修复天气等低奖励品类候选偏少;每品类抓取页数上限可调、默认 20。

**Architecture:** `scanner.discover_candidates` 不再拿不带品类的 `full`(全品类前 N 页)去 `tag_pool` 交集;改为把每个需要品类的 tag 抓取记录本身按 cid 并入候选池,`full` 仅在 `include_other=True` 时抓取(补「无 curated 标签」的市场)。页数上限提为扫描级全局设置 `reward_scan_max_pages`,由 `manager` 读设置后透传给 scanner。

**Tech Stack:** Python 3、pytest、Flask、SQLite、`unittest.mock.MagicMock`。

## Global Constraints

- 每页固定 `page_size=100`;`reward_scan_max_pages` 默认 **20**(= 2000/品类)。
- 设置属**扫描级全局**,存 `settings` 表 → 加进 `config.ENGINE_DEFAULTS`;`/api/settings` 路由按 `ENGINE_DEFAULTS` 白名单自动收键,**routes.py 不改**。
- `scanner` **不读** `db.get_settings()`(现有 scanner 测试用 MagicMock db);由 `manager` 读设置后以 `max_pages=` 透传。
- `get_rewards_markets` 自身 `max_pages=5` 默认**不动**(其它调用方不受影响)。
- UI 字符串为简体中文;含中文的 `config.html` 由主 agent 直接编辑,写后校验无 BOM、中文无别字。
- 不动 `filter_for_template`、离场、下单等下游逻辑。
- 每个任务结束跑 `pytest` 全绿(现有 545 用例 + 新增)。

---

### Task 1: 设置键 `reward_scan_max_pages`(+ /api/settings 契约)

**Files:**
- Modify: `config.py:33-39`(`ENGINE_DEFAULTS`)
- Test: `tests/test_settings_routes.py`

**Interfaces:**
- Produces: `config.ENGINE_DEFAULTS["reward_scan_max_pages"] == 20`;`db.get_settings()["reward_scan_max_pages"]` 可读(`database.py:244` `dict(ENGINE_DEFAULTS)` 打底);`/api/settings` GET 返回该键、POST 可存。

- [ ] **Step 1: 写失败契约测试**

在 `tests/test_settings_routes.py` 末尾追加:

```python
def test_reward_scan_max_pages_default_and_roundtrip(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    # 默认值出现在 GET
    assert client.get("/api/settings").get_json()["reward_scan_max_pages"] == 20
    # POST 存 + 回读
    resp = client.post("/api/settings", json={"reward_scan_max_pages": 12})
    assert resp.status_code == 200
    assert db.get_settings()["reward_scan_max_pages"] == 12
    # 属引擎键,不落默认模板
    assert "reward_scan_max_pages" not in db.get_template(db.get_default_template_id())
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `pytest tests/test_settings_routes.py::test_reward_scan_max_pages_default_and_roundtrip -v`
Expected: FAIL —— `KeyError: 'reward_scan_max_pages'`(GET 结果里没这个键)。

- [ ] **Step 3: 加设置键**

`config.py` 的 `ENGINE_DEFAULTS`(当前 `config.py:33-39`)加最后一行:

```python
ENGINE_DEFAULTS = {
    "scan_interval_sec": 30,
    "fill_check_interval_sec": 5,
    "cooldown_minutes": 20,
    "rewards_cache_ttl_sec": 600,
    "discovery_interval_sec": 14400,
    # 每品类奖励抓取页数上限(每页 100)。默认 20=2000/品类,覆盖天气等大品类;
    # 扫描级全局,由 manager 透传给 scanner。
    "reward_scan_max_pages": 20,
}
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `pytest tests/test_settings_routes.py -v`
Expected: PASS(新用例 + 原有全绿)。

- [ ] **Step 5: 提交**

```bash
git add config.py tests/test_settings_routes.py
git commit -m "feat(settings): 加 reward_scan_max_pages(默认20)扫描级全局键"
```

---

### Task 2: scanner 候选池改为各品类 tag 并集(核心修复)

**Files:**
- Modify: `engine/scanner.py`(import 行 `32`;`discover_candidates` `112-240`;`fetch_candidates` `256-271`)
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `config.ENGINE_DEFAULTS["reward_scan_max_pages"]`(Task 1)。
- Produces:
  - `discover_candidates(self, templates, on_progress=None, on_found=None, cancel=None, max_pages=ENGINE_DEFAULTS["reward_scan_max_pages"])`
  - `fetch_candidates(self, templates, on_progress=None, on_found=None, skip_orderbook=False, cancel=None, max_pages=ENGINE_DEFAULTS["reward_scan_max_pages"])`
  - 候选池 = 各 `slugs_needed` 品类 `get_rewards_markets(tag_slug=…)` 记录按 `condition_id` 去重的并集;`include_other=True` 时再并入 `get_rewards_markets()`(不带品类)的记录。所有 `get_rewards_markets` 调用带 `max_pages=`。

- [ ] **Step 1: 写失败测试(bug 复现 + dedup + 不抓 full + 透传)**

在 `tests/test_scanner.py` 的 `class TestFetchCandidatesCategoryWiring` 末尾追加四个用例:

```python
    def test_tag_market_not_in_full_still_included(self):
        # W 只在 weather tag 查询里,不在(不带品类的)full —— 修前被 tag_pool 丢掉。
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [{"condition_id": "HI", "tokens": [], "rewards_config": []}]
            if tag_slug == "weather":
                return [{"condition_id": "W", "tokens": [], "rewards_config": []}]
            return []

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        pool = scanner.fetch_candidates(
            [{"included_categories": ["weather"], "include_other": False,
              "min_reward_usd": 0}],
            skip_orderbook=True,
        )
        assert {m["condition_id"] for m in pool} == {"W"}

    def test_no_untagged_full_fetch_when_not_include_other(self):
        # include_other=False 不应触发不带品类的 full 抓取。
        def fake_rewards(tag_slug=None, **kw):
            return ([{"condition_id": "W", "tokens": [], "rewards_config": []}]
                    if tag_slug == "weather" else [])

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        scanner.fetch_candidates(
            [{"included_categories": ["weather"], "include_other": False,
              "min_reward_usd": 0}],
            skip_orderbook=True,
        )
        assert all(
            c.kwargs.get("tag_slug") is not None
            for c in api.get_rewards_markets.call_args_list
        ), "include_other=False 不应调用不带品类的 get_rewards_markets"

    def test_same_market_in_two_slugs_deduped(self):
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug in ("weather", "politics"):
                return [{"condition_id": "X", "tokens": [], "rewards_config": []}]
            return []

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        pool = scanner.fetch_candidates(
            [{"included_categories": ["weather", "politics"],
              "include_other": False, "min_reward_usd": 0}],
            skip_orderbook=True,
        )
        assert [m["condition_id"] for m in pool] == ["X"]

    def test_max_pages_threaded_to_every_rewards_call(self):
        def fake_rewards(tag_slug=None, max_pages=5, **kw):
            return ([{"condition_id": "W", "tokens": [], "rewards_config": []}]
                    if tag_slug == "weather" else [])

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        scanner.fetch_candidates(
            [{"included_categories": ["weather"], "include_other": False,
              "min_reward_usd": 0}],
            skip_orderbook=True, max_pages=17,
        )
        assert api.get_rewards_markets.call_args_list  # 至少调用过一次
        for c in api.get_rewards_markets.call_args_list:
            assert c.kwargs.get("max_pages") == 17
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `pytest tests/test_scanner.py::TestFetchCandidatesCategoryWiring -v`
Expected: 新四例 FAIL —— `test_tag_market_not_in_full_still_included` 得到空集(W 被丢);`test_no_untagged_full_fetch...` 因当前无条件抓 full 而断言失败;`test_max_pages_threaded...` 因当前调用不带 `max_pages` 而失败。

- [ ] **Step 3: 改 import(引入 ENGINE_DEFAULTS)**

`engine/scanner.py:32` 现为:

```python
from config import CATALOG_SLUGS, CATEGORY_CATALOG
```

改为:

```python
from config import CATALOG_SLUGS, CATEGORY_CATALOG, ENGINE_DEFAULTS
```

- [ ] **Step 4: 重写 `discover_candidates` 头部(签名 + 池构建)**

`engine/scanner.py` 的 `discover_candidates`:签名加 `max_pages`,并把 `112-155`(从 `def discover_candidates` 到 `blacklist = self.db.get_blacklist_ids()` 之前)整段替换。

替换**签名**(当前 `112-114`):

```python
    def discover_candidates(
        self,
        templates,
        on_progress=None,
        on_found=None,
        cancel=None,
        max_pages=ENGINE_DEFAULTS["reward_scan_max_pages"],
    ) -> list[dict]:
```

替换**方法体开头到 `blacklist = ...` 之前**(当前 `115-151`)为:

```python
        """共享发现:抓各需要品类的奖励市场并集(不勾「其他」不抓全品类),打 tags,
        补精确奖励。钱包无关、网络密集(rewards 端点)、不抓订单簿、不算价。"""
        union = included_union(templates)
        inc_other = any_include_other(templates)
        # 只查用得上的品类:收「其他」时判其他绕不开、必须查全 14;否则只需 included 并集。
        slugs_needed = set(CATALOG_SLUGS) if inc_other else (union & set(CATALOG_SLUGS))
        floors = [t.get("min_reward_usd", 0) for t in templates]
        min_floor = min(floors) if floors else 0

        def _tag_slug(slug):  # 返回整条记录(用于并入候选池 + 建 cid 集打标签)
            try:
                rows = self.api.get_rewards_markets(tag_slug=slug, max_pages=max_pages)
                return slug, rows
            except Exception as e:
                # 单个品类查询失败不拖垮整轮发现(奖励端点偶发 500):该 slug 记空,
                # 其命中市场退化为「其他」(include_other 时仍会被 full 收)。
                logger.warning(
                    "Discovery tag_slug %s failed (treated as empty): %s", slug, e
                )
                return slug, []

        # 只查 slugs_needed 各一次奖励端点,并发拉;每 slug 保留整条记录。
        slug_rows = dict(_parallel_map(_tag_slug, slugs_needed))
        category_ids = {
            slug: {m.get("condition_id", "") for m in rows}
            for slug, rows in slug_rows.items()
        }

        # full(不带品类=全品类)仅在有号收「其他」时需要:用来捞无 curated 标签的市场。
        # 不勾「其他」的号(如天气号)彻底跳过这次全品类抓取——正是它当初把低奖励品类
        # 挤出前 500、造成候选偏少的根因。
        full = (
            self.api.get_rewards_markets(max_pages=max_pages) if inc_other else []
        )

        # 品类计数快照(配置页勾选用):仅当查了全 14(即 inc_other,full 有值)才算得准。
        if slugs_needed == set(CATALOG_SLUGS):
            self.last_catalog = self._catalog_payload(full, category_ids)

        # 候选池 = 各需要品类 tag 记录按 cid 去重的并集;inc_other 再并入 full(补「其他」)。
        by_cid = {}
        for slug in slugs_needed:
            for market in slug_rows.get(slug, []):
                cid = market.get("condition_id", "")
                if cid and cid not in by_cid:
                    by_cid[cid] = market
        if inc_other:
            for market in full:
                cid = market.get("condition_id", "")
                if cid and cid not in by_cid:
                    by_cid[cid] = market

        pool = tag_pool(list(by_cid.values()), category_ids, slugs_needed)
        blacklist = self.db.get_blacklist_ids()
```

> `115-151` 之后的部分(`wanted` 粗筛循环、`_precise_reward`、并发精确奖励 + `on_found`、`return out`)**保持不变**。它们以 `pool` / `blacklist` / `union` / `inc_other` / `min_floor` 为输入,变量名都在上面新代码里保留。

- [ ] **Step 5: 给 `fetch_candidates` 加 `max_pages` 透传**

`engine/scanner.py` 的 `fetch_candidates`(当前 `256-271`)签名与调用改为:

```python
    def fetch_candidates(
        self,
        templates,
        on_progress=None,
        on_found=None,
        skip_orderbook=False,
        cancel=None,
        max_pages=ENGINE_DEFAULTS["reward_scan_max_pages"],
    ) -> list[dict]:
        """共享采集 = 发现 + (除非 skip_orderbook)刷新订单簿。手动扫描/单测用。
        cancel(): 返回 True 时在最近的检查点抛 ScanSuperseded 让位(手动重扫接管)。"""
        pool = self.discover_candidates(
            templates,
            on_progress=on_progress,
            on_found=on_found,
            cancel=cancel,
            max_pages=max_pages,
        )
        if not skip_orderbook:
            self.refresh_orderbooks(pool, cancel=cancel)
        return pool
```

- [ ] **Step 6: 跑 scanner 全测,确认通过(含原有回归)**

Run: `pytest tests/test_scanner.py -v`
Expected: PASS。新四例过;原有 `test_include_other_keeps_untagged`(inc_other=True 仍靠 full 收无标签市场)、`test_slug_query_failure_tolerated`、`test_catalog_payload_intersects_with_full` 等**不变即绿**。

- [ ] **Step 7: 提交**

```bash
git add engine/scanner.py tests/test_scanner.py
git commit -m "fix(scanner): 候选池改由各品类tag并集组成,修低奖励品类候选偏少"
```

---

### Task 3: `category_counts` 用同一可调上限

**Files:**
- Modify: `engine/scanner.py`(`category_counts` `285-317`)
- Test: `tests/test_scanner.py`(`class TestCategoryCounts`)

**Interfaces:**
- Produces: `category_counts(self, catalog, max_pages=ENGINE_DEFAULTS["reward_scan_max_pages"])`;其内 `full` 与逐 slug 的 `get_rewards_markets` 均带 `max_pages=`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_scanner.py` 的 `class TestCategoryCounts` 末尾追加:

```python
    def test_category_counts_threads_max_pages(self):
        def fake_rewards(tag_slug=None, max_pages=5, **kw):
            return [{"condition_id": "A"}]

        api = MagicMock()
        api.get_rewards_markets.side_effect = fake_rewards
        scanner = MarketScanner(api, MagicMock(), "")
        scanner.category_counts([{"slug": "weather", "label": "天气"}], max_pages=13)
        assert api.get_rewards_markets.call_args_list
        for c in api.get_rewards_markets.call_args_list:
            assert c.kwargs.get("max_pages") == 13
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `pytest tests/test_scanner.py::TestCategoryCounts::test_category_counts_threads_max_pages -v`
Expected: FAIL —— 当前 `full = self.api.get_rewards_markets()` 不带 `max_pages`,断言 `== 13` 失败。

- [ ] **Step 3: 改 `category_counts`**

`engine/scanner.py` 的 `category_counts`,签名加 `max_pages`,两处 `get_rewards_markets` 加 `max_pages=`:

签名(当前 `285`):

```python
    def category_counts(
        self, catalog, max_pages=ENGINE_DEFAULTS["reward_scan_max_pages"]
    ) -> dict:
```

`full` 抓取(当前 `288`):

```python
        full = self.api.get_rewards_markets(max_pages=max_pages)
```

`_slug_ids` 内(当前 `296-298`):

```python
        def _slug_ids(slug):
            with use_proxy(proxy):
                rows = self.api.get_rewards_markets(tag_slug=slug, max_pages=max_pages)
            return {m.get("condition_id", "") for m in rows} & full_ids
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `pytest tests/test_scanner.py::TestCategoryCounts -v`
Expected: PASS(新用例 + 原有 `test_counts_and_other`、`test_parallel_slug_calls_carry_proxy` 全绿)。

- [ ] **Step 5: 提交**

```bash
git add engine/scanner.py tests/test_scanner.py
git commit -m "fix(scanner): category_counts 用可调 max_pages,低奖励品类不再少算"
```

---

### Task 4: manager 读设置并透传

**Files:**
- Modify: `engine/manager.py`(`_scan_with_status` 的 `fetch_candidates` 调用 `864-870`;`_compute_category_catalog` 的 `category_counts` 调用 `650`)
- Test: `tests/test_manager.py`

**Interfaces:**
- Consumes: `db.get_settings()["reward_scan_max_pages"]`(Task 1);`scanner.fetch_candidates(..., max_pages=)`、`scanner.category_counts(..., max_pages=)`(Task 2/3)。

- [ ] **Step 1: 写失败测试**

在 `tests/test_manager.py` 的 `class TestSharedScanWithStatus` 末尾追加(沿用文件内 `_make_manager` 与 `FakeScanner`/`patch` 模式):

```python
    def test_scan_threads_reward_scan_max_pages_from_settings(self):
        from config import ENGINE_DEFAULTS

        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        settings = dict(ENGINE_DEFAULTS)
        settings["reward_scan_max_pages"] = 42
        db.get_settings.return_value = settings
        seen = {}

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def fetch_candidates(self, templates, on_progress=None,
                                 on_found=None, **kw):
                seen["max_pages"] = kw.get("max_pages")
                return []

            def filter_for_template(self, pool, tmpl, addr):
                return pool

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager.scan_markets()
        assert seen["max_pages"] == 42
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `pytest tests/test_manager.py::TestSharedScanWithStatus::test_scan_threads_reward_scan_max_pages_from_settings -v`
Expected: FAIL —— 当前 `fetch_candidates` 调用不带 `max_pages`,`seen["max_pages"]` 为 `None`。

- [ ] **Step 3: 改 `_scan_with_status`**

`engine/manager.py` 的 `fetch_candidates` 调用(当前 `864-870`),加 `max_pages=`:

```python
                candidate_pool = scanner.fetch_candidates(
                    templates,
                    on_progress=on_progress,
                    on_found=on_found,
                    skip_orderbook=skip_orderbook,
                    cancel=cancel,
                    max_pages=self.db.get_settings()["reward_scan_max_pages"],
                )
```

- [ ] **Step 4: 改 `_compute_category_catalog`**

`engine/manager.py` 的 `category_counts` 调用(当前 `650`):

```python
            payload = scanner.category_counts(
                CATEGORY_CATALOG,
                max_pages=self.db.get_settings()["reward_scan_max_pages"],
            )
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `pytest tests/test_manager.py -v`
Expected: PASS(新用例 + 原有 manager 测试全绿;`FakeScanner` 的 `**kw` 吸收新 kwarg,旧用例不受影响)。

- [ ] **Step 6: 提交**

```bash
git add engine/manager.py tests/test_manager.py
git commit -m "feat(manager): 扫描/品类计数透传 reward_scan_max_pages 设置"
```

---

### Task 5: 配置页加「每品类扫描页数上限」输入框

**Files:**
- Modify: `web/templates/config.html`(引擎参数表单 `162-165` 之后)

**Interfaces:**
- Consumes: `/api/settings` 已回传/接收 `reward_scan_max_pages`(Task 1)。引擎表单 JS 通用(`config.html:338` 按 name 回填、`:417` 按 name 序列化提交),新 input 自动接线,**无 JS 改动**。

- [ ] **Step 1: 加输入框**

`web/templates/config.html`,在 `discovery_interval_sec` 的 `form-group`(当前 `162-165`)之后、`</div>`(表单 grid 收尾)之前,插入:

```html
            <div class="form-group">
                <label>每品类扫描页数上限 (每页 100，默认 20)</label>
                <input type="number" name="reward_scan_max_pages" step="1">
            </div>
```

- [ ] **Step 2: 校验无 BOM、中文正确**

Run:
```bash
head -c 3 web/templates/config.html | xxd            # 期望不是 ef bb bf(无 BOM)
grep -n "每品类扫描页数上限" web/templates/config.html # 期望命中一行、无别字
```
Expected: 首字节非 `ef bb bf`;grep 命中新增标签且中文正确。

- [ ] **Step 3: 手动核对接线(读代码,非跑测)**

确认 `config.html:338` 的 `loadEngine()` 会按 `name` 回填、`:417` 的 engine-form submit 会按 `name` 收 `input[type=number]` 提交到 `/api/settings`;新 input 的 `name="reward_scan_max_pages"` 命中两处通用逻辑,无需改 JS。

- [ ] **Step 4: 全量回归**

Run: `pytest`
Expected: PASS(全绿;前端为纯模板改动,无新测试)。

- [ ] **Step 5: 提交**

```bash
git add web/templates/config.html
git commit -m "feat(config): 配置页加每品类扫描页数上限输入框"
```

---

## 收尾(全部任务后)

- [ ] `pytest`(全量)绿。
- [ ] 人工验收(用户环境,给某天气号配 `include_other=False`+仅勾天气):手动扫描后候选数明显上升;`market_maker.log` 里天气 tag 抓取翻页超过 5 页(至 `next_cursor` 到底或 20 页);不再有不带品类的 full 抓取日志。
- [ ] 合并到 `main`(用 `superpowers:finishing-a-development-branch`)。发版属行为改变,建议主版本号(见 `docs/版本号规范.md`),但**发版待用户确认**。

## Self-Review 记录

- **Spec 覆盖:** 改动一(_tag_slug 返回记录)→ Task 2 Step 4;改动二(池并集/full 条件化)→ Task 2 Step 4;改动三(max_pages 默认 20)→ Task 1 + Task 2;改动四(category_counts)→ Task 3;改动五(manager 透传两处)→ Task 4;改动六(配置页输入框)→ Task 5;测试 1-7 分布于 Task 2/3 + Task 1 契约。全覆盖。
- **占位符:** 无 TBD/TODO;每步给出实代码或实命令。
- **类型一致:** `max_pages` 形参名、`ENGINE_DEFAULTS["reward_scan_max_pages"]` 默认、`get_rewards_markets(..., max_pages=)` 关键字在 Task 1→2→3→4 一致;`fetch_candidates`/`discover_candidates`/`category_counts` 签名与调用点匹配。
