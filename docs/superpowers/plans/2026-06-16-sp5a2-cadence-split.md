# SP5a-2 节奏拆分(发现慢/下单快)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把自动循环里昂贵的「全量奖励市场发现」挪到慢节奏(4h 可配),下单/重判仍按快节奏(30s)从缓存候选池跑,每个下单轮先刷新订单簿再选品。

**Architecture:** Scanner 把 `fetch_candidates` 拆成 `discover_candidates`(奖励发现、不抓簿)+ `refresh_orderbooks(pool)`(刷 `_orderbooks`),`fetch_candidates` = 两者合一(手动/测试不变)。EngineManager 用 `_should_discover(now)` 决策,`_scanner_loop` 每轮「按需发现 + 必下单轮」:`_discover` = `_scan_with_status(skip_orderbook=True)`,`_place_round` = 刷簿 + 每钱包 filter + place(空池跳过);删 `_do_scan`。

**Tech Stack:** Python 3.12 / pytest(MagicMock 桩 API、FakeScanner patch)。

**执行顺序:** T1 config(独立)→ T2 scanner 拆分(独立)→ T3 manager 循环(依赖 T1 键 + T2 方法)。基线:SP5a-1 合并后 `396 passed`。

---

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `config.py` | 加 `discovery_interval_sec` 引擎键 | 修改 |
| `engine/scanner.py` | 拆 `discover_candidates` + `refresh_orderbooks`,`fetch_candidates` 改为合一 | 修改 |
| `engine/manager.py` | `_should_discover`/`_discover`/`_place_round`/`_scanner_loop` 重构,删 `_do_scan`,`_scan_with_status` 加 `skip_orderbook` | 修改 |
| `tests/test_database.py` | 引擎键集合断言 + 新键默认值 | 修改 |
| `tests/test_scanner.py` | discover 无簿 / refresh 填簿 / fetch 含簿 | 修改 |
| `tests/test_manager.py` | `_do_scan` 测试迁移到 `_place_round` + `_should_discover`/`_discover` 新测试 | 修改 |

---

## Task 1: config 键 discovery_interval_sec

**Files:** Modify `config.py`(`ENGINE_DEFAULTS`)。Test: `tests/test_database.py`。

- [ ] **Step 1: 改失败测试 + 加新测试**

在 `tests/test_database.py` 的 `test_config_split_engine_and_template_defaults` 里,把引擎键集合断言:
```python
    assert set(ENGINE_DEFAULTS) == {
        "scan_interval_sec",
        "fill_check_interval_sec",
        "cooldown_minutes",
        "rewards_cache_ttl_sec",
    }
```
改为:
```python
    assert set(ENGINE_DEFAULTS) == {
        "scan_interval_sec",
        "fill_check_interval_sec",
        "cooldown_minutes",
        "rewards_cache_ttl_sec",
        "discovery_interval_sec",
    }
```

并在该函数末尾(`assert set(ENGINE_DEFAULTS) & set(TEMPLATE_DEFAULTS) == set()` 之后)加一行:
```python
    assert ENGINE_DEFAULTS["discovery_interval_sec"] == 14400
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_database.py::test_config_split_engine_and_template_defaults -v`
Expected: FAIL(当前 ENGINE_DEFAULTS 无 `discovery_interval_sec`,集合不等 / KeyError)

- [ ] **Step 3: 实现**

在 `config.py` 的 `ENGINE_DEFAULTS` 加一项(放在 `rewards_cache_ttl_sec` 之后、闭合 `}` 之前):
```python
    "discovery_interval_sec": 14400,
```
即:
```python
ENGINE_DEFAULTS = {
    "scan_interval_sec": 30,
    "fill_check_interval_sec": 5,
    "cooldown_minutes": 20,
    "rewards_cache_ttl_sec": 600,
    "discovery_interval_sec": 14400,
}
```

- [ ] **Step 4: 运行确认 PASS + 无回归**

Run: `python -m pytest tests/test_database.py -q`
Expected: ALL PASS

- [ ] **Step 5: Commit(不 stage `.claude/settings.local.json`)**

```bash
git add config.py tests/test_database.py
git commit -m "feat(config): 引擎键 discovery_interval_sec(4h,发现慢节奏)"
```

---

## Task 2: Scanner 拆分(discover_candidates + refresh_orderbooks)

**Files:** Modify `engine/scanner.py`(`fetch_candidates`)。Test: `tests/test_scanner.py`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_scanner.py` 的 `TestFetchCandidatesCategoryWiring` 类**之后**(`class TestFilterForTemplate` 之前)插入一个新类:

```python
class TestDiscoverAndRefreshSplit:
    def test_discover_candidates_has_no_orderbooks(self):
        api = MagicMock()
        api.get_rewards_markets.return_value = [
            {"condition_id": "C", "tokens": [{"token_id": "C-y"}], "rewards_config": []}
        ]
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        pool = scanner.discover_candidates(
            [{"excluded_categories": [], "min_reward_usd": 0}]
        )
        assert pool and all("_orderbooks" not in m for m in pool)
        api.get_spread.assert_not_called()      # 发现阶段不抓订单簿
        api.get_orderbook.assert_not_called()

    def test_refresh_orderbooks_fills_and_overwrites(self):
        api = MagicMock()
        api.get_spread.return_value = 0.01
        api.get_orderbook.return_value = {
            "bids": [{"price": "0.30", "size": "100"}],
            "asks": [{"price": "0.31", "size": "100"}],
            "tick_size": "0.01",
        }
        scanner = MarketScanner(api, MagicMock(), "")
        pool = [
            {"condition_id": "A", "tokens": [{"token_id": "A-y"}],
             "_orderbooks": {"STALE": {}}}  # 旧簿应被覆盖
        ]
        scanner.refresh_orderbooks(pool)
        assert "A-y" in pool[0]["_orderbooks"]
        assert "STALE" not in pool[0]["_orderbooks"]  # 覆盖写,不留陈旧

    def test_fetch_candidates_still_includes_orderbooks(self):
        api = MagicMock()
        api.get_rewards_markets.return_value = [
            {"condition_id": "C", "tokens": [{"token_id": "C-y"}], "rewards_config": []}
        ]
        api.get_spread.return_value = 0.01
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        db = MagicMock()
        db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        pool = scanner.fetch_candidates(
            [{"excluded_categories": [], "min_reward_usd": 0}]
        )
        assert all("_orderbooks" in m for m in pool)
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_scanner.py::TestDiscoverAndRefreshSplit -v`
Expected: FAIL(`AttributeError: 'MarketScanner' object has no attribute 'discover_candidates'`)

- [ ] **Step 3: 实现拆分**

在 `engine/scanner.py` 把现有 `fetch_candidates` 方法(从 `def fetch_candidates(` 到其 `return out` 结束)**整段替换**为下面三个方法。`discover_candidates` 是原 `fetch_candidates` 的循环体去掉订单簿那一行;`refresh_orderbooks` 新增;`fetch_candidates` 改为合一:

```python
    def discover_candidates(
        self, templates, on_progress=None, on_found=None
    ) -> list[dict]:
        """共享发现:抓全量奖励市场,按品类交集采集阶段排除,打 tags,补精确奖励。
        钱包无关、网络密集(rewards 端点)、不抓订单簿、不算价。"""
        inter = excluded_intersection(templates)
        queried = queried_categories(templates)
        floors = [t.get("min_reward_usd", 0) for t in templates]
        min_floor = min(floors) if floors else 0

        full = self.api.get_rewards_markets()
        category_ids = {}
        for slug in queried:
            rows = self.api.get_rewards_markets(tag_slug=slug)
            category_ids[slug] = {m.get("condition_id", "") for m in rows}

        pool = partition_candidates(full, category_ids, inter)
        blacklist = self.db.get_blacklist_ids()

        out = []
        checked = 0
        for market in pool:
            cid = market.get("condition_id", "")
            if cid in blacklist:
                continue
            total_rate = sum(
                rc.get("rate_per_day", 0) for rc in market.get("rewards_config", [])
            )
            if total_rate < min_floor:
                continue  # 比最宽松模板还低,任何模板都不会要
            self.db.upsert_market_meta(
                cid,
                market.get("question", ""),
                market.get("market_slug", ""),
                market.get("event_slug", ""),
            )
            # 精确每市场奖励(与旧 scan 一致:/rewards/markets/{cid})
            market_reward = total_rate
            try:
                raw = self.api.get_rewards_for_market(cid)
                if raw:
                    market_reward = sum(
                        rc.get("rate_per_day", 0)
                        for rd in raw
                        for rc in rd.get("rewards_config", [])
                    )
            except Exception as e:
                logger.warning("Precise reward fetch failed for %s: %s", cid, e)
            market["market_reward"] = market_reward
            # 候选池展示就绪键(供 /api/eligible 与持久化显示市场名/奖励)。
            # 不含 order_price/outcome:候选池是按市场的,单价改为每钱包下单时
            # 实时计算(见 filter_for_template),前端对缺失价格以「—」兜底。
            market["market_id"] = cid
            market["market_name"] = market.get("question", "")
            market["daily_reward"] = market_reward
            checked += 1
            if on_progress:
                on_progress(
                    checked, len(pool), f"Checking: {market.get('question','')}"
                )
            if on_found:
                on_found(market)
            out.append(market)
        logger.info(
            "discover_candidates: %d candidates (queried %d categories)",
            len(out),
            len(queried),
        )
        return out

    def refresh_orderbooks(self, pool):
        """给候选池每个市场刷新订单簿快照(覆盖写)。钱包无关、可重复调。
        某 token 抓不到则不入该市场的 _orderbooks(filter 现有逻辑会跳过该 token),
        覆盖写保证不留上一轮的陈旧簿。"""
        for market in pool:
            market["_orderbooks"] = self._fetch_orderbooks(market)

    def fetch_candidates(
        self, templates, on_progress=None, on_found=None, skip_orderbook=False
    ) -> list[dict]:
        """共享采集 = 发现 + (除非 skip_orderbook)刷新订单簿。手动扫描/单测用。"""
        pool = self.discover_candidates(
            templates, on_progress=on_progress, on_found=on_found
        )
        if not skip_orderbook:
            self.refresh_orderbooks(pool)
        return pool
```

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_scanner.py -v`
Expected: PASS(新增 3 个 + 既有 `TestFetchCandidatesCategoryWiring`(用 `skip_orderbook=True`,现等价于 discover-only)、`TestFilterForTemplate` 全过)

- [ ] **Step 5: 全套无回归**

Run: `python -m pytest -q`
Expected: ALL PASS（manager 暂未改,仍调旧 `fetch_candidates` 合一形态,不受影响）

- [ ] **Step 6: Commit**

```bash
git add engine/scanner.py tests/test_scanner.py
git commit -m "refactor(scanner): 拆 discover_candidates + refresh_orderbooks(fetch_candidates=合一)"
```

---

## Task 3: Manager 循环节奏拆分

**Files:** Modify `engine/manager.py`(`_scan_with_status` 加形参、新 `_should_discover`/`_discover`/`_place_round`、重写 `_scanner_loop`、删 `_do_scan`)。Test: `tests/test_manager.py`。

- [ ] **Step 1: 迁移/新增测试**

(a) 在 `tests/test_manager.py` 的 `TestSharedScanWithStatus` 类里,把三个 `_do_scan` 测试**替换**为对应的 `_place_round` 版本。

把 `test_auto_do_scan_filters_per_wallet_and_places` 整个方法替换为:
```python
    def test_place_round_filters_per_wallet_and_places(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xABC": worker}
        manager.eligible_markets = [{"market_id": "m9", "tags": []}]

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def refresh_orderbooks(self, pool):
                pass

            def filter_for_template(self, pool, tmpl, addr):
                return pool

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager._place_round()
        worker.place_orders.assert_called_once_with(
            [{"market_id": "m9", "tags": []}], cancel_dropouts=True
        )
```

把 `test_auto_do_scan_empty_pool_skips_placement` 整个方法替换为:
```python
    def test_place_round_empty_pool_skips_placement(self):
        # 空候选池 -> 不下单,避免 cancel_dropouts 误撤全部买单。
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xABC": worker}
        manager.eligible_markets = []
        manager._place_round()
        worker.place_orders.assert_not_called()
```

把 `test_auto_do_scan_distributes_sorted_by_competitiveness` 整个方法替换为:
```python
    def test_place_round_distributes_sorted_by_competitiveness(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xABC": worker}
        manager.eligible_markets = [
            {"market_id": "hi", "market_competitiveness": 0.9},
            {"market_id": "lo", "market_competitiveness": 0.1},
            {"market_id": "mid", "market_competitiveness": 0.5},
        ]

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def refresh_orderbooks(self, pool):
                pass

            def filter_for_template(self, pool, tmpl, addr):
                return list(pool)

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager._place_round()
        distributed = worker.place_orders.call_args[0][0]
        assert [m["market_id"] for m in distributed] == ["lo", "mid", "hi"]
```

(b) 在同一个 `TestSharedScanWithStatus` 类里追加节奏 / 发现测试:
```python
    def test_should_discover_empty_pool_true(self):
        manager, db = _make_manager()
        manager.eligible_markets = []
        manager.last_scan_time = 1000.0
        assert manager._should_discover(1000.0) is True

    def test_should_discover_recent_false(self):
        manager, db = _make_manager()
        manager.eligible_markets = [{"market_id": "m1"}]
        manager.last_scan_time = 1000.0
        # discovery_interval 默认 14400;距上次仅 30s -> 不发现
        assert manager._should_discover(1030.0) is False

    def test_should_discover_stale_true(self):
        manager, db = _make_manager()
        manager.eligible_markets = [{"market_id": "m1"}]
        manager.last_scan_time = 1000.0
        # 超过 14400s -> 该发现
        assert manager._should_discover(1000.0 + 14401) is True

    def test_discover_skips_orderbook(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        captured = {}

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, **kw
            ):
                captured.update(kw)
                return [{"market_id": "m1"}]

            def filter_for_template(self, pool, tmpl, addr):
                return pool

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager._discover()
        assert captured.get("skip_orderbook") is True
        assert manager.eligible_markets == [{"market_id": "m1"}]
        assert manager.last_scan_time > 0
```

(c) `_should_discover` 会读 `get_settings()["discovery_interval_sec"]`,而 `_make_manager()`(test_manager.py 顶部)的 settings 桩缺该键。在该桩 dict 里 `"cooldown_minutes": 20,` 那行**之后**补一行:
```python
        "discovery_interval_sec": 14400,
```
即 `_make_manager` 的 `db.get_settings.return_value` 变为含 `scan_interval_sec`/`fill_check_interval_sec`/`cooldown_minutes`/`discovery_interval_sec` + 模板键的 dict。

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_manager.py::TestSharedScanWithStatus -v`
Expected: FAIL(`_place_round`/`_should_discover`/`_discover` 尚不存在 → AttributeError)

- [ ] **Step 3: `_scan_with_status` 加 `skip_orderbook` 形参**

把
```python
    def _scan_with_status(self) -> list:
```
改为
```python
    def _scan_with_status(self, skip_orderbook: bool = False) -> list:
```
并把其内部
```python
            candidate_pool = scanner.fetch_candidates(
                templates, on_progress=on_progress, on_found=on_found
            )
```
改为
```python
            candidate_pool = scanner.fetch_candidates(
                templates,
                on_progress=on_progress,
                on_found=on_found,
                skip_orderbook=skip_orderbook,
            )
```

- [ ] **Step 4: 加 `_should_discover` / `_discover` / `_place_round`,删 `_do_scan`**

把整个 `_do_scan` 方法(从 `def _do_scan(self):` 到其结尾 `logger.error("Error distributing to wallet %s: %s", address, e)` 那行)**替换**为下面三个方法:

```python
    def _should_discover(self, now: float) -> bool:
        """无缓存池、或距上次发现 >= discovery_interval -> 该重新发现。"""
        interval = self.db.get_settings()["discovery_interval_sec"]
        return (not self.eligible_markets) or (now - self.last_scan_time) >= interval

    def _discover(self):
        """慢节奏:全量奖励发现(不抓订单簿),刷新缓存候选池 + 持久化。"""
        self._scan_with_status(skip_orderbook=True)

    def _place_round(self):
        """快节奏:刷新订单簿 -> 每钱包精筛 + 下单(跌出撤单)。空池跳过。"""
        if not self.eligible_markets:
            return
        scanner = MarketScanner(self._scanner_api, self.db, "")
        scanner.refresh_orderbooks(self.eligible_markets)
        for address, worker in self.engines.items():
            if not worker.running:
                continue
            try:
                tmpl = self.db.get_template_for(address)
                eligible = scanner.filter_for_template(
                    self.eligible_markets, tmpl, address
                )
                eligible.sort(
                    key=lambda m: float(m.get("market_competitiveness", 0) or 0)
                )
                worker.place_orders(eligible, cancel_dropouts=True)
            except Exception as e:
                logger.error("Error distributing to wallet %s: %s", address, e)
```

- [ ] **Step 5: 重写 `_scanner_loop`(按需发现 + 必下单轮,各自独立 try)**

把
```python
    def _scanner_loop(self):
        """Shared scanner: runs once per scan_interval, feeds all wallets."""
        settings = self.db.get_settings()
        scan_interval = settings["scan_interval_sec"]

        while not self._stop_event.is_set():
            if self._scanner_api and self.engines:
                try:
                    self._do_scan()
                except Exception as e:
                    logger.error("Scanner error: %s", e)

            self._stop_event.wait(timeout=scan_interval)
```
改为
```python
    def _scanner_loop(self):
        """快节奏循环:按 discovery_interval 发现新市场(慢),每轮刷簿+下单(快)。"""
        settings = self.db.get_settings()
        place_interval = settings["scan_interval_sec"]

        while not self._stop_event.is_set():
            if self._scanner_api and self.engines:
                try:
                    if self._should_discover(time.time()):
                        self._discover()
                except Exception as e:
                    logger.error("Discovery error: %s", e)
                # 发现失败也不拖垮下单:_scan_with_status 失败保留上一份缓存池,
                # 本轮仍用它刷簿下单;首启失败则池空,_place_round 空池 guard 跳过。
                try:
                    self._place_round()
                except Exception as e:
                    logger.error("Place round error: %s", e)

            self._stop_event.wait(timeout=place_interval)
```

- [ ] **Step 6: 运行确认 PASS**

Run: `python -m pytest tests/test_manager.py -v`
Expected: PASS(迁移的 3 个 `_place_round` + 4 个新增 + 既有 `_scan_with_status`/`scan_markets` 用例不变)

- [ ] **Step 7: 全套测试无回归**

Run: `python -m pytest -q`
Expected: ALL PASS（基线 396 + T1 的 1（`discovery_interval` 断言并入既有用例,净 0 新增计数)... 实际计数:T2 +3、T3 净 +4(迁移 3 个改名不增减、新增 `_should_discover`×3 + `_discover`×1 = +4)。预期 `403 passed`。)

- [ ] **Step 8: Commit**

```bash
git add engine/manager.py tests/test_manager.py
git commit -m "feat(manager): 节奏拆分——发现按 discovery_interval(4h)、下单按 scan_interval(快)"
```

---

## 验收 checkpoint(对应 spec §八)

1. 发现按 `discovery_interval_sec`、下单按 `scan_interval_sec`:`_should_discover` 三测 + `_scanner_loop` 重写。
2. 每下单轮先刷簿再选品:`_place_round` 调 `refresh_orderbooks` 后 `filter_for_template`(`test_place_round_filters_per_wallet_and_places`)。
3. 空池跳过下单:`test_place_round_empty_pool_skips_placement`。
4. 手动扫描/下单不变:`scan_markets`→`_scan_with_status()`(默认带簿);`TestScanMarketsLastScanTime`/`test_manual_scan_*` 不改仍过。
5. `last_scan_time`=发现时间;前端不改(本计划不动 routes/模板)。
6. `pytest` 全绿:T3 Step 7。

## 范围之外

方案 B(价差/价区门槛挪进 `place_orders`、订单簿只取子集)· SP6 模板 UI。
