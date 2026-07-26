# Step3 并发预取 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Step3 逐单串行的盘口/奖励取数改成 6 路并发预取，判定仍串行，让单钱包一轮从 36 秒量级降到 6 秒量级，从而把被它拖累的止损间隔拉回同一量级。

**Architecture:** `check_sell_orders` 拆成三步——主线程读一次 DB（黑名单、模板、TTL）、6 路并发预取两张表（`{token_id: 盘口}`、`{condition_id: (max_spread, daily_rate)}`）、主线程照原逻辑串行判定。线程池里只做纯网络读，DB 访问和所有副作用（撤单、记账、状态行）留在主线程，因为每线程一条 sqlite 连接且建了不回收。代理 contextvar 不会自动传进 worker，复用现有的 `_parallel_map`（上提为 `api/proxy.parallel_map`）来带。

**Tech Stack:** Python 3、`concurrent.futures.ThreadPoolExecutor`、`contextvars`、pytest、`unittest.mock.MagicMock`。

设计文档：`docs/superpowers/specs/2026-07-26-step3-parallel-prefetch-design.md`

## Global Constraints

- **判定逻辑与判定顺序一字不改。** 奖励闸门仍在空盘口守卫之前，`midpoint` 仍在其之后。这次只改「数据从哪来」，不改「拿到数据怎么办」。
- **绝不 fail-close。** 取不到数据就跳过该单，绝不因为取数失败而撤单。
- **绝不直连。** worker 线程必须带上调用线程的 `current_proxy`；`ThreadPoolExecutor` 默认不继承 contextvar，漏了就是拿真实 IP 去请求，违反 IP 隔离铁律。
- **worker 线程不访问 `db`、不写状态行、不发撤单请求。** 每线程一条 sqlite 连接且永久留在 `Database._connections` 里从不回收（`models/database.py:17-30`），Step3 每 5 秒一轮，worker 碰 db 就是无限泄漏连接。
- **`books` 里的 `None` 和「盘口为空」是两件事。** `None` = 取数失败（该单跳过、不留状态行，等价于旧的异常路径）；取到了但 bids/asks 为空 = 走「跳过(盘口为空)」分支并留状态行。塌成一个会让监控状态表说谎。同理 `daily_rate` 的 `0.0`（奖励真归零，要撤单）与 `None`（取不到，跳过）不可合并。
- Step3 并发度是模块常量 `_STEP3_MAX_WORKERS = 6`，不进配置页。发现阶段的 `_DISCOVERY_MAX_WORKERS = 4` 保持不动。
- 撤单仍然串行，仍然「撤不掉就 WARNING + return」。
- Chinese 注释/字符串必须逐字照抄本计划，不得改写或用形近字替代。文件保持 UTF-8 无 BOM。
- 每个任务只 stage 它自己列出的文件，绝不 `git add -A`。commit message 结尾附一行 `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`。

## File Structure

- `api/proxy.py`（修改）：接收从 scanner 上提来的 `parallel_map`。这个函数存在的唯一理由是代理 contextvar 传播，属于代理层。
- `engine/scanner.py`（修改）：删掉 `_parallel_map` 定义，改用 `api.proxy.parallel_map` 并在两个调用点显式传 `_DISCOVERY_MAX_WORKERS`。
- `engine/monitor.py`（修改）：新增 `_STEP3_MAX_WORKERS` 常量与 `_prefetch` 方法；`_market_rewards` 改为接收 ttl；`check_sell_orders` 与 `_check_compliance` 改用预取数据；删除 `_round_market_rewards` 与 `_round_rewards`。
- `tests/test_proxy.py`（修改）：接收从 `tests/test_scanner.py` 挪来的 `TestParallelMap`。
- `tests/test_scanner.py`（修改）：移出 `TestParallelMap`。
- `tests/test_monitor.py`（修改）：`_market_rewards` / `_check_compliance` 的调用适配，新增预取与语义守护测试。

---

### Task 1: `parallel_map` 上提到 `api/proxy.py`

纯搬迁，行为不变。`max_workers` 从默认参数改为必传——两个调用方要的值不同（发现阶段 4、Step3 6），留默认值只会误导。

**Files:**
- Modify: `api/proxy.py`（加 import + 在 `use_proxy` 之后插入函数）
- Modify: `engine/scanner.py:24`（import）、`:35`（import）、`:39-62`（删定义）、`:186`、`:305`（注释）、`:345`（注释）、`:348`
- Test: `tests/test_proxy.py`（接收测试类）、`tests/test_scanner.py:726-753`（移出）

**Interfaces:**
- Consumes: `api.proxy.current_proxy`（已存在的 ContextVar）
- Produces: `parallel_map(func, items, max_workers) -> list` —— 并发对 items 跑 func，结果按输入序返回；空 items 返回 `[]` 且不建线程池；每个 worker 内 `current_proxy` 等于调用线程的值。

- [ ] **Step 1: 把测试挪到新家并改成新接口**

从 `tests/test_scanner.py` 删除整个 `class TestParallelMap`（第 726-753 行区块，含前面的空行），把下面这个版本追加到 `tests/test_proxy.py` 末尾：

```python
class TestParallelMap:
    def test_preserves_order(self):
        from api.proxy import parallel_map

        assert parallel_map(lambda x: x * 2, [1, 2, 3, 4], 4) == [2, 4, 6, 8]

    def test_empty_items(self):
        from api.proxy import parallel_map

        assert parallel_map(lambda x: x, [], 4) == []

    def test_propagates_current_proxy_into_workers(self):
        # 关键:新线程默认 current_proxy=None(会直连、泄露真实 IP);助手必须把调用
        # 线程的代理带进每个 worker,否则并行化就破了 IP 隔离铁律。
        from api.proxy import parallel_map, current_proxy, use_proxy

        with use_proxy("http://user:pass@host:9999"):
            seen = parallel_map(lambda x: current_proxy.get(), list(range(8)), 4)
        assert seen == ["http://user:pass@host:9999"] * 8

    def test_no_proxy_stays_none(self):
        from api.proxy import parallel_map, current_proxy

        assert parallel_map(lambda x: current_proxy.get(), [1, 2], 4) == [None, None]

    def test_max_workers_is_required(self):
        # 必传:发现阶段要 4、Step3 要 6,留默认值会让调用方拿到不合适的并发度。
        import pytest
        from api.proxy import parallel_map

        with pytest.raises(TypeError):
            parallel_map(lambda x: x, [1])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_proxy.py -v`
Expected: FAIL，`ImportError: cannot import name 'parallel_map' from 'api.proxy'`

- [ ] **Step 3: 在 `api/proxy.py` 加实现**

`api/proxy.py:9-13` 的 import 块加一行（放在 `import contextvars` 之后、`import threading` 之前，保持字母序无关的现有顺序即可）：

```python
from concurrent.futures import ThreadPoolExecutor
```

在 `use_proxy` 函数之后（`api/proxy.py:32` 那行空行之后）插入：

```python
def parallel_map(func, items, max_workers):
    """并发对 items 跑 func,结果按输入序返回。

    关键:ThreadPoolExecutor 的 worker 线程默认 current_proxy=None(会直连、泄露真实
    IP,违反「绝不直连」铁律)。这里捕获**调用线程**的 current_proxy,在每个 worker 里
    设回,保住代理 IP 隔离。func 应自带异常处理(否则异常在收结果时抛出)。

    max_workers 必传:各调用方的合适值不同(发现阶段 4、Step3 6),留默认值会误导。
    """
    items = list(items)
    if not items:
        return []
    proxy = current_proxy.get()

    def _task(item):
        token = current_proxy.set(proxy)
        try:
            return func(item)
        finally:
            current_proxy.reset(token)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as ex:
        return list(ex.map(_task, items))
```

- [ ] **Step 4: 改 `engine/scanner.py` 用新函数**

删除 `engine/scanner.py` 里整个 `_parallel_map` 函数定义（第 42-62 行，即从 `def _parallel_map(` 到 `return list(ex.map(_task, items))` 及其后的空行）。保留 `_DISCOVERY_MAX_WORKERS = 4` 那一行和 `from concurrent.futures import ThreadPoolExecutor, as_completed`（文件其它地方仍直接用它们）。

`engine/scanner.py:35` 的 import 改成：

```python
from api.proxy import use_proxy, current_proxy, parallel_map
```

第 186 行改成：

```python
        slug_rows = dict(_parallel_map(_tag_slug, slugs_needed))
```
→
```python
        slug_rows = dict(
            parallel_map(_tag_slug, slugs_needed, _DISCOVERY_MAX_WORKERS)
        )
```

第 348 行改成：

```python
        results = _parallel_map(lambda m: self._fetch_orderbooks(m), pool)
```
→
```python
        results = parallel_map(
            lambda m: self._fetch_orderbooks(m), pool, _DISCOVERY_MAX_WORKERS
        )
```

两处注释里提到旧名字的也要跟上：第 305 行 `代理隔离同 _parallel_map。` 改成 `代理隔离同 parallel_map。`；第 345 行 `_parallel_map 保序 + 带 current_proxy。` 改成 `parallel_map 保序 + 带 current_proxy。`

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_proxy.py tests/test_scanner.py -v`
Expected: PASS。`TestParallelMap` 在新家全绿，scanner 的测试不回归。

再确认没有残留引用：

Run: `grep -rn "_parallel_map" --include=*.py .`
Expected: 无输出

- [ ] **Step 6: 全量回归并提交**

Run: `python -m pytest -q`
Expected: 801 passed（改动前 800，本任务净增 1 个 `test_max_workers_is_required`）

```bash
git add api/proxy.py engine/scanner.py tests/test_proxy.py tests/test_scanner.py
git commit -m "$(cat <<'EOF'
refactor: parallel_map 上提到 api/proxy,max_workers 改必传

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `_market_rewards` 改为接收 ttl，不再自读 DB

为 Task 3 的并发预取铺路：worker 线程绝不能碰 `db`，所以 TTL 由主线程读好传进来。

**Files:**
- Modify: `engine/monitor.py:949-972`（`_market_rewards`）、`:974-987`（`_round_market_rewards` 过渡改动）
- Test: `tests/test_monitor.py:1755-1811`（`TestMarketRewards` 的调用适配）

**Interfaces:**
- Consumes: 无
- Produces: `OrderMonitor._market_rewards(condition_id: str, ttl: float) -> tuple[float|None, float|None]` —— 返回 `(max_spread_cents, daily_rate_usd)`；`ttl` 由调用方传入，方法内不再访问 `self.db`。

- [ ] **Step 1: 改测试到新签名（会先失败）**

`tests/test_monitor.py` 的 `TestMarketRewards` 里，把 7 处调用改成显式传 ttl。逐处改法：

```python
    def test_returns_max_spread_and_daily_rate(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = self._payload()
        assert monitor._market_rewards("cid1", 0) == (3.0, 120.0)

    def test_empty_condition_id_returns_none_pair(self):
        monitor, api, db = _make_monitor()
        assert monitor._market_rewards("", 0) == (None, None)
        api.get_rewards_for_market.assert_not_called()

    def test_api_failure_returns_none_pair(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.side_effect = RuntimeError("network")
        assert monitor._market_rewards("cid1", 0) == (None, None)

    def test_missing_rewards_config_gives_none_rate(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 4}]
        assert monitor._market_rewards("cid1", 0) == (4.0, None)

    def test_cached_within_ttl_fetches_once(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = self._payload()
        monitor._market_rewards("cid1", 600)
        monitor._market_rewards("cid1", 600)
        assert api.get_rewards_for_market.call_count == 1

    def test_ttl_zero_refetches_every_call(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = self._payload()
        monitor._market_rewards("cid1", 0)
        monitor._market_rewards("cid1", 0)
        assert api.get_rewards_for_market.call_count == 2

    def test_nothing_parsable_is_not_cached(self):
        # 一无所获不写缓存,下轮重试(沿用旧 _market_max_spread 的语义)
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = [{"condition_id": "cid1"}]
        monitor._market_rewards("cid1", 600)
        monitor._market_rewards("cid1", 600)
        assert api.get_rewards_for_market.call_count == 2

    def test_rate_present_but_max_spread_missing_not_cached(self):
        # max_spread 取不到就不写缓存(与旧 _market_max_spread 一致);rate 有值也不例外。
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = [
            {"rewards_config": [{"rate_per_day": 120}]}
        ]
        monitor._market_rewards("cid1", 600)
        monitor._market_rewards("cid1", 600)
        assert api.get_rewards_for_market.call_count == 2
```

注意 `_make_monitor({"rewards_cache_ttl_sec": ...})` 的参数不再需要（TTL 现在从调用参数来），上面已按无参形式写好；不要保留会误导读者的旧 settings 覆盖。

再加一个测试，钉住「不碰 db」这条硬约束——它是整个方案能不泄漏 sqlite 连接的前提：

```python
    def test_does_not_touch_db(self):
        # worker 线程会调它,而每线程一条 sqlite 连接且建了不回收 —— 碰 db 就是
        # 每 5 秒一轮地泄漏连接。
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = self._payload()
        db.reset_mock()
        monitor._market_rewards("cid1", 0)
        assert db.method_calls == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_monitor.py::TestMarketRewards -v`
Expected: FAIL，`TypeError: _market_rewards() takes 2 positional arguments but 3 were given`

- [ ] **Step 3: 改实现**

`engine/monitor.py:949` 的方法签名与 docstring 换成：

```python
    def _market_rewards(
        self, condition_id: str, ttl: float
    ) -> tuple[float | None, float | None]:
        """(rewards_max_spread 美分, 每日奖励美元),同一次响应解析,TTL 缓存。

        ttl 由调用方(主线程)读好传入,**本方法绝不碰 db**:Step3 的并发预取会在 worker
        线程里调它,而每线程一条 sqlite 连接、建了从不回收(models/database.py),worker
        里访问 db 就是每 5 秒一轮地泄漏连接。

        任一项为 None = 该项取不到(接口失败/字段缺失),调用方各自安全跳过。
        每日奖励的 0.0 与 None 含义不同:0.0=奖励真归零(要撤单),None=取不到(跳过)。
        max_spread 保持 float(不 int 化):实盘存在 3.5/4.5 美分,截断会缩窄奖励区间。
        """
```

然后删掉方法体里这一行：

```python
        ttl = self.db.get_settings()["rewards_cache_ttl_sec"]
```

方法体其余部分（`if not condition_id`、`now = time.time()`、缓存命中判断、取数、解析、写缓存）一律不动。

`engine/monitor.py:974` 的 `_round_market_rewards` 现在要自己读 ttl 传下去。把它的方法体改成：

```python
        if condition_id in self._round_rewards:
            return self._round_rewards[condition_id]
        ttl = self.db.get_settings()["rewards_cache_ttl_sec"]
        pair = self._market_rewards(condition_id, ttl)
        self._round_rewards[condition_id] = pair
        return pair
```

（这是过渡形态：Task 4 会把 `_round_market_rewards` 整个删掉，ttl 改由 `check_sell_orders` 读一次。）

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_monitor.py -v`
Expected: PASS。`TestMarketRewards` 全绿，Step3 的其它测试组不回归。

- [ ] **Step 5: 全量回归并提交**

Run: `python -m pytest -q`
Expected: 802 passed（上一任务后 801，本任务净增 1 个 `test_does_not_touch_db`）

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "$(cat <<'EOF'
refactor: _market_rewards 接收 ttl,不再自读 db

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `_prefetch` 并发预取方法

新增方法，本任务暂无调用方（Task 4 接线）。

**Files:**
- Modify: `engine/monitor.py`（模块常量区加 `_STEP3_MAX_WORKERS`；import 加 `parallel_map`；在 `_market_rewards` 之后插入 `_prefetch`）
- Test: `tests/test_monitor.py`（新增 `TestStep3Prefetch`）

**Interfaces:**
- Consumes: Task 1 的 `api.proxy.parallel_map(func, items, max_workers)`；Task 2 的 `_market_rewards(condition_id, ttl)`
- Produces: `OrderMonitor._prefetch(open_orders: list, blacklist: set, ttl: float) -> tuple[dict, dict]` —— 返回 `(books, rewards)`，`books = {token_id: 盘口 dict 或 None}`，`rewards = {condition_id: (max_spread, daily_rate)}`。只为「真要判定的买单」取数。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_monitor.py` 末尾：

```python
class TestStep3Prefetch:
    """Step3 本轮盘口/奖励的并发预取:按 token/市场去重、逐项容错、带钱包代理。"""

    def _buy(self, oid, token, cid, matched="0"):
        return {
            "id": oid,
            "side": "BUY",
            "asset_id": token,
            "market": cid,
            "size_matched": matched,
            "price": "0.30",
            "original_size": "500",
        }

    def _ob(self):
        return {
            "bids": [{"price": "0.30", "size": "1000"}],
            "asks": [{"price": "0.31", "size": "1000"}],
            "tick_size": "0.01",
        }

    def _ready(self, monitor, api):
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 120}]}
        ]

    def test_dedups_orderbook_by_token(self):
        # 同一 token 上两笔买单只取一次盘口
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        orders = [self._buy("o1", "tokA", "cid1"), self._buy("o2", "tokA", "cid1")]
        books, rewards = monitor._prefetch(orders, set(), 0)
        assert api.get_orderbook.call_count == 1
        assert set(books) == {"tokA"}
        assert books["tokA"] == self._ob()

    def test_dedups_rewards_by_market(self):
        # 同市场 YES/NO 两侧:盘口按 token 取两次,奖励按市场只取一次
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        orders = [self._buy("o1", "tokYes", "cid1"), self._buy("o2", "tokNo", "cid1")]
        books, rewards = monitor._prefetch(orders, set(), 0)
        assert api.get_orderbook.call_count == 2
        assert api.get_rewards_for_market.call_count == 1
        assert rewards == {"cid1": (3.0, 120.0)}

    def test_orderbook_failure_becomes_none(self):
        # 取数失败记 None(调用方据此跳过该单),绝不外抛、绝不拖垮其它单
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        api.get_orderbook.side_effect = RuntimeError("network")
        books, rewards = monitor._prefetch([self._buy("o1", "tokA", "cid1")], set(), 0)
        assert books == {"tokA": None}

    def test_one_failure_does_not_affect_others(self):
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)

        def _ob_or_boom(token_id):
            if token_id == "tokBad":
                raise RuntimeError("network")
            return self._ob()

        api.get_orderbook.side_effect = _ob_or_boom
        orders = [self._buy("o1", "tokBad", "cid1"), self._buy("o2", "tokOk", "cid2")]
        books, rewards = monitor._prefetch(orders, set(), 0)
        assert books["tokBad"] is None
        assert books["tokOk"] == self._ob()

    def test_rewards_failure_becomes_none_pair(self):
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        api.get_rewards_for_market.side_effect = RuntimeError("network")
        books, rewards = monitor._prefetch([self._buy("o1", "tokA", "cid1")], set(), 0)
        assert rewards == {"cid1": (None, None)}

    def test_skips_blacklisted_market(self):
        # 黑名单单在判定里于取数之前就 return,不必为它发请求
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        books, rewards = monitor._prefetch(
            [self._buy("o1", "tokA", "cid1")], {"cid1"}, 0
        )
        api.get_orderbook.assert_not_called()
        api.get_rewards_for_market.assert_not_called()
        assert (books, rewards) == ({}, {})

    def test_skips_sell_orders_and_partially_filled(self):
        # 卖单和部分成交单不进判定,不为它们取数
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        orders = [
            {
                "id": "s1",
                "side": "SELL",
                "asset_id": "tokS",
                "market": "cidS",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
            },
            self._buy("o2", "tokP", "cidP", matched="100"),
        ]
        books, rewards = monitor._prefetch(orders, set(), 0)
        api.get_orderbook.assert_not_called()
        assert (books, rewards) == ({}, {})

    def test_no_orders_makes_no_requests(self):
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        books, rewards = monitor._prefetch([], set(), 0)
        assert (books, rewards) == ({}, {})
        api.get_orderbook.assert_not_called()
        api.get_rewards_for_market.assert_not_called()

    def test_carries_wallet_proxy_into_workers(self):
        # worker 线程默认 current_proxy=None 会直连、泄露真实 IP;预取必须把调用线程
        # 的代理带进去,否则并发化就破了 IP 隔离铁律。
        from api.proxy import current_proxy, use_proxy

        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        seen = []

        def _ob_recording(token_id):
            seen.append(current_proxy.get())
            return self._ob()

        api.get_orderbook.side_effect = _ob_recording
        with use_proxy("http://user:pass@host:9999"):
            monitor._prefetch([self._buy("o1", "tokA", "cid1")], set(), 0)
        assert seen == ["http://user:pass@host:9999"]

    def test_does_not_touch_db(self):
        # 硬约束:worker 碰 db 会让每 5 秒一轮的 Step3 无限泄漏 sqlite 连接
        monitor, api, db = _make_monitor()
        self._ready(monitor, api)
        db.reset_mock()
        monitor._prefetch([self._buy("o1", "tokA", "cid1")], set(), 0)
        assert db.method_calls == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_monitor.py::TestStep3Prefetch -v`
Expected: FAIL，`AttributeError: 'OrderMonitor' object has no attribute '_prefetch'`

- [ ] **Step 3: 写实现**

`engine/monitor.py:20` 之后（import 块末尾、`from engine import monitor_status` 那行之后）加：

```python
from api.proxy import parallel_map
```

`engine/monitor.py:27`（`LIQUIDATE_COOLDOWN_SEC = 60` 那行）之后加：

```python

# Step3 预取并发上限。发现阶段用 4(实测奖励端点/代理娇气);Step3 只打盘口和奖励两个
# 轻接口、不跑分页,略高一档。5 钱包各开各的,单个代理承受的并发就是这个数。要回退
# 并发化只改这一个常量。
_STEP3_MAX_WORKERS = 6
```

在 `_market_rewards` 方法之后（`_round_market_rewards` 之前）插入：

```python
    def _prefetch(self, open_orders, blacklist, ttl):
        """Step3 本轮的盘口 / 奖励并发预取,返回 (books, rewards) 两张表。

        books   = {token_id: 订单簿 dict 或 None}
        rewards = {condition_id: (max_spread, daily_rate)}

        **books 里的 None 表示取数失败**,与「取到了但买卖盘为空」不是一回事:前者该单
        本轮跳过(等价于旧的取数抛异常路径、不留状态行),后者要走「盘口为空」分支并留
        状态行。两者塌成一个会让监控状态表说谎。

        只为「真要判定的买单」取数:卖单和部分成交单不进判定,黑名单市场的单在取数之前
        就 return,都不必为它们发请求。worker 线程只发网络请求、绝不碰 db(每线程一条
        sqlite 连接且建了不回收),每个任务自带异常处理、绝不外抛。
        """
        pending = [
            o
            for o in open_orders
            if o.get("side") == "BUY"
            and float(o.get("size_matched", 0) or 0) <= 0
            and o.get("market", "") not in blacklist
        ]
        token_ids = [t for t in {o.get("asset_id", "") for o in pending} if t]
        cids = [c for c in {o.get("market", "") for o in pending} if c]

        def _book(token_id):
            try:
                return self.api.get_orderbook(token_id)
            except Exception as e:
                logger.warning("[Step3] 预取订单簿失败 %s: %s", token_id, e)
                return None

        books = dict(
            zip(token_ids, parallel_map(_book, token_ids, _STEP3_MAX_WORKERS))
        )
        rewards = dict(
            zip(
                cids,
                parallel_map(
                    lambda c: self._market_rewards(c, ttl), cids, _STEP3_MAX_WORKERS
                ),
            )
        )
        return books, rewards
```

`_market_rewards` 已自带异常处理（失败返回 `(None, None)`），所以奖励那一路不需要再包 try。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_monitor.py -v`
Expected: PASS。`TestStep3Prefetch` 全绿；既有测试组不受影响（本任务还没有人调 `_prefetch`）。

- [ ] **Step 5: 全量回归并提交**

Run: `python -m pytest -q`
Expected: 812 passed（上一任务后 802，本任务新增 10 个）

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "$(cat <<'EOF'
feat: Step3 盘口/奖励并发预取方法(6 路,带钱包代理)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: 接线 —— `check_sell_orders` 用预取，`_check_compliance` 只判定

**Files:**
- Modify: `engine/monitor.py:56-60`（删 `_round_rewards` 初始化）、`:908-947`（`check_sell_orders`）、`:974-987`（删 `_round_market_rewards`）、`:989-1026`（`_check_compliance` 签名与取数）、奖励取数行
- Test: `tests/test_monitor.py:1109-1122`（`_check_compliance` 调用适配）、新增语义守护测试

**Interfaces:**
- Consumes: Task 3 的 `_prefetch(open_orders, blacklist, ttl) -> (books, rewards)`
- Produces: `OrderMonitor._check_compliance(o: dict, blacklist: set, settings: dict, books: dict, rewards: dict) -> None` —— 只判定，不发取数请求。

- [ ] **Step 1: 写失败的测试**

先把 `tests/test_monitor.py` 里 `TestStep3Blacklist` 的直接调用改成新签名（第 1120 行）：

```python
        monitor._check_compliance(o, {"cid1"}, {}, {}, {})
```

然后追加到 `tests/test_monitor.py` 末尾：

```python
class TestStep3PrefetchWiring:
    """接线后的语义等价:取数失败 vs 盘口为空是两回事;DB 读收到主线程一次。"""

    def _buy(self, oid="o1", token="tokA", cid="cid1"):
        return {
            "id": oid,
            "side": "BUY",
            "asset_id": token,
            "market": cid,
            "size_matched": "0",
            "price": "0.30",
            "original_size": "500",
        }

    def _rewards(self):
        return [{"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 300}]}]

    def test_orderbook_failure_skips_order_without_status_row(self):
        # 等价于旧版 get_orderbook 抛异常被外层吞掉:跳过该单、不留状态行、不撤单
        monitor, api, db = _make_monitor({"min_reward_usd": 100.0})
        monitor.begin_status_tick()
        api.get_open_orders.return_value = [self._buy()]
        api.get_orderbook.side_effect = RuntimeError("network")
        api.get_rewards_for_market.return_value = self._rewards()
        monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()
        assert [r for r in monitor._status_rows if r.get("stage") == "Step3"] == []

    def test_empty_book_still_records_status_row(self):
        # 「取到了但买卖盘为空」是另一回事:走原分支并留状态行
        monitor, api, db = _make_monitor({"min_reward_usd": 100.0})
        monitor.begin_status_tick()
        api.get_open_orders.return_value = [self._buy()]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        api.get_rewards_for_market.return_value = self._rewards()
        monitor.check_sell_orders()
        rows = [r for r in monitor._status_rows if r.get("stage") == "Step3"]
        assert rows and rows[0]["action"] == "跳过(盘口为空)"

    def test_failed_order_does_not_block_the_next_one(self):
        monitor, api, db = _make_monitor({"min_reward_usd": 100.0})
        monitor.begin_status_tick()
        api.get_open_orders.return_value = [
            self._buy("o1", "tokBad", "cid1"),
            self._buy("o2", "tokOk", "cid2"),
        ]

        def _ob_or_boom(token_id):
            if token_id == "tokBad":
                raise RuntimeError("network")
            return {
                "bids": [{"price": "0.30", "size": "1000"}],
                "asks": [{"price": "0.31", "size": "1000"}],
                "tick_size": "0.01",
            }

        api.get_orderbook.side_effect = _ob_or_boom
        api.get_rewards_for_market.return_value = self._rewards()
        monitor.check_sell_orders()
        rows = [r for r in monitor._status_rows if r.get("stage") == "Step3"]
        assert len(rows) == 1 and rows[0]["market"] == "cid2"

    def test_db_reads_are_once_per_round_not_per_order(self):
        # 黑名单和模板原来是每单各读一遍;60 个挂单就是 60 次查询
        monitor, api, db = _make_monitor({"min_reward_usd": 100.0})
        api.get_open_orders.return_value = [
            self._buy("o1", "tokA", "cid1"),
            self._buy("o2", "tokB", "cid2"),
            self._buy("o3", "tokC", "cid3"),
        ]
        api.get_orderbook.return_value = {
            "bids": [{"price": "0.30", "size": "1000"}],
            "asks": [{"price": "0.31", "size": "1000"}],
            "tick_size": "0.01",
        }
        api.get_rewards_for_market.return_value = self._rewards()
        db.reset_mock()
        monitor.check_sell_orders()
        assert db.get_blacklist_ids.call_count == 1
        assert db.get_template_for.call_count == 1
        assert db.get_settings.call_count == 1

    def test_still_iterates_all_orders_for_status_rows(self):
        # 遍历必须是全量 open_orders:卖单和部分成交单的状态行不能因为预取只挑买单
        # 而凭空消失。
        monitor, api, db = _make_monitor({"min_reward_usd": 100.0})
        monitor.begin_status_tick()
        api.get_open_orders.return_value = [
            {
                "id": "s1",
                "side": "SELL",
                "asset_id": "tokS",
                "market": "cidS",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
            },
            {
                "id": "p1",
                "side": "BUY",
                "asset_id": "tokP",
                "market": "cidP",
                "size_matched": "100",
                "price": "0.30",
                "original_size": "500",
            },
        ]
        api.get_orderbook.return_value = {"bids": [], "asks": [], "tick_size": "0.01"}
        api.get_rewards_for_market.return_value = self._rewards()
        monitor.check_sell_orders()
        stages = [r.get("stage") for r in monitor._status_rows]
        assert "止盈卖单" in stages and "Step1" in stages
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_monitor.py::TestStep3PrefetchWiring tests/test_monitor.py::TestStep3Blacklist -v`
Expected: FAIL。`TestStep3Blacklist` 报 `TypeError: _check_compliance() takes 2 positional arguments but 6 were given`；`test_db_reads_are_once_per_round_not_per_order` 报 `assert 3 == 1`（黑名单每单读一次）。

- [ ] **Step 3: 改 `check_sell_orders`**

`engine/monitor.py:908-915` 换成：

```python
    def check_sell_orders(self):
        """Reused tick name kept for the manager loop; runs strategy compliance."""
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed for %s: %s", self.wallet_address, e)
            return
        # DB 读全部收在主线程、每轮一次:黑名单和模板原来是每单各读一遍(60 个挂单 =
        # 60 次查询),ttl 还要传进预取的 worker —— worker 绝不碰 db。
        blacklist = self.db.get_blacklist_ids()
        settings = self.db.get_template_for(self.wallet_address)
        ttl = self.db.get_settings()["rewards_cache_ttl_sec"]
        books, rewards = self._prefetch(open_orders, blacklist, ttl)
```

（原来的 `self._round_rewards = {}` 那一行随之删掉。）

循环体里两处状态行分流（卖单、部分成交）一字不动，只把最后的调用改成：

```python
            try:
                self._check_compliance(o, blacklist, settings, books, rewards)
            except Exception as e:
                logger.error("Compliance error on %s: %s", o.get("id"), e)
```

- [ ] **Step 4: 删掉 `_round_market_rewards` 与 `_round_rewards`**

删除 `engine/monitor.py` 的整个 `_round_market_rewards` 方法（`def _round_market_rewards(self, condition_id):` 到 `return pair`），以及 `__init__` 里这两行：

```python
        # 本轮 Step3 已取过奖励的市场:condition_id -> (max_spread, daily_rate)
        self._round_rewards: dict = {}
```

（那段注释共 4 行，从 `# 本轮 Step3 已取过奖励的市场` 到 `self._round_rewards: dict = {}`，整段删掉。）

- [ ] **Step 5: 改 `_check_compliance` 签名与取数**

方法签名与 docstring 换成：

```python
    def _check_compliance(self, o: dict, blacklist, settings, books, rewards):
        """Decide what to do with a resting buy this tick: first re-check the
        bid-ask spread (cancel if it widened past the threshold), else price
        compliance.

        盘口与奖励由 check_sell_orders 并发预取好传入,本方法只判定、不发取数请求;
        撤单、记账、状态行仍在主线程串行。判定逻辑与顺序与预取化之前完全一致。
        """
```

黑名单那一行的 DB 读换成参数：

```python
        if cid in self.db.get_blacklist_ids():
```
→
```python
        if cid in blacklist:
```

模板读与盘口取数这两行：

```python
        settings = self.db.get_template_for(self.wallet_address)
        ob = self.api.get_orderbook(token_id)
```
→
```python
        ob = books.get(token_id)
        if ob is None:
            # 预取取不到盘口。等价于旧版 get_orderbook 抛异常被外层吞掉的路径:本轮
            # 跳过该单、不留状态行。「取到了但买卖盘为空」是另一回事,走下面的
            # 「跳过(盘口为空)」分支并留状态行,两者不可混为一谈。
            logger.warning(
                "[Step3] 单 %s 市场 %s | 订单簿取不到,本轮跳过", o.get("id"), cid
            )
            return
```

奖励取数那一行：

```python
        max_spread, daily_rate = self._round_market_rewards(cid)
```
→
```python
        max_spread, daily_rate = rewards.get(cid, (None, None))
```

方法其余部分（价差复查、奖励闸门、空盘口守卫、midpoint、单价区间、奖励区间、keep）一律不动。

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_monitor.py -v`
Expected: PASS。新的 `TestStep3PrefetchWiring` 全绿，`TestStep3RewardDrop` / `TestStep3PriceBand` / `TestStep3EligibilityRecheck` / `TestStep3Blacklist` / `TestCheckSellOrders` / `TestStep3ActionLog` / `TestMonitorStatusSnapshot` 全部保持通过。

若某个既有断言因为多线程下 mock 调用顺序不再确定而失败，改断言（比如把顺序相关的断言换成集合或计数），不要改实现——判定阶段仍是主线程串行，动作类型与状态行的产生顺序没有变化。

- [ ] **Step 7: 确认没有残留引用**

Run: `grep -rn "_round_market_rewards\|_round_rewards" --include=*.py .`
Expected: 无输出

- [ ] **Step 8: 全量回归并提交**

Run: `python -m pytest -q`
Expected: 817 passed（上一任务后 812，本任务新增 5 个）

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "$(cat <<'EOF'
perf: Step3 改用并发预取,判定仍串行

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## 收尾提醒

纯性能改动，无配置变更、无数据格式变更、无用户可见行为变更。按 `docs/版本号规范.md` 属于修订号；若与其它功能同批发版，跟随那一批的级别。

发版说明可以提一句预期效果：多挂单钱包（几十个在挂单）的监控 tick 会明显变快，止损和成交检测的实际间隔跟着缩短。并发度写死在 `engine/monitor.py` 的 `_STEP3_MAX_WORKERS`，代理压力大时改小它即可。
