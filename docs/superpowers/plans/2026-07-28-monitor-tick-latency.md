# 监控 tick 提速 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「奖励下跌 / 档位变化 → 撤单」的响应延迟从一分钟多压下来，办法是缩短监控 tick 的周期，而不是改任何交易判定。

**Architecture:** `check_exit` 现在逐仓串行发两次请求（`get_trades` 重建成本 + `get_orderbook`），是 tick 里唯一 O(持仓数) 的串行段。照 Step 3 已验证的 `_prefetch` 模式改成两拨并发预取（成交拨按 `condition_id` 去重、盘口拨按 `asset_id` 去重，两拨串行且成交先盘口后），judgment/下单/记账全部留在主线程。再顺手让步骤 1 到 4 共用一份 `get_open_orders` 快照，并加一条分段耗时日志作为验证手段。阶段 2（Step 3 独立线程）和阶段 3（补 `rewards_min_size` 复查）在后面。

**Tech Stack:** Python 3、pytest、`unittest.mock`、`api/proxy.py` 的 `parallel_map`（`ThreadPoolExecutor` + contextvar 代理选路）。

## Global Constraints

以下约束适用于每一个 Task，不再逐条重复：

- **预取 worker 线程只发网络请求，绝不碰 `db`。** `models/database.py` 每线程开一条 sqlite 连接且从不回收，worker 里读一次 db 就是每轮泄漏一批连接。所有 DB 读、`_record_action`、`_status_add`、下单、撤单一律留在主线程。
- **不改任何交易判定逻辑。** `plan_exit`、`plan_take_profit`、`recheck_resting_buy`、`effective_theta_stop`、`plan_liquidation` 一个字符都不动。本计划只动取数的并发结构和调用节奏。
- **卖单永不低于成本**：`_exit_position` 里 `ceil_to_tick(cost)` 的钳位和那条 WARNING 保持原样。
- **成本只认 `get_trades` 逐笔重建**，绝不用 Data API `avgPrice`；重建不出就跳过并留 ⚠️裸奔状态行。
- **`0.0` 与 `None` 语义不可合并**成一个 falsy 判断（奖励额、best_bid 都适用）。
- **Gamma 结算状态取数失败一律 fail-open。**
- 每个预取任务自带 try/except、绝不外抛；「取数失败」与「取到了但为空」在缓存里必须能分开。
- 提交只 stage 本 Task 涉及的文件，不要卷进无关的 WIP。
- 本仓有自动格式化 hook：每次编辑 `.py` 会重排整个文件。**每个 Task 提交前跑 `git diff --stat`**，如果无关代码被重新折行，还原掉再提交。
- 跑测试：`pytest -q`。当前基线 861 绿。

---

## 阶段 1：逐仓取数并发化

### Task 1: `_exit_prefetch` 并发预取（纯新增，先不接线）

**Files:**
- Modify: `engine/monitor.py`（`__init__` 约 55 行、`begin_status_tick` 约 63 行，新方法加在 `_sell_book` 之前）
- Test: `tests/test_monitor.py`（新增 `class TestExitPrefetch`，放在 `class TestStep3Prefetch` 之前）

**Interfaces:**
- Consumes: `api.proxy.parallel_map(func, items, max_workers)`、`py_clob_client_v2` 的 `TradeParams`（`engine/monitor.py` 顶部已 import 两者）
- Produces: `OrderMonitor._exit_prefetch(positions) -> None`（副作用写进 `self._trades_by_cid` / `self._book_cache`）；两个 per-tick 缓存字典；模块级常量 `_EXIT_MAX_WORKERS = 6`、`_MISSING = object()`

- [ ] **Step 1: 写失败的测试**

加到 `tests/test_monitor.py`，紧挨在 `class TestStep3Prefetch` 之前：

```python
class TestExitPrefetch:
    """离场判定的成交/盘口并发预取:按市场/asset 去重、逐项容错、不重复取。"""

    def _pos(self, asset, cid, size="100"):
        return {"asset": asset, "conditionId": cid, "size": size, "curPrice": "0.30"}

    def _ob(self):
        return {
            "bids": [{"price": "0.30", "size": "1000"}],
            "asks": [{"price": "0.31", "size": "1000"}],
            "tick_size": "0.01",
        }

    def test_dedups_trades_by_condition_id(self):
        # 同市场 YES/NO 两个仓:成交只取一次,盘口按 asset 取两次
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_orderbook.return_value = self._ob()
        api.get_trades.return_value = [{"id": "t1"}]
        monitor._exit_prefetch([self._pos("tokYes", "cid1"), self._pos("tokNo", "cid1")])
        assert api.get_trades.call_count == 1
        assert api.get_orderbook.call_count == 2
        assert monitor._trades_by_cid == {"cid1": [{"id": "t1"}]}
        assert set(monitor._book_cache) == {"tokYes", "tokNo"}

    def test_trades_failure_recorded_as_none(self):
        # 取数失败记 None,与「取到了但没有成交」([])区分开
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_orderbook.return_value = self._ob()
        api.get_trades.side_effect = RuntimeError("network")
        monitor._exit_prefetch([self._pos("tokA", "cid1")])
        assert monitor._trades_by_cid == {"cid1": None}

    def test_empty_trades_list_is_not_none(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_orderbook.return_value = self._ob()
        api.get_trades.return_value = []
        monitor._exit_prefetch([self._pos("tokA", "cid1")])
        assert monitor._trades_by_cid == {"cid1": []}

    def test_book_failure_recorded_as_none(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_trades.return_value = []
        api.get_orderbook.side_effect = RuntimeError("network")
        monitor._exit_prefetch([self._pos("tokA", "cid1")])
        assert monitor._book_cache == {"tokA": None}

    def test_one_failure_does_not_affect_others(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_trades.return_value = []

        def _ob_or_boom(asset_id):
            if asset_id == "tokBad":
                raise RuntimeError("network")
            return self._ob()

        api.get_orderbook.side_effect = _ob_or_boom
        monitor._exit_prefetch([self._pos("tokBad", "cid1"), self._pos("tokOk", "cid2")])
        assert monitor._book_cache["tokBad"] is None
        assert monitor._book_cache["tokOk"] == self._ob()

    def test_second_call_only_fetches_the_difference(self):
        # check_low_balance 先预取过一轮时,check_exit 这轮只补差集
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_orderbook.return_value = self._ob()
        api.get_trades.return_value = []
        monitor._exit_prefetch([self._pos("tokA", "cid1")])
        monitor._exit_prefetch([self._pos("tokA", "cid1"), self._pos("tokB", "cid2")])
        assert api.get_trades.call_count == 2       # cid1 一次 + cid2 一次
        assert api.get_orderbook.call_count == 2    # tokA 一次 + tokB 一次

    def test_begin_status_tick_clears_both_caches(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_orderbook.return_value = self._ob()
        api.get_trades.return_value = []
        monitor._exit_prefetch([self._pos("tokA", "cid1")])
        monitor.begin_status_tick()
        assert monitor._trades_by_cid == {}
        assert monitor._book_cache == {}

    def test_trades_wave_completes_before_books_wave(self):
        # 顺序不可调:盘口驱动挂价与 B0 判定,必须是更新鲜的那份
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        seen = []
        api.get_trades.side_effect = lambda *a, **k: seen.append("trades") or []
        api.get_orderbook.side_effect = lambda *a, **k: seen.append("book") or self._ob()
        monitor._exit_prefetch(
            [self._pos("tokA", "cid1"), self._pos("tokB", "cid2")]
        )
        assert seen.count("trades") == 2 and seen.count("book") == 2
        assert seen.index("book") > max(i for i, s in enumerate(seen) if s == "trades")

    def test_skips_blank_ids(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._exit_prefetch([{"asset": "", "conditionId": "", "size": "10"}])
        assert api.get_trades.call_count == 0
        assert api.get_orderbook.call_count == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_monitor.py::TestExitPrefetch -q`
Expected: FAIL，`AttributeError: 'OrderMonitor' object has no attribute '_exit_prefetch'`

- [ ] **Step 3: 实现**

在 `engine/monitor.py` 顶部、`_STEP3_MAX_WORKERS = 6` 那一行下面加：

```python
_EXIT_MAX_WORKERS = 6
# 缓存里 None 表示「取数失败」,所以「没这个键」需要另一个哨兵,不能用 None 表达。
_MISSING = object()
```

在 `OrderMonitor.__init__` 里 `self._just_dumped: set = set()` 之后加：

```python
        # 本 tick 的离场取数预取表(begin_status_tick 重置)。
        # condition_id -> get_trades 原始返回;None = 本轮取数失败
        self._trades_by_cid: dict = {}
        # asset_id -> get_orderbook 原始返回;None = 本轮取数失败
        self._book_cache: dict = {}
```

在 `begin_status_tick` 里 `self._just_dumped = set()` 之后加：

```python
        self._trades_by_cid = {}
        self._book_cache = {}
```

在 `_sell_book` 方法之前加：

```python
    def _exit_prefetch(self, positions) -> None:
        """本 tick 离场判定所需的成交/盘口并发预取,结果写进两张 per-tick 表。

        **成交先取、盘口后取,两拨串行,顺序不可调。** 先取的那拨等到判定时,已经陈旧了
        后取那拨的全部耗时;而盘口驱动挂卖价与 B0 强平判定(是钱路上的决定),成本则只在
        我们自己成交时才变。与 Step3 `_prefetch` 里「奖励先、盘口后」是同构的理由。

        成交拨按 condition_id 去重:同一市场的 YES/NO 两个持仓共用一次
        get_trades(market=cid)。盘口拨按 asset_id 去重。已经在表里的键一律跳过,所以
        check_low_balance 先预取过一轮之后,check_exit 这轮只补差集。

        表里的 None 表示**取数失败**,与「取到了但没有成交 / 盘口为空」不是一回事:
        前者让成本算不出来(跳过 + ⚠️裸奔),后者是正常的空结果。两者塌成一个会让监控
        状态表说谎。

        worker 线程只发网络请求、**绝不碰 db**(每线程一条 sqlite 连接且从不回收),
        每个任务自带异常处理、绝不外抛。判定、下单、记账、状态行全部留在主线程。
        """
        cids = [
            c
            for c in {p.get("conditionId", "") for p in positions}
            if c and c not in self._trades_by_cid
        ]
        assets = [
            a
            for a in {p.get("asset", "") for p in positions}
            if a and a not in self._book_cache
        ]

        def _trades(cid):
            try:
                return self.api.get_trades(TradeParams(market=cid))
            except Exception as e:
                logger.warning("[离场预取] get_trades(market=%s) 失败: %s", cid, e)
                return None

        def _book(asset_id):
            try:
                return self.api.get_orderbook(asset_id)
            except Exception as e:
                logger.warning("[离场预取] 订单簿 %s 失败: %s", asset_id, e)
                return None

        if cids:
            self._trades_by_cid.update(
                zip(cids, parallel_map(_trades, cids, _EXIT_MAX_WORKERS))
            )
        if assets:
            self._book_cache.update(
                zip(assets, parallel_map(_book, assets, _EXIT_MAX_WORKERS))
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_monitor.py::TestExitPrefetch -q`
Expected: 9 passed

Run: `pytest -q`
Expected: 870 passed（861 + 9）

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat(monitor): 离场取数并发预取 _exit_prefetch(未接线)"
```

---

### Task 2: `_cost_lots` / `_sell_book` 改读预取表（带回退）

**Files:**
- Modify: `engine/monitor.py:166-187`（`_cost_lots`）、`engine/monitor.py:348-362`（`_sell_book`）
- Test: `tests/test_monitor.py`（`class TestExitPrefetch` 末尾追加）

**Interfaces:**
- Consumes: Task 1 的 `self._trades_by_cid` / `self._book_cache` / `_MISSING`
- Produces: 两个方法签名不变（`_cost_lots(asset_id, size, condition_id) -> (cost|None, lots)`、`_sell_book(asset_id) -> (tick_float, tick_str, best_bid, best_ask)`），只改取数来源

- [ ] **Step 1: 写失败的测试**

追加到 `class TestExitPrefetch` 末尾：

```python
    def test_cost_lots_uses_prefetched_trades(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._trades_by_cid["cid1"] = []
        monitor._cost_lots("tokA", 100.0, "cid1")
        assert api.get_trades.call_count == 0

    def test_cost_lots_falls_back_when_not_prefetched(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_trades.return_value = []
        monitor._cost_lots("tokA", 100.0, "cid1")
        assert api.get_trades.call_count == 1

    def test_cost_lots_prefetch_failure_means_cost_unknown(self):
        # 预取那轮取数失败 -> 与自取失败同样的结果:成本未知,调用方跳过 + ⚠️裸奔
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._trades_by_cid["cid1"] = None
        assert monitor._cost_lots("tokA", 100.0, "cid1") == (None, [])
        assert api.get_trades.call_count == 0

    def test_sell_book_uses_prefetched_orderbook(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._book_cache["tokA"] = self._ob()
        tick, tick_str, bid, ask = monitor._sell_book("tokA")
        assert api.get_orderbook.call_count == 0
        assert (tick, tick_str, bid, ask) == (0.01, "0.01", 0.30, 0.31)

    def test_sell_book_falls_back_when_not_prefetched(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_orderbook.return_value = self._ob()
        assert monitor._sell_book("tokA")[2] == 0.30
        assert api.get_orderbook.call_count == 1

    def test_sell_book_prefetch_failure_degrades_like_fetch_failure(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._book_cache["tokA"] = None
        assert monitor._sell_book("tokA") == (0.01, "0.01", None, None)
        assert api.get_orderbook.call_count == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_monitor.py::TestExitPrefetch -q`
Expected: FAIL，`test_cost_lots_uses_prefetched_trades` 报 `assert 1 == 0`（现在还是无条件自取）

- [ ] **Step 3: 实现**

把 `_cost_lots` 的方法体（`engine/monitor.py:175-187`，从 `if asset_id in self._cost_cache:` 到 `return result`）替换成：

```python
        if asset_id in self._cost_cache:
            return self._cost_cache[asset_id]
        funder = self._funder()
        trades = self._trades_by_cid.get(condition_id, _MISSING)
        if trades is _MISSING:
            # 没走预取的调用点(_resolution_dump 等)照旧自取,行为不变。
            try:
                trades = self.api.get_trades(TradeParams(market=condition_id))
            except Exception as e:
                logger.warning(
                    "get_trades(market=%s) for cost failed: %s", condition_id, e
                )
                self._cost_cache[asset_id] = (None, [])
                return None, []
        if trades is None:
            # 预取那轮取数失败(已在预取里 WARNING 过),等价于自取失败:成本未知。
            self._cost_cache[asset_id] = (None, [])
            return None, []
        fills = extract_fills(trades, funder, asset_id)
        result = position_cost_with_lots(fills, size)
        self._cost_cache[asset_id] = result
        return result
```

并在 `_cost_lots` 的 docstring 末尾补一句：

```
    盘口/成交优先读本 tick 的预取表(_exit_prefetch 写入),表里没有该键才自取。
```

把 `_sell_book` 整个方法（`engine/monitor.py:348-362`）替换成：

```python
    def _sell_book(self, asset_id: str):
        """(tick_float, tick_str, best_bid, best_ask);失败/空时缺的位回 None。

        优先读本 tick 的预取表(_exit_prefetch 写入);表里没有该 asset 才自取,所以
        不走预取的调用点行为不变。表里的 None = 预取时取数失败,与自取失败同样降级。
        """
        ob = self._book_cache.get(asset_id, _MISSING)
        if ob is _MISSING:
            try:
                ob = self.api.get_orderbook(asset_id)
            except Exception as e:
                logger.warning(
                    "orderbook for %s failed (exit tick=0.01): %s", asset_id, e
                )
                return 0.01, "0.01", None, None
        if ob is None:
            logger.warning("orderbook for %s 预取取不到 (exit tick=0.01)", asset_id)
            return 0.01, "0.01", None, None
        try:
            tick_str = ob.get("tick_size", "0.01")
            bids = sorted(
                ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True
            )
            asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            return float(tick_str), tick_str, best_bid, best_ask
        except Exception as e:
            logger.warning("orderbook for %s 解析失败 (exit tick=0.01): %s", asset_id, e)
            return 0.01, "0.01", None, None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_monitor.py -q`
Expected: 全绿（`TestCostHelper`、`TestCheckExit`、`TestLowBalance`、`TestResolutionExit` 都不该有变化，因为没预取时行为完全一致）

Run: `pytest -q`
Expected: 876 passed

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat(monitor): _cost_lots/_sell_book 优先读预取表,缺失回退自取"
```

---

### Task 3: `check_exit` 与 `check_low_balance` 接上预取

**Files:**
- Modify: `engine/monitor.py:390-395` 附近（`check_low_balance` 取到 positions 之后）、`engine/monitor.py:491-493` 附近（`check_exit` 的 gamma 调用之后、循环之前）
- Test: `tests/test_monitor.py`（`class TestExitPrefetch` 末尾追加）

**Interfaces:**
- Consumes: Task 1 的 `_exit_prefetch`
- Produces: `OrderMonitor.last_position_count: int`（Task 6 的耗时日志要读）

- [ ] **Step 1: 写失败的测试**

追加到 `class TestExitPrefetch` 末尾：

```python
    def test_check_exit_prefetches_before_looping(self):
        # 3 个仓分属 2 个市场:成交按市场取 2 次,盘口按 asset 取 3 次
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.get_user_positions.return_value = [
            self._pos("tokA", "cid1"),
            self._pos("tokB", "cid1"),
            self._pos("tokC", "cid2"),
        ]
        api.get_open_orders.return_value = []
        api.get_orderbook.return_value = self._ob()
        api.get_trades.return_value = []
        monitor.check_exit()
        assert api.get_trades.call_count == 2
        assert api.get_orderbook.call_count == 3
        assert monitor.last_position_count == 3

    def test_check_exit_does_not_prefetch_just_dumped(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._just_dumped.add("tokA")
        api.get_user_positions.return_value = [
            self._pos("tokA", "cid1"),
            self._pos("tokB", "cid2"),
        ]
        api.get_open_orders.return_value = []
        api.get_orderbook.return_value = self._ob()
        api.get_trades.return_value = []
        monitor.check_exit()
        assert api.get_orderbook.call_count == 1
        assert "tokA" not in monitor._book_cache

    def test_low_balance_prefetches_and_check_exit_reuses(self):
        # 低余额先预取过,check_exit 不再重复取同一批
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        db.get_template_for.return_value = {
            "low_balance_threshold_usd": 4,
            "liquidate_target_usd": 4,
            "cooldown_minutes": 20,
        }
        api.get_balance.return_value = 1.0
        api.get_user_positions.return_value = [self._pos("tokA", "cid1")]
        api.get_open_orders.return_value = []
        api.get_orderbook.return_value = self._ob()
        api.get_trades.return_value = []
        monitor.check_low_balance()
        before = api.get_orderbook.call_count
        monitor.check_exit()
        assert api.get_orderbook.call_count == before  # 复用,没再取
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_monitor.py::TestExitPrefetch -q`
Expected: FAIL，`test_check_exit_prefetches_before_looping` 报 `AttributeError: ... 'last_position_count'`

- [ ] **Step 3: 实现**

在 `check_low_balance` 里，`positions = self.api.get_user_positions(...)` / `open_orders = ...` 那个 try 块之后、`meta, candidates = {}, []` 之前，插入：

```python
        # 逐仓的成交/盘口一次性并发取好,下面的循环只做纯判定(见 _exit_prefetch)。
        self._exit_prefetch([p for p in positions if float(p.get("size", 0) or 0) > 0])
```

在 `check_exit` 里，`resolving = {c for c in cids if in_resolution(status_map.get(c))}` 之后、`for pos in positions:` 之前，插入：

```python
        self.last_position_count = len(cids)
        # 本轮真要判定的仓(刚被低余额清掉的不算)一次性并发取好成交与盘口。
        self._exit_prefetch(
            [
                p
                for p in positions
                if float(p.get("size", 0) or 0) > 0
                and p.get("asset", "") not in self._just_dumped
            ]
        )
```

在 `OrderMonitor.__init__` 里 `self._book_cache: dict = {}` 之后加：

```python
        # 上一轮 check_exit 判定的持仓数,只给 tick 耗时日志用,不进任何判定。
        self.last_position_count: int = 0
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_monitor.py -q`
Expected: 全绿

Run: `pytest -q`
Expected: 879 passed

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat(monitor): check_exit/check_low_balance 接上离场预取"
```

---

### Task 4: 步骤 1 到 4 共用一份 `get_open_orders` 快照

**Files:**
- Modify: `engine/monitor.py`（`check_buy_orders` 约 190 行、`check_resolution` 约 277 行、`check_low_balance` 约 364 行、`check_exit` 约 462 行的签名与取数）、`engine/manager.py:130-142`（`_tick`）
- Test: `tests/test_manager.py`（新增 `class TestTickSharesOpenOrders`）

**Interfaces:**
- Consumes: 无
- Produces: 四个方法签名变为 `check_buy_orders(open_orders=None)` / `check_resolution(open_orders=None)` / `check_low_balance(open_orders=None)` / `check_exit(open_orders=None)`。`None` = 未提供，各自照旧自取，所以现有直接调用这些方法的测试一律不用改。**`check_sell_orders`（Step 3）签名不变、继续自取。**

**为什么 Step 3 不共用：** 它跑在 tick 最后，共用快照是最陈旧的那一份。它全程只撤不挂（`monitor.py:1290` 起每个分支都是「撤单不重挂」），所以陈旧快照不会导致乱下单，但会对一张已经被 Step 1 撤掉的单再撤一次，并往 `actions` 表写一条幻影 `reward_drop_cancel` 记录污染历史页。省下的一个往返不值这个。

- [ ] **Step 1: 写失败的测试**

新增到 `tests/test_manager.py` 末尾：

```python
class TestTickSharesOpenOrders:
    """一个 tick 里 get_open_orders 只取两次:步骤 1-4 共用一份,Step3 自取新鲜的。"""

    def _worker(self):
        from engine.manager import WalletWorker

        api = MagicMock()
        db = MagicMock()
        api.get_open_orders.return_value = []
        api.get_trades.return_value = []
        api.get_user_positions.return_value = []
        api.gamma_resolution_status.return_value = {}
        api.get_balance.return_value = 100.0
        db.get_settings.return_value = {"rewards_cache_ttl_sec": 0}
        db.get_template_for.return_value = {"low_balance_threshold_usd": 0}
        db.get_blacklist_ids.return_value = set()
        worker = WalletWorker(api, db, "0xW", {"fill_check_interval_sec": 5})
        worker._last_pnl_date = "9999-01-01"  # 不让台账重算线程掺进来
        return worker, api, db

    def test_tick_fetches_open_orders_twice(self):
        worker, api, db = self._worker()
        worker._tick()
        assert api.get_open_orders.call_count == 2

    def test_shared_snapshot_failure_does_not_kill_the_tick(self):
        # 共用快照取不到时各步自己降级,tick 不能整个抛出去
        worker, api, db = self._worker()
        api.get_open_orders.side_effect = RuntimeError("network")
        worker._tick()  # 不抛异常即通过
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_manager.py::TestTickSharesOpenOrders -q`
Expected: FAIL，`assert 4 == 2`（现在是 4 次）

- [ ] **Step 3: 实现**

`engine/monitor.py`，四个方法各改两处。

`check_buy_orders`：签名改成 `def check_buy_orders(self, open_orders=None):`，把

```python
        open_ids: set = set()
        if fills:
            try:
                open_ids = {o.get("id") for o in self.api.get_open_orders()}
            except Exception as e:
                logger.warning(
                    "get_open_orders failed in Step 1 (本轮跳过撤余量): %s", e
                )
```

改成

```python
        open_ids: set = set()
        if fills:
            if open_orders is not None:
                open_ids = {o.get("id") for o in open_orders}
            else:
                try:
                    open_ids = {o.get("id") for o in self.api.get_open_orders()}
                except Exception as e:
                    logger.warning(
                        "get_open_orders failed in Step 1 (本轮跳过撤余量): %s", e
                    )
```

`check_resolution`：签名改成 `def check_resolution(self, open_orders=None):`，把开头的

```python
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed (skip resolution guard): %s", e)
            return
```

改成

```python
        if open_orders is None:
            try:
                open_orders = self.api.get_open_orders()
            except Exception as e:
                logger.error("get_open_orders failed (skip resolution guard): %s", e)
                return
```

`check_low_balance`：签名改成 `def check_low_balance(self, open_orders=None):`，把

```python
        try:
            positions = self.api.get_user_positions(self._funder())
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.warning("fetch failed (skip low-balance): %s", e)
            return
```

改成

```python
        try:
            positions = self.api.get_user_positions(self._funder())
            if open_orders is None:
                open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.warning("fetch failed (skip low-balance): %s", e)
            return
```

`check_exit`：签名改成 `def check_exit(self, open_orders=None):`，把

```python
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed (skip exit): %s", e)
            return
```

改成

```python
        if open_orders is None:
            try:
                open_orders = self.api.get_open_orders()
            except Exception as e:
                logger.error("get_open_orders failed (skip exit): %s", e)
                return
```

`engine/manager.py` 的 `_tick`（130-142 行）改成：

```python
    @_worker_proxied
    def _tick(self):
        """One monitor pass: detect fills, UMA resolution guard, three-tier
        exit, strategy compliance. check_resolution runs right after fill
        detection to cancel buys in any market whose UMA resolution was just
        proposed; check_exit then reflects the latest fills.

        步骤 1-4 共用一份 tick 开头取的挂单快照。Step3(check_sell_orders)**不共用**:
        它跑在最后、共用的那份最陈旧,而它会对已经消失的单再撤一次并往 actions 写
        幻影记录,省一个往返不值。快照取不到时传 None,各步照旧自取或自行降级。
        """
        self._maybe_rebuild_pnl()
        self.monitor.begin_status_tick()
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.warning("tick 开头取挂单失败(各步自行降级): %s", e)
            open_orders = None
        self.monitor.check_buy_orders(open_orders)
        self.monitor.check_resolution(open_orders)
        self.monitor.check_low_balance(open_orders)
        self.monitor.check_exit(open_orders)
        self.monitor.check_sell_orders()
        self.monitor.publish_status()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_manager.py::TestTickSharesOpenOrders -q`
Expected: 2 passed

Run: `pytest -q`
Expected: 881 passed

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py engine/manager.py tests/test_manager.py
git commit -m "perf(monitor): 步骤 1-4 共用一份挂单快照,Step3 仍自取新鲜的"
```

---

### Task 5: tick 分段耗时日志

**Files:**
- Modify: `engine/manager.py`（Task 4 改过的 `_tick`）
- Test: `tests/test_manager.py`（`class TestTickSharesOpenOrders` 末尾追加）

**Interfaces:**
- Consumes: Task 3 的 `monitor.last_position_count`
- Produces: 一条 INFO 日志，前缀 `[tick]`

- [ ] **Step 1: 写失败的测试**

追加到 `class TestTickSharesOpenOrders`：

```python
    def test_tick_logs_per_step_timing(self, caplog):
        worker, api, db = self._worker()
        with caplog.at_level(logging.INFO, logger="engine.manager"):
            worker._tick()
        line = [r.message % r.args for r in caplog.records if "[tick]" in str(r.msg)]
        assert len(line) == 1
        for name in ("成交", "结算", "低余额", "离场", "合规"):
            assert name in line[0]

    def test_timing_log_emitted_even_when_a_step_raises(self, caplog):
        # 慢在哪一步的证据,不能因为那一步抛异常就丢掉
        worker, api, db = self._worker()
        worker.monitor.check_exit = MagicMock(side_effect=RuntimeError("boom"))
        with caplog.at_level(logging.INFO, logger="engine.manager"):
            with pytest.raises(RuntimeError):
                worker._tick()
        assert any("[tick]" in str(r.msg) for r in caplog.records)
```

`tests/test_manager.py` 顶部若无 `import logging` / `import pytest` 则补上。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_manager.py::TestTickSharesOpenOrders -q`
Expected: FAIL，`assert 0 == 1`（还没有这条日志）

- [ ] **Step 3: 实现**

把 Task 4 写好的 `_tick` 方法体（从 `self._maybe_rebuild_pnl()` 到 `self.monitor.publish_status()`）替换成：

```python
        t0 = time.time()
        marks: list = []

        def _step(name, fn, *a):
            t = time.time()
            try:
                fn(*a)
            finally:
                marks.append(f"{name}{time.time() - t:.1f}s")

        self._maybe_rebuild_pnl()
        self.monitor.begin_status_tick()
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.warning("tick 开头取挂单失败(各步自行降级): %s", e)
            open_orders = None
        try:
            _step("成交", self.monitor.check_buy_orders, open_orders)
            _step("结算", self.monitor.check_resolution, open_orders)
            _step("低余额", self.monitor.check_low_balance, open_orders)
            _step("离场", self.monitor.check_exit, open_orders)
            _step("合规", self.monitor.check_sell_orders)
            self.monitor.publish_status()
        finally:
            # 监控周期 = 本轮耗时 + fill_check_interval_sec,而撤单/止损的响应延迟上限
            # 就是这个周期。哪一步在吃时间只有这条日志看得见,放 finally 是因为抛异常的
            # 那一轮往往正是最慢的那一轮,证据不能跟着异常一起丢。
            logger.info(
                "[tick] %s 持仓%s 挂单%s 总计%.1fs | %s",
                self.wallet_address[:8],
                self.monitor.last_position_count,
                len(open_orders) if open_orders is not None else "?",
                time.time() - t0,
                " ".join(marks),
            )
```

`engine/manager.py` 顶部若无 `import time` 则补上（该文件已在用 `time.time()`，通常已有）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_manager.py -q`
Expected: 全绿

Run: `pytest -q`
Expected: 883 passed

- [ ] **Step 5: 提交**

```bash
git add engine/manager.py tests/test_manager.py
git commit -m "feat(monitor): tick 分段耗时日志(定位周期瓶颈)"
```

---

### Task 6: 文档订正

**Files:**
- Modify: `CLAUDE.md`（monitor 那一大段里讲 Step 3 的部分）

**Interfaces:** 无代码接口。

- [ ] **Step 1: 订正 Step 3 「重挂」的错误描述**

`CLAUDE.md` 里现在写着（monitor 段落靠后）：

> Surviving buys then go through `determine_order_price`; if the recomputed target price falls outside the configured 单价区间 ... otherwise it is **replaced/cancelled** if its price no longer matches the current tick.

`replaced` 是错的。`engine/monitor.py:1290` 起每个分支都是「撤单不重挂（下单引擎在下次 place_orders 重挂）」，Step 3 从不下单。把该句尾改成：

> otherwise it is **cancelled (never re-placed — the next `place_orders` round re-places it)** if its price no longer matches the current tick.

- [ ] **Step 2: 补记离场预取的不变量**

在同一段里描述 `check_exit` 的位置补一句（与 Step 3 预取那段的写法对齐）：

> `check_exit`/`check_low_balance` prefetch their per-position data 6-way through `parallel_map` before looping (`_exit_prefetch`): trades wave keyed by `condition_id` (a market's YES and NO positions share one `get_trades`), then orderbook wave keyed by `asset_id` — **trades first, books second, never reversed**, because the book drives the resting price and the B0 stop-loss trigger while the cost only moves when we ourselves fill. Prefetch workers do pure network reads and **must never touch `db`** (one sqlite connection per thread, never reclaimed). `None` in either table means *the fetch failed* and is not the same as an empty result: a failed trades fetch leaves the cost unknown, which skips the position with the ⚠️裸奔 warning row.

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: 订正 Step3 从不重挂 + 记录离场预取的不变量"
```

---

### 阶段 1 收尾：实测

阶段 1 到此结束。**在开阶段 2 之前先跑一晚，从日志里捞新周期：**

```bash
grep "\[tick\]" market_maker.log | tail -50
```

判据：

- 如果总计时长已经掉到 15 秒以内，阶段 2（Step 3 独立线程）的边际收益很小，多一条线程和多一个并发撤单面未必划算，**拿这个数据回去问用户还做不做**。
- 如果观测到的撤单延迟仍然远大于「总计 + 5 秒」，说明大头在 Polymarket 自己的传播延迟上，客户端再优化也没用，阶段 2 同样不该做。
- 如果 `离场` 那一段仍然是大头，先看是不是持仓数远超 6（并发度打满），考虑把 `_EXIT_MAX_WORKERS` 提到 8，而不是急着上阶段 2。

---

## 阶段 2：Step 3 独立节奏（**做之前先看上面的实测结论**）

### Task 7: 合规检查独立线程

**Files:**
- Modify: `config.py:33-43`（`ENGINE_DEFAULTS`）、`engine/manager.py`（`WalletWorker` 的 `start`/`stop`/线程管理）、`engine/monitor.py`（`publish_status` 加车道参数）
- Test: `tests/test_database.py`（引擎键契约）、`tests/test_manager.py`（新增 `class TestComplianceLane`）

**Interfaces:**
- Consumes: Task 4 的 `check_sell_orders()` 自取语义
- Produces: 设置键 `compliance_interval_sec`（默认 10）；`OrderMonitor.publish_status(lane=None)`；`WalletWorker._compliance_thread`

- [ ] **Step 1: 写失败的测试**

`tests/test_database.py` 里 `ENGINE_KEYS` 那个列表（约 34-38 行）加一项 `"compliance_interval_sec"`，并加一条断言：

```python
def test_compliance_interval_default():
    from config import ENGINE_DEFAULTS

    assert ENGINE_DEFAULTS["compliance_interval_sec"] == 10
```

`tests/test_manager.py` 新增：

```python
class TestComplianceLane:
    """Step3 走独立线程与独立周期,状态行不与主 tick 互相覆盖。"""

    def test_publish_status_uses_separate_lane_key(self):
        from engine.monitor import OrderMonitor
        from engine.monitor_status import get_snapshot, clear_snapshot

        clear_snapshot()
        api, db = MagicMock(), MagicMock()
        api.get_funder.return_value = "0xFUNDER"
        monitor = OrderMonitor(api, db, "0xABC")
        monitor.begin_status_tick()
        monitor._status_add(market="m1", side="买入", stage="Step1")
        monitor.publish_status()
        monitor.begin_status_tick()
        monitor._status_add(market="m2", side="买入", stage="Step3")
        monitor.publish_status(lane="step3")
        markets = {r.get("market") for r in get_snapshot()["rows"]}
        assert markets == {"m1", "m2"}  # 两条车道并存,不互相覆盖

    def test_compliance_thread_started_and_stopped(self):
        from engine.manager import WalletWorker

        api, db = MagicMock(), MagicMock()
        api.get_funder.return_value = "0xF"
        db.get_settings.return_value = {"compliance_interval_sec": 10}
        worker = WalletWorker(api, db, "0xW", {"fill_check_interval_sec": 5})
        worker.start()
        assert worker._compliance_thread is not None
        assert worker._compliance_thread.is_alive()
        worker.stop()
        worker._compliance_thread.join(timeout=5)
        assert not worker._compliance_thread.is_alive()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_manager.py::TestComplianceLane tests/test_database.py -q`
Expected: FAIL，`KeyError: 'compliance_interval_sec'`

- [ ] **Step 3: 实现**

`config.py` 的 `ENGINE_DEFAULTS` 里，`"fill_check_interval_sec": 5,` 之后加：

```python
    # Step3 合规复查(奖励下跌/价差走宽/档位变化 -> 撤单)的独立周期。它与持仓无关,
    # 不该被 O(持仓数) 的离场检查拖着走,故走自己的线程和节奏。
    "compliance_interval_sec": 10,
```

`engine/monitor.py` 的 `publish_status` 改成：

```python
    def publish_status(self, lane=None) -> None:
        """把本轮状态行发布到监控状态表。

        lane 区分车道:主 tick 传 None,Step3 独立线程传 "step3"。两条车道各自一个
        快照 key,否则后发布的那条会把另一条整份覆盖掉。get_snapshot 跨 key 合并、
        行内自带 wallet 字段,所以读侧和前端不用改。
        """
        try:
            key = self.wallet_address if lane is None else f"{self.wallet_address}#{lane}"
            monitor_status.set_snapshot(key, self._status_rows, self._tick_ts)
        except Exception as e:
            logger.warning("publish_status failed: %s", e)
```

`engine/manager.py` 的 `WalletWorker`：在 `__init__` 里加 `self._compliance_thread = None`；`start()` 里起主线程之后加：

```python
        self._compliance_thread = threading.Thread(
            target=self._run_compliance,
            daemon=True,
            name=f"compliance-{self.wallet_address[:8]}",
        )
        self._compliance_thread.start()
```

新增方法（放在 `_run` 之后）：

```python
    def _run_compliance(self):
        """Step3 合规复查的独立车道。

        它只依赖挂单/盘口/奖励,与持仓数无关,所以不该被 O(持仓数) 的离场检查拖着走。
        撤单是幂等的:撤一张已经撤掉的单只会拿到 already-canceled,现有代码是 WARNING
        加 return,不会刷屏。状态行走 "step3" 车道,不与主 tick 互相覆盖。
        """
        interval = self.db.get_settings().get("compliance_interval_sec", 10)
        while not self._stop_event.is_set():
            try:
                self._compliance_tick()
            except Exception as e:
                logger.exception(
                    "Compliance tick crashed for %s (continuing): %s",
                    self.wallet_address,
                    e,
                )
            self._stop_event.wait(timeout=interval)

    @_worker_proxied
    def _compliance_tick(self):
        self.monitor.begin_compliance_tick()  # 轻量入口,见下方 ⚠️
        self.monitor.check_sell_orders()
        self.monitor.publish_status(lane="step3")
```

从 `_tick` 里**删掉** `_step("合规", self.monitor.check_sell_orders)` 那一行，并把 Task 5 测试里的 `"合规"` 断言一并移除。

⚠️ **这是本 Task 最容易踩的坑：** `begin_status_tick` 会重置 `_cost_cache` / `_trades_by_cid` / `_book_cache` / `_just_dumped`，两条线程都调它就会互相清掉对方本轮的离场缓存，后果是 `check_exit` 中途丢失成本、误报 ⚠️裸奔。合规车道根本不用这些缓存，所以给它一个只重置状态行的轻量入口。

在 `engine/monitor.py` 的 `begin_status_tick` 之后新增：

```python
    def begin_compliance_tick(self) -> None:
        """合规车道的轻量 tick 入口:只重置状态行,**绝不碰离场缓存**。

        _cost_cache / _trades_by_cid / _book_cache / _just_dumped 属于主 tick 的离场
        判定,合规检查一个都用不到。两条线程都调 begin_status_tick 会互相清掉对方本轮
        的缓存,让 check_exit 中途丢失成本、误报 ⚠️裸奔。
        """
        self._status_rows = []
        self._tick_ts = time.time()
```

`_compliance_tick` 里调它，不要调 `begin_status_tick`：

```python
    @_worker_proxied
    def _compliance_tick(self):
        self.monitor.begin_compliance_tick()
        self.monitor.check_sell_orders()
        self.monitor.publish_status(lane="step3")
```

为这一点补一条测试：

```python
    def test_compliance_tick_does_not_clear_exit_caches(self):
        from engine.manager import WalletWorker

        api, db = MagicMock(), MagicMock()
        api.get_funder.return_value = "0xF"
        api.get_open_orders.return_value = []
        db.get_blacklist_ids.return_value = set()
        db.get_settings.return_value = {"rewards_cache_ttl_sec": 0}
        db.get_template_for.return_value = {}
        worker = WalletWorker(api, db, "0xW", {"fill_check_interval_sec": 5})
        worker.monitor.begin_status_tick()
        worker.monitor._cost_cache["tokA"] = (0.3, [])
        worker._compliance_tick()
        assert worker.monitor._cost_cache == {"tokA": (0.3, [])}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add config.py engine/manager.py engine/monitor.py tests/
git commit -m "perf(monitor): Step3 合规复查走独立线程与独立周期"
```

---

## 阶段 3：补 `rewards_min_size` 复查（与前两阶段互不依赖，可随时插队）

### Task 8: `_market_rewards` 多返回一项 `rewards_min_size`

**Files:**
- Modify: `engine/rewards.py`（新增 `extract_min_size`）、`engine/monitor.py:51`（缓存注释）、`engine/monitor.py:962-992`（`_market_rewards`）、`engine/monitor.py:998`（docstring）、`engine/monitor.py:1159`（解包）
- Test: `tests/test_rewards.py`（新增 `extract_min_size` 用例）、`tests/test_monitor.py`（更新 12 处断言）

**Interfaces:**
- Consumes: 无
- Produces: `engine.rewards.extract_min_size(rewards_items) -> int | None`；`_market_rewards(cid, ttl) -> tuple[float|None, float|None, int|None]`（**从二元组变三元组**）

**背景（实测）：** `/rewards/markets/{cid}` 的响应项顶层同时带 `rewards_min_size` 和 `rewards_max_spread`（2026-07-28 实测：单条 item，`rewards_min_size = 200`、`rewards_max_spread = 4.5`）。所以这一项**零新增请求**。

**必须一并更新的现有断言**（从二元组改三元组）：`tests/test_monitor.py` 的 1795、1799、1805、1810、2101、2131、2142、2162 行。

- [ ] **Step 1: 写失败的测试**

`tests/test_rewards.py` 新增：

```python
from engine.rewards import extract_min_size


class TestExtractMinSize:
    def test_reads_top_level_field(self):
        assert extract_min_size([{"rewards_min_size": 200}]) == 200

    def test_string_value_is_coerced(self):
        assert extract_min_size([{"rewards_min_size": "200"}]) == 200

    def test_missing_field_is_none(self):
        assert extract_min_size([{"rewards_max_spread": 3}]) is None

    def test_empty_list_is_none(self):
        assert extract_min_size([]) is None

    def test_unparsable_is_none(self):
        assert extract_min_size([{"rewards_min_size": "abc"}]) is None

    def test_zero_is_none_not_zero(self):
        # 0 份额没有意义,当成「取不到」处理,免得被当成一个真实档位去匹配
        assert extract_min_size([{"rewards_min_size": 0}]) is None
```

`tests/test_monitor.py` 的 `class TestMarketRewards` 里把上述行改成三元组，例如 1795 行：

```python
        assert monitor._market_rewards("cid1", 0) == (3.0, 120.0, 200)
```

（对应的 `api.get_rewards_for_market.return_value` 里补上 `"rewards_min_size": 200`。1799/1805 行的「取不到」用例改成 `(None, None, None)`，1810 行改成 `(4.0, None, None)`。`TestStep3Prefetch` 的 2101/2162 行改成 `(3.0, 120.0, 200)`，2131/2142 行改成 `(None, None, None)`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_rewards.py tests/test_monitor.py -q`
Expected: FAIL，`ImportError: cannot import name 'extract_min_size'`

- [ ] **Step 3: 实现**

`engine/rewards.py` 末尾加：

```python
def extract_min_size(rewards_items: list) -> Optional[int]:
    """Parse rewards_min_size from get_rewards_for_market()'s return.

    与 rewards_max_spread 同在每个 item 的顶层(2026-07-28 对线上响应实测)。返回该
    市场的最低奖励份额,取不到 / 无法解析 / 非正数一律 None。调用方把 None 当成
    「判断不了」安全跳过,绝不当成一个真实档位去匹配。
    """
    for it in rewards_items or []:
        if not isinstance(it, dict):
            continue
        v = it.get("rewards_min_size")
        if v is None:
            continue
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return None
```

`engine/monitor.py`：顶部 import 处补 `extract_min_size`；51 行的注释改成 `# condition_id -> ((max_spread, daily_rate, min_size), fetched_at) TTL cache for Step 3.`；`_market_rewards` 的返回类型标注改成 `tuple[float | None, float | None, int | None]`，把

```python
            pair = (extract_max_spread(items), extract_daily_rate(items))
```

改成

```python
            pair = (
                extract_max_spread(items),
                extract_daily_rate(items),
                extract_min_size(items),
            )
```

998 行 docstring 里的 `rewards = {condition_id: (max_spread, daily_rate)}` 改成 `{condition_id: (max_spread, daily_rate, min_size)}`；1159 行改成：

```python
        max_spread, daily_rate, min_size = rewards.get(cid, (None, None, None))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add engine/rewards.py engine/monitor.py tests/test_rewards.py tests/test_monitor.py
git commit -m "feat(monitor): 奖励响应多解析一项 rewards_min_size(零新增请求)"
```

---

### Task 9: Step 3 加「档位不再匹配就撤」闸门

**Files:**
- Modify: `engine/monitor.py`（`_check_compliance` 里，紧接在奖励额闸门之后）
- Test: `tests/test_monitor.py`（新增 `class TestStep3MinSizeChange`）

**Interfaces:**
- Consumes: Task 8 的 `min_size`；`engine.tiers.tier_for(size_tiers, min_size)`
- Produces: 新 action_type `min_size_change_cancel`

**闸门位置：** 放在奖励额闸门（`reward_drop_cancel`，约 1170 行）之后、「盘口为空跳过」（约 1208 行）之前。理由与奖励额闸门相同：档位判定根本用不到盘口，而没有卖单的在挂买单照样会被别人的市价卖吃掉。

- [ ] **Step 1: 写失败的测试**

```python
class TestStep3MinSizeChange:
    """份额要求变了导致档位不再匹配 -> 撤买单不重挂(零新增请求,与奖励复查同一份响应)。"""

    def _order(self):
        return {
            "id": "o1",
            "side": "BUY",
            "asset_id": "tokA",
            "market": "cid1",
            "size_matched": "0",
            "price": "0.30",
            "original_size": "500",
        }

    def _ob(self):
        return {
            "bids": [{"price": "0.30", "size": "1000"}],
            "asks": [{"price": "0.31", "size": "1000"}],
            "tick_size": "0.01",
        }

    def _settings(self, sizes):
        return {
            "min_reward_usd": 0,
            "max_spread_cents": 100,
            "min_price_cents": 0,
            "max_price_cents": 100,
            "size_tiers": [{"size": s, "enabled": True, "shares": s} for s in sizes],
        }

    def test_cancels_when_tier_no_longer_matches(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._check_compliance(
            self._order(),
            set(),
            self._settings([100]),
            {"tokA": self._ob()},
            {"cid1": (3.0, 500.0, 200)},   # 份额要求变成 200,已启用档位只有 100
        )
        api.cancel_orders.assert_called_once_with(["o1"])
        assert db.record_action.called
        assert db.record_action.call_args.kwargs["action_type"] == "min_size_change_cancel"

    def test_keeps_when_tier_still_matches(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._check_compliance(
            self._order(),
            set(),
            self._settings([200]),
            {"tokA": self._ob()},
            {"cid1": (3.0, 500.0, 200)},
        )
        api.cancel_orders.assert_not_called()

    def test_skips_when_min_size_unavailable(self):
        # 取不到 -> 不撤(绝不 fail-close),下轮重试
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._check_compliance(
            self._order(),
            set(),
            self._settings([100]),
            {"tokA": self._ob()},
            {"cid1": (3.0, 500.0, None)},
        )
        api.cancel_orders.assert_not_called()

    def test_gate_runs_before_empty_book_skip(self):
        # 盘口没有卖单的市场,在挂买单照样会被市价卖吃掉,所以档位闸门要排在空盘口跳过之前
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        monitor._check_compliance(
            self._order(),
            set(),
            self._settings([100]),
            {"tokA": {"bids": [{"price": "0.30", "size": "1"}], "asks": []}},
            {"cid1": (None, 500.0, 200)},
        )
        api.cancel_orders.assert_called_once_with(["o1"])

    def test_failed_cancel_records_nothing(self):
        monitor, api, db = _make_monitor()
        monitor.begin_status_tick()
        api.cancel_orders.side_effect = RuntimeError("already canceled")
        monitor._check_compliance(
            self._order(),
            set(),
            self._settings([100]),
            {"tokA": self._ob()},
            {"cid1": (3.0, 500.0, 200)},
        )
        assert not db.record_action.called
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_monitor.py::TestStep3MinSizeChange -q`
Expected: FAIL，`test_cancels_when_tier_no_longer_matches` 报 `cancel_orders` 没被调用

- [ ] **Step 3: 实现**

`engine/monitor.py` 顶部 import 处补 `from engine.tiers import tier_for`。在 `_check_compliance` 里，奖励额闸门那个 `return` 之后、`# --- 奖励区间合规检查` 那行注释之前，插入：

```python
        # 份额要求实时复查:市场的 rewards_min_size 变了、已启用档位里不再有对应模块 ->
        # 撤买单不重挂。与奖励额闸门同一份响应,零新增请求。
        #
        # 排在「盘口为空跳过」之前,理由与奖励额闸门相同:档位判定用不到盘口,而没有卖单的
        # 在挂买单照样会被别人的市价卖吃掉。
        #
        # None 表示取不到,跳过本轮、绝不撤(fail-open);这里绝不能写成 falsy 判断。
        # 判据复用下单时那一套 tier_for(精确匹配 enabled 且 size 相等),两处口径不会走偏。
        if min_size is not None and tier_for(settings.get("size_tiers"), min_size) is None:
            reason = f"最低奖励份额变为 {min_size}，已启用档位无匹配模块，撤买单不重挂"
            try:
                self.api.cancel_orders([o.get("id")])
            except Exception as e:
                logger.warning("Min-size cancel %s failed: %s", o.get("id"), e)
                return
            self._record_action(
                market_id=cid,
                action_type="min_size_change_cancel",
                side="-",
                price=-1,
                size=osize,
                reason=reason,
                price_basis=(
                    f"实时最低奖励份额={min_size}；已启用档位="
                    f"{sorted(enabled_sizes(settings.get('size_tiers')))}；"
                    f"来源：CLOB /rewards/markets/{cid} 实时取数"
                ),
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{cur_price:.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="撤单(份额要求变化)",
                detail=reason,
            )
            logger.info(
                "[Step3] min-size cancel %s market %s: %s", o.get("id"), cid, reason
            )
            return
```

import 处一并补 `enabled_sizes`（与 `tier_for` 同一模块）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_monitor.py::TestStep3MinSizeChange -q`
Expected: 5 passed

Run: `pytest -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat(monitor): Step3 复查 rewards_min_size,档位不再匹配即撤买单"
```

---

### Task 10: 文档与发版说明

**Files:**
- Modify: `CLAUDE.md`（Step 3 复查项那一段）

- [ ] **Step 1: 补记新闸门**

`CLAUDE.md` 里描述 Step 3 复查顺序的地方，在 reward-drop 那一段之后补：

> The market's **`rewards_min_size`** is then re-checked from that same rewards response (zero extra requests — it sits at the response item's top level next to `rewards_max_spread`): if `tier_for(size_tiers, min_size)` no longer matches an enabled module the buy is cancelled cancel-only (`min_size_change_cancel`, no replace). `None` skips (couldn't determine — never fail-close). This closes a real hole: `rewards_min_size` was previously judged **only** at discovery (4 h) and at placement off the pool snapshot, so a market that changed its size requirement while keeping its daily reward above the floor kept a resting buy on a non-matching tier for up to 4 hours.

- [ ] **Step 2: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md 记录 Step3 的份额要求复查闸门"
```

---

## 发版说明要点（合并后写 RELEASE_NOTES.md 时用）

- 撤单和止损的响应都变快了，原因是逐仓取数改成了并发。没有任何交易判定改变。
- **新增的份额要求复查是行为改变**：以前份额要求变了、奖励还够，买单最长会在不匹配的档位上挂 4 小时；现在几十秒内就撤掉（cancel-only，不重挂，下一轮下单照常）。发版说明里要写清楚，这会让一部分人看到「撤单变多了」。
- 阶段 2 若实施，新增设置键 `compliance_interval_sec`（默认 10 秒）。
