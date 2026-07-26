# 在挂单市场每日奖励实时复查 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 监控 Step3 每 5 秒复查在挂单市场的实时每日奖励，跌破该钱包模板的 `min_reward_usd` 立即撤买单，并把实时值写回候选池，防止下单轮把单挂回去。

**Architecture:** Step3 已经在调 `/rewards/markets/{condition_id}`，把它的返回值多解析一个 `rewards_config[].rate_per_day` 求和，就得到实时每日奖励，不新增接口调用形态。撤单分支插在 `_check_compliance` 里既有判定之间，其余判定顺序一律不动。写回通过 manager 注入 `WalletWorker` 再注入 `OrderMonitor` 的回调完成，不产生 monitor 到 manager 的反向引用。

**Tech Stack:** Python 3、pytest、`unittest.mock.MagicMock`、SQLite（`models/database.py`）。

设计文档：`docs/superpowers/specs/2026-07-26-realtime-reward-recheck-design.md`

## Global Constraints

- 绝不 fail-close：取奖励失败、响应为空、解析不出奖励配置，一律本轮跳过，不撤不重挂，记 WARNING。
- `0.0` 与 `None` 语义必须分开：`0.0` 是奖励真归零（撤单），`None` 是取不到（跳过）。禁止用 `if not daily_rate` 这类假值判断把两者合并。
- 撤单失败不写回：`cancel_orders` 抛错时 WARNING + return，不记 action、不调回调。
- 写回不许拖累交易：回调整体包 try/except，只记 WARNING，绝不中断撤单流程。
- 只撤买单，持仓不动，仍由 `check_exit` 管。
- 不新增任何配置项，门槛复用模板的 `min_reward_usd`（默认 100.0）。
- 用户可见字符串一律简体中文。
- 每次 commit 只 stage 本任务列出的文件，不要 `git add -A`。commit message 结尾附一行 `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`。

## File Structure

- `engine/rewards.py`（修改）：新增纯函数 `extract_daily_rate`，与 `extract_max_spread` 并列。这个文件只做 `/rewards/markets/{cid}` 响应的无 IO 解析。
- `engine/monitor.py`（修改）：`_market_max_spread` 改名并扩展为 `_market_rewards`；`_check_compliance` 插入奖励撤单分支；`__init__` 增加 `on_reward_update` 参数与 `_notify_reward_update` 辅助方法。
- `engine/manager.py`（修改）：`WalletWorker.__init__` 增加 `on_reward_update` 透传；`EngineManager.update_market_reward` 新方法；`start_wallet` 注入回调。
- `models/database.py`（修改）：`update_eligible_reward` 新方法，紧邻 `get_eligible_markets`。
- `config.py`（修改）：`rewards_cache_ttl_sec` 默认值 600 改 0。
- `README.md` / `docs/系统逻辑与参数说明.md` / `web/templates/config.html`（修改）：该配置项的文案同步。
- `tests/test_rewards.py`、`tests/test_monitor.py`、`tests/test_database.py`、`tests/test_manager.py`（修改）：对应测试。

---

### Task 1: 纯函数 `extract_daily_rate`

**Files:**
- Modify: `engine/rewards.py`
- Test: `tests/test_rewards.py`

**Interfaces:**
- Consumes: 无
- Produces: `extract_daily_rate(rewards_items: list) -> Optional[float]` —— 对 `get_rewards_for_market()` 的返回值求 `rate_per_day` 总和，单位美元/天。一个都没解析到返回 `None`，解析到了返回累加值（可能是 `0.0`）。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_rewards.py` 末尾，同时把第 3 行的 import 改成：

```python
from engine.rewards import extract_max_spread, extract_daily_rate
```

追加的测试：

```python
def test_daily_rate_sums_multiple_configs():
    items = [{"rewards_config": [{"rate_per_day": 10}, {"rate_per_day": 5}]}]
    assert extract_daily_rate(items) == 15.0


def test_daily_rate_sums_across_data_items():
    items = [
        {"rewards_config": [{"rate_per_day": 10}]},
        {"rewards_config": [{"rate_per_day": 5}]},
    ]
    assert extract_daily_rate(items) == 15.0


def test_daily_rate_zero_is_zero_not_none():
    # 0 是「奖励真归零」,调用方要据此撤单;绝不能和「取不到」(None)混为一谈。
    result = extract_daily_rate([{"rewards_config": [{"rate_per_day": 0}]}])
    assert result == 0.0
    assert result is not None


def test_daily_rate_string_values_parsed():
    assert extract_daily_rate([{"rewards_config": [{"rate_per_day": "2.5"}]}]) == 2.5


def test_daily_rate_empty_returns_none():
    assert extract_daily_rate([]) is None
    assert extract_daily_rate(None) is None


def test_daily_rate_no_rewards_config_returns_none():
    assert extract_daily_rate([{"condition_id": "0x1", "rewards_max_spread": 3}]) is None


def test_daily_rate_unparseable_skipped_but_rest_summed():
    items = [{"rewards_config": [{"rate_per_day": "abc"}, {"rate_per_day": 3}]}]
    assert extract_daily_rate(items) == 3.0


def test_daily_rate_all_unparseable_returns_none():
    assert extract_daily_rate([{"rewards_config": [{"rate_per_day": "abc"}]}]) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_rewards.py -v`
Expected: FAIL，`ImportError: cannot import name 'extract_daily_rate'`

- [ ] **Step 3: 写实现**

追加到 `engine/rewards.py` 末尾：

```python
def extract_daily_rate(rewards_items: list) -> Optional[float]:
    """Sum rate_per_day across all reward configs of /rewards/markets/{cid}.

    Returns the market's total daily reward in USD, or None when the payload
    carries no parsable rewards_config at all. 0.0 and None are different:
    0.0 means the reward really is zero (cancel the resting buy), None means
    we could not tell (skip safely). Callers must not collapse them into one
    falsy check.

    Same formula the discovery scan uses for market_reward
    (engine/scanner.py _precise_reward), so the two stay on one yardstick.
    """
    total = 0.0
    found = False
    for it in rewards_items or []:
        if not isinstance(it, dict):
            continue
        for rc in it.get("rewards_config") or []:
            if not isinstance(rc, dict):
                continue
            v = rc.get("rate_per_day")
            if v is None:
                continue
            try:
                total += float(v)
            except (TypeError, ValueError):
                continue
            found = True
    return total if found else None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_rewards.py -v`
Expected: PASS，全部用例通过

- [ ] **Step 5: 提交**

```bash
git add engine/rewards.py tests/test_rewards.py
git commit -m "$(cat <<'EOF'
feat: 新增 extract_daily_rate 解析市场每日奖励

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `_market_rewards` 一次取数返回两个值

把 `OrderMonitor._market_max_spread` 扩展为 `_market_rewards`，同一次 HTTP 响应解析出 `rewards_max_spread` 和每日奖励。这一步是纯重构，不改任何判定行为。

**Files:**
- Modify: `engine/monitor.py:16`（import）、`engine/monitor.py:44`（缓存字段）、`engine/monitor.py:932-954`（方法）、`engine/monitor.py:1059`（调用点）
- Test: `tests/test_monitor.py`

**Interfaces:**
- Consumes: Task 1 的 `extract_daily_rate(rewards_items) -> Optional[float]`
- Produces: `OrderMonitor._market_rewards(condition_id: str) -> tuple[Optional[float], Optional[float]]`，返回 `(max_spread_cents, daily_rate_usd)`。缓存字段由 `self._max_spread_cache` 改名为 `self._rewards_cache`，值形如 `{cid: ((max_spread, daily_rate), fetched_at)}`。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_monitor.py` 末尾：

```python
class TestMarketRewards:
    """一次取数拿到 rewards_max_spread 与每日奖励;TTL=0 时每次都重新联网。"""

    def _payload(self, max_spread=3, rate=120):
        return [
            {
                "rewards_max_spread": max_spread,
                "rewards_config": [{"rate_per_day": rate}],
            }
        ]

    def test_returns_max_spread_and_daily_rate(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = self._payload()
        assert monitor._market_rewards("cid1") == (3.0, 120.0)

    def test_empty_condition_id_returns_none_pair(self):
        monitor, api, db = _make_monitor()
        assert monitor._market_rewards("") == (None, None)
        api.get_rewards_for_market.assert_not_called()

    def test_api_failure_returns_none_pair(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.side_effect = RuntimeError("network")
        assert monitor._market_rewards("cid1") == (None, None)

    def test_missing_rewards_config_gives_none_rate(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 4}]
        assert monitor._market_rewards("cid1") == (4.0, None)

    def test_cached_within_ttl_fetches_once(self):
        monitor, api, db = _make_monitor({"rewards_cache_ttl_sec": 600})
        api.get_rewards_for_market.return_value = self._payload()
        monitor._market_rewards("cid1")
        monitor._market_rewards("cid1")
        assert api.get_rewards_for_market.call_count == 1

    def test_ttl_zero_refetches_every_call(self):
        monitor, api, db = _make_monitor({"rewards_cache_ttl_sec": 0})
        api.get_rewards_for_market.return_value = self._payload()
        monitor._market_rewards("cid1")
        monitor._market_rewards("cid1")
        assert api.get_rewards_for_market.call_count == 2

    def test_nothing_parsable_is_not_cached(self):
        # 一无所获不写缓存,下轮重试(沿用旧 _market_max_spread 的语义)
        monitor, api, db = _make_monitor({"rewards_cache_ttl_sec": 600})
        api.get_rewards_for_market.return_value = [{"condition_id": "cid1"}]
        monitor._market_rewards("cid1")
        monitor._market_rewards("cid1")
        assert api.get_rewards_for_market.call_count == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_monitor.py::TestMarketRewards -v`
Expected: FAIL，`AttributeError: 'OrderMonitor' object has no attribute '_market_rewards'`

- [ ] **Step 3: 写实现**

`engine/monitor.py:16` 的 import 改成：

```python
from engine.rewards import extract_max_spread, extract_daily_rate
```

`engine/monitor.py:43-44` 的缓存字段改成：

```python
        # condition_id -> ((max_spread, daily_rate), fetched_at) TTL cache for Step 3.
        self._rewards_cache: dict = {}
```

`engine/monitor.py:932-954` 整个 `_market_max_spread` 方法替换为：

```python
    def _market_rewards(self, condition_id: str) -> tuple[float | None, float | None]:
        """(rewards_max_spread 美分, 每日奖励美元),同一次响应解析,TTL 缓存。

        任一项为 None = 该项取不到(接口失败/字段缺失),调用方各自安全跳过。
        每日奖励的 0.0 与 None 含义不同:0.0=奖励真归零(要撤单),None=取不到(跳过)。
        max_spread 保持 float(不 int 化):实盘存在 3.5/4.5 美分,截断会缩窄奖励区间。
        """
        if not condition_id:
            return None, None
        ttl = self.db.get_settings()["rewards_cache_ttl_sec"]
        now = time.time()
        hit = self._rewards_cache.get(condition_id)
        if hit and (now - hit[1]) < ttl:
            return hit[0]
        try:
            items = self.api.get_rewards_for_market(condition_id)
        except Exception as e:
            logger.warning("get_rewards_for_market(%s) failed: %s", condition_id, e)
            return None, None
        pair = (extract_max_spread(items), extract_daily_rate(items))
        if pair == (None, None):
            return pair  # 一无所获不写缓存,下轮重试
        self._rewards_cache[condition_id] = (pair, now)
        return pair
```

`engine/monitor.py:1059` 的调用点改成：

```python
        max_spread, daily_rate = self._market_rewards(cid)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_monitor.py -v`
Expected: PASS。新的 `TestMarketRewards` 全绿，且 `TestStep3PriceBand` / `TestStep3EligibilityRecheck` / `TestCheckSellOrders` 等既有 Step3 用例全部保持通过（本任务不改任何判定行为）。

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "$(cat <<'EOF'
refactor: Step3 一次取数同时拿 max_spread 与每日奖励

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: DB `update_eligible_reward`

**Files:**
- Modify: `models/database.py`（在 `get_eligible_markets` 之后、`# --- Market Meta` 注释之前插入）
- Test: `tests/test_database.py`

**Interfaces:**
- Consumes: 无
- Produces: `Database.update_eligible_reward(condition_id: str, reward: float) -> None`，把 `eligible_markets` 表里该市场所有 token 行的 `daily_reward` 更新为 `reward`。空 `condition_id` 是 no-op。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_database.py` 的 `class TestEligibleMarkets` 里（该类使用现成的 `db` fixture，定义在同文件 76 行）：

```python
    def test_update_eligible_reward_overwrites_all_tokens_of_market(self, db):
        db.save_eligible_markets(
            [
                {
                    "market_id": "0xabc",
                    "token_id": "yes",
                    "market_name": "M",
                    "outcome": "Yes",
                    "daily_reward": 300.0,
                    "order_price": 0.30,
                    "order_size": 100,
                },
                {
                    "market_id": "0xabc",
                    "token_id": "no",
                    "market_name": "M",
                    "outcome": "No",
                    "daily_reward": 300.0,
                    "order_price": 0.70,
                    "order_size": 100,
                },
                {
                    "market_id": "0xother",
                    "token_id": "yes",
                    "market_name": "N",
                    "outcome": "Yes",
                    "daily_reward": 300.0,
                    "order_price": 0.30,
                    "order_size": 100,
                },
            ]
        )
        db.update_eligible_reward("0xabc", 5.0)
        rows = {(r["market_id"], r["token_id"]): r["daily_reward"]
                for r in db.get_eligible_markets()}
        assert rows[("0xabc", "yes")] == 5.0
        assert rows[("0xabc", "no")] == 5.0
        assert rows[("0xother", "yes")] == 300.0  # 别的市场不受影响

    def test_update_eligible_reward_empty_id_is_noop(self, db):
        db.save_eligible_markets(
            [
                {
                    "market_id": "0xabc",
                    "token_id": "yes",
                    "market_name": "M",
                    "outcome": "Yes",
                    "daily_reward": 300.0,
                    "order_price": 0.30,
                    "order_size": 100,
                }
            ]
        )
        db.update_eligible_reward("", 5.0)
        assert db.get_eligible_markets()[0]["daily_reward"] == 300.0

    def test_update_eligible_reward_unknown_market_is_noop(self, db):
        db.update_eligible_reward("0xnothere", 5.0)  # 不抛异常即可
        assert db.get_eligible_markets() == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_database.py::TestEligibleMarkets -v`
Expected: FAIL，`AttributeError: 'Database' object has no attribute 'update_eligible_reward'`

- [ ] **Step 3: 写实现**

在 `models/database.py` 的 `get_eligible_markets` 方法之后插入：

```python
    def update_eligible_reward(self, condition_id: str, reward: float):
        """把实时复查到的每日奖励写回该市场在 eligible_markets 的所有 token 行。

        监控 Step3 发现在挂单市场的奖励跌破门槛时调用,让 /api/eligible 的展示值
        与低余额清仓的 get_market_daily_reward 都跟着变准。市场不在表里是 no-op。
        """
        if not condition_id:
            return
        c = self.conn.cursor()
        c.execute(
            "UPDATE eligible_markets SET daily_reward = ? WHERE market_id = ?",
            (float(reward), condition_id),
        )
        self.conn.commit()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_database.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add models/database.py tests/test_database.py
git commit -m "$(cat <<'EOF'
feat: db.update_eligible_reward 写回市场每日奖励

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: 写回管道（manager 方法 + 回调注入 + monitor 侧辅助方法）

搭好从 `OrderMonitor` 回到候选池的通路。这一步还没有人触发它，Task 5 才接上。

**Files:**
- Modify: `engine/manager.py:49-66`（`WalletWorker.__init__`）、`engine/manager.py:842`（`start_wallet`）、`engine/manager.py` 新增 `EngineManager.update_market_reward`（放在 `_should_discover` 之前）
- Modify: `engine/monitor.py:31`（`OrderMonitor.__init__` 签名）、新增 `_notify_reward_update`
- Test: `tests/test_manager.py`

**Interfaces:**
- Consumes: Task 3 的 `Database.update_eligible_reward(condition_id, reward)`
- Produces:
  - `EngineManager.update_market_reward(condition_id: str, reward: float) -> None`
  - `WalletWorker.__init__(api, db, wallet_address, settings, on_reward_update=None)`
  - `OrderMonitor.__init__(api, db, wallet_address, on_reward_update=None)`，实例属性 `self.on_reward_update`
  - `OrderMonitor._notify_reward_update(condition_id: str, reward: float) -> None`，回调不存在或抛错都只记 WARNING

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_manager.py` 末尾：

```python
class TestUpdateMarketReward:
    """实时奖励写回候选池:内存池条目就地改写 + DB 同步,失败不外抛。"""

    def _mgr(self, pool):
        db = MagicMock()
        manager = EngineManager(db, encryption_key=b"x" * 32)
        manager.eligible_markets = pool
        return manager, db

    def test_rewrites_pool_entry_and_syncs_db(self):
        pool = [
            {"condition_id": "0xabc", "market_reward": 300.0, "daily_reward": 300.0},
            {"condition_id": "0xother", "market_reward": 300.0, "daily_reward": 300.0},
        ]
        manager, db = self._mgr(pool)
        manager.update_market_reward("0xabc", 5.0)
        # 命中的市场两个键都改写(prefilter 判 market_reward,前端显示 daily_reward)
        assert pool[0]["market_reward"] == 5.0
        assert pool[0]["daily_reward"] == 5.0
        # 其它市场不受影响
        assert pool[1]["market_reward"] == 300.0
        db.update_eligible_reward.assert_called_once_with("0xabc", 5.0)

    def test_market_not_in_pool_still_syncs_db(self):
        manager, db = self._mgr([])
        manager.update_market_reward("0xabc", 5.0)
        db.update_eligible_reward.assert_called_once_with("0xabc", 5.0)


class TestRewardUpdateCallbackWiring:
    """回调一路从 manager 注入到 monitor;monitor 侧调用永不外抛。"""

    def test_worker_passes_callback_to_monitor(self):
        cb = MagicMock()
        worker = WalletWorker(
            MagicMock(),
            MagicMock(),
            "0xW",
            {"fill_check_interval_sec": 5},
            on_reward_update=cb,
        )
        assert worker.monitor.on_reward_update is cb

    def test_worker_without_callback_defaults_to_none(self):
        worker = WalletWorker(
            MagicMock(), MagicMock(), "0xW", {"fill_check_interval_sec": 5}
        )
        assert worker.monitor.on_reward_update is None

    def test_notify_is_noop_without_callback(self):
        from engine.monitor import OrderMonitor

        monitor = OrderMonitor(MagicMock(), MagicMock(), "0xW")
        monitor._notify_reward_update("0xabc", 5.0)  # 不抛异常即可

    def test_notify_swallows_callback_failure(self):
        from engine.monitor import OrderMonitor

        cb = MagicMock(side_effect=RuntimeError("db down"))
        monitor = OrderMonitor(MagicMock(), MagicMock(), "0xW", on_reward_update=cb)
        monitor._notify_reward_update("0xabc", 5.0)  # 写回失败绝不能中断交易流程
        cb.assert_called_once_with("0xabc", 5.0)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_manager.py::TestUpdateMarketReward tests/test_manager.py::TestRewardUpdateCallbackWiring -v`
Expected: FAIL，`AttributeError: 'EngineManager' object has no attribute 'update_market_reward'` 以及 `TypeError: __init__() got an unexpected keyword argument 'on_reward_update'`

- [ ] **Step 3: 写实现**

`engine/monitor.py:31` 的 `OrderMonitor.__init__` 签名改成：

```python
    def __init__(self, api, db, wallet_address: str, on_reward_update=None):
```

在该方法体内 `self.wallet_address = wallet_address` 之后加：

```python
        # 实时奖励写回候选池的回调(manager 注入);None=不写回(测试/临时下单 worker)。
        self.on_reward_update = on_reward_update
```

在 `engine/monitor.py` 的 `_record_action` 方法之后加：

```python
    def _notify_reward_update(self, condition_id: str, reward: float) -> None:
        """把实时每日奖励写回候选池。纯筛选/展示用,绝不能中断撤单流程。"""
        if not self.on_reward_update:
            return
        try:
            self.on_reward_update(condition_id, reward)
        except Exception as e:
            logger.warning("奖励写回失败 %s: %s", condition_id, e)
```

`engine/manager.py:49` 的 `WalletWorker.__init__` 签名改成：

```python
    def __init__(
        self, api: PolymarketAPI, db, wallet_address: str, settings: dict,
        on_reward_update=None,
    ):
```

同方法体内 `self.monitor = OrderMonitor(api, db, wallet_address)` 那一行改成：

```python
        self.monitor = OrderMonitor(
            api, db, wallet_address, on_reward_update=on_reward_update
        )
```

`engine/manager.py:842`（`start_wallet` 内）改成：

```python
        worker = WalletWorker(
            api, self.db, address, settings,
            on_reward_update=self.update_market_reward,
        )
```

在 `EngineManager._should_discover` 之前插入新方法：

```python
    def update_market_reward(self, condition_id: str, reward: float):
        """实时复查到的每日奖励写回候选池(内存+DB),下单轮 prefilter 立刻用新值。

        监控 Step3 撤掉「奖励跌破门槛」的买单后调用。不写回的话,30 秒后的下单轮会
        拿最长 4 小时前的快照把单挂回来、5 秒后监控再撤,来回打架。只写 market_reward
        就足以让市场跌出 eligible(prefilter 判的是 or 条件),daily_reward 是展示键。
        """
        pool = self.eligible_markets  # 先取本地引用:扫描会整体重绑该属性
        for m in pool:
            if m.get("condition_id") == condition_id:
                m["market_reward"] = reward
                m["daily_reward"] = reward
        self.db.update_eligible_reward(condition_id, reward)
```

注意 `engine/manager.py:797` 那个临时下单 worker 不传回调，保持原样。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_manager.py tests/test_monitor.py -v`
Expected: PASS，既有用例（含 `WalletWorker(api, db, "0xW", {...})` 四参数调用）全部保持通过。

- [ ] **Step 5: 提交**

```bash
git add engine/manager.py engine/monitor.py tests/test_manager.py
git commit -m "$(cat <<'EOF'
feat: 实时奖励写回候选池的回调管道

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Step3 奖励跌破门槛撤单

**Files:**
- Modify: `engine/monitor.py`（`_check_compliance`，在 `max_spread, daily_rate = self._market_rewards(cid)` 之后插入新分支）
- Test: `tests/test_monitor.py`

**Interfaces:**
- Consumes: Task 2 的 `_market_rewards`、Task 4 的 `_notify_reward_update`
- Produces: 新的 action 类型 `reward_drop_cancel`（`side="-"`、`price=-1`、`size` 为订单原始份数），以及 Step3 状态行 `撤单(奖励下降)`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_monitor.py` 末尾：

```python
class TestStep3RewardDrop:
    """在挂单市场的每日奖励跌破 min_reward_usd → 立即撤买单不重挂 + 写回候选池。"""

    SETTINGS = {"min_reward_usd": 100.0}

    def _ob(self, best_bid="0.30", best_ask="0.31", tick="0.01"):
        return {
            "bids": [{"price": best_bid, "size": "1000"}],
            "asks": [{"price": best_ask, "size": "1000"}],
            "tick_size": tick,
        }

    def _order(self):
        return {
            "id": "o1",
            "side": "BUY",
            "asset_id": "tok1",
            "market": "cid1",
            "size_matched": "0",
            "price": "0.30",
            "original_size": "500",
        }

    def _rewards(self, rate, max_spread=3):
        return [
            {
                "rewards_max_spread": max_spread,
                "rewards_config": [{"rate_per_day": rate}],
            }
        ]

    def _run(self, rate, settings=None, cb=None):
        monitor, api, db = _make_monitor(settings or self.SETTINGS)
        if cb is not None:
            monitor.on_reward_update = cb
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        if isinstance(rate, Exception):
            api.get_rewards_for_market.side_effect = rate
        else:
            api.get_rewards_for_market.return_value = rate
        monitor.check_sell_orders()
        return monitor, api, db

    def test_cancels_when_reward_below_threshold(self):
        monitor, api, db = self._run(self._rewards(5))
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_not_called()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["reward_drop_cancel"]
        kw = db.record_action.call_args_list[0].kwargs
        assert kw["side"] == "-"
        assert kw["price"] == -1
        assert kw["size"] == 500
        assert "5.00" in kw["reason"] and "100.00" in kw["reason"]

    def test_cancels_when_reward_is_zero(self):
        # 0 是「奖励真归零」,必须撤单——不能被当成「取不到」跳过。
        monitor, api, db = self._run(self._rewards(0))
        api.cancel_orders.assert_called_once_with(["o1"])

    def test_keeps_when_reward_equals_threshold(self):
        # 门槛是「低于才撤」,与扫描阶段 prefilter 的 < 口径一致。
        monitor, api, db = self._run(self._rewards(100))
        api.cancel_orders.assert_not_called()

    def test_keeps_when_reward_above_threshold(self):
        monitor, api, db = self._run(self._rewards(300))
        api.cancel_orders.assert_not_called()
        db.record_action.assert_not_called()

    def test_fetch_failure_does_not_cancel(self):
        # 接口抖一下就撤光正在赚奖励的单是最坏结果:取不到一律跳过。
        monitor, api, db = self._run(RuntimeError("network"))
        api.cancel_orders.assert_not_called()
        db.record_action.assert_not_called()

    def test_missing_rewards_config_does_not_cancel(self):
        monitor, api, db = self._run([{"rewards_max_spread": 3}])
        api.cancel_orders.assert_not_called()

    def test_writes_back_realtime_reward(self):
        cb = MagicMock()
        monitor, api, db = self._run(self._rewards(5), cb=cb)
        cb.assert_called_once_with("cid1", 5.0)

    def test_cancel_failure_skips_writeback_and_action(self):
        cb = MagicMock()
        monitor, api, db = _make_monitor(self.SETTINGS)
        monitor.on_reward_update = cb
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = self._rewards(5)
        api.cancel_orders.side_effect = RuntimeError("network")
        monitor.check_sell_orders()  # must not raise
        cb.assert_not_called()
        db.record_action.assert_not_called()

    def test_writeback_failure_does_not_break_cancel(self):
        cb = MagicMock(side_effect=RuntimeError("db down"))
        monitor, api, db = self._run(self._rewards(5), cb=cb)
        api.cancel_orders.assert_called_once_with(["o1"])
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["reward_drop_cancel"]

    def test_status_row_published(self):
        monitor, api, db = _make_monitor(self.SETTINGS)
        monitor.begin_status_tick()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = self._rewards(5)
        monitor.check_sell_orders()
        rows = [r for r in monitor._status_rows if r.get("stage") == "Step3"]
        assert rows and rows[0]["action"] == "撤单(奖励下降)"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_monitor.py::TestStep3RewardDrop -v`
Expected: FAIL，`test_cancels_when_reward_below_threshold` 报 `AssertionError: Expected 'cancel_orders' to be called once. Called 0 times.`（当前奖励金额不参与判定）

- [ ] **Step 3: 写实现**

先把 `engine/monitor.py:1082-1083` 这两行**上移**到 `max_spread, daily_rate = self._market_rewards(cid)` 之前（新分支和后续代码共用，避免重复计算）：

```python
        cur_price = float(o.get("price", 0) or 0)
        osize = int(float(o.get("original_size", 0) or 0))
```

然后在 `max_spread, daily_rate = self._market_rewards(cid)` 之后、`if max_spread is None:` 之前插入：

```python
        # 奖励金额实时复查:市场每日奖励跌破门槛 -> 撤买单不重挂,并把实时值写回候选池
        # (不写回的话,30 秒后的下单轮会拿最长 4 小时前的快照把单挂回来,来回打架)。
        # daily_rate 的 0.0 与 None 含义不同:0.0=奖励真归零(撤),None=取不到(跳过)。
        # 排在「max_spread 取不到就跳过」之前:两个值来自同一次响应,万一响应里
        # rewards_max_spread 缺失而 rewards_config 正常,奖励判定不该被一起跳过。
        min_reward = float(settings.get("min_reward_usd", 0) or 0)
        if daily_rate is not None and daily_rate < min_reward:
            reason = (
                f"实时每日奖励 ${daily_rate:.2f} < 门槛 ${min_reward:.2f}，撤买单不重挂"
            )
            try:
                self.api.cancel_orders([o.get("id")])
            except Exception as e:
                logger.warning("Reward-drop cancel %s failed: %s", o.get("id"), e)
                return
            self._record_action(
                market_id=cid,
                action_type="reward_drop_cancel",
                side="-",
                price=-1,
                size=osize,
                reason=reason,
                price_basis=(
                    f"实时每日奖励=${daily_rate:.2f}；门槛=${min_reward:.2f}；"
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
                action="撤单(奖励下降)",
                detail=reason,
            )
            self._notify_reward_update(cid, daily_rate)
            logger.info(
                "[Step3] reward-drop cancel %s market %s: %s", o.get("id"), cid, reason
            )
            return
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_monitor.py -v`
Expected: PASS。`TestStep3RewardDrop` 全绿，既有 Step3 用例保持通过（既有用例的 `get_rewards_for_market` 返回值里没有 `rewards_config`，`daily_rate` 为 `None`，新分支自然跳过）。

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "$(cat <<'EOF'
feat: Step3 每日奖励跌破门槛立即撤买单

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: 默认关闭奖励缓存 + 文案同步 + 全量回归

**Files:**
- Modify: `config.py:37`
- Modify: `README.md:173`
- Modify: `docs/系统逻辑与参数说明.md:238`
- Modify: `web/templates/config.html:191`
- Test: 全量 `pytest`

**Interfaces:**
- Consumes: Task 2 的 `_market_rewards`（TTL=0 时每次实时取）
- Produces: 无新接口

配置链路对 `0` 已确认安全，不需要额外改动：前端 `web/templates/config.html:521` 用 `if (!isNaN(v))` 收值（不是假值判断），`web/routes.py:332` 直接透传，`Database.get_settings`（`models/database.py:281`）用 `if k in stored` 覆盖默认值而不是 `or`。所以用户在配置页把它填 0 能正常存取。

- [ ] **Step 1: 改默认值**

`config.py:37` 改成：

```python
    # 0=每次实时取(在挂单市场的奖励复查要求实时);代理吃紧时可调回 600 恢复缓存。
    "rewards_cache_ttl_sec": 0,
```

- [ ] **Step 2: 跑测试看是否有用例断言了旧默认值**

Run: `pytest -q`
Expected: PASS。若有用例断言 `rewards_cache_ttl_sec == 600`，把断言改成 `0`；`tests/test_database.py::test_config_split_engine_and_template_defaults` 只断言键集合，不受影响。

- [ ] **Step 3: 同步三处文案**

`README.md:173` 改成：

```markdown
| `rewards_cache_ttl_sec` | 0 | 奖励参数复查缓存 TTL（秒），0=每次实时取 |
```

`docs/系统逻辑与参数说明.md:238` 改成：

```markdown
| `rewards_cache_ttl_sec` | 0 | Step 3 复查奖励参数(max_spread + 每日奖励)的缓存 TTL(秒),0=每次实时取 |
```

`web/templates/config.html:191` 改成：

```html
                <label>奖励参数缓存 (秒，0=每次实时取)</label>
```

- [ ] **Step 4: 全量回归**

Run: `pytest -q`
Expected: PASS，全部通过（改动前基线 735 个）。

再跑一次语法检查确认模板没写坏：

Run: `python -c "import config; print(config.ENGINE_DEFAULTS['rewards_cache_ttl_sec'])"`
Expected: 输出 `0`

- [ ] **Step 5: 提交**

```bash
git add config.py README.md docs/系统逻辑与参数说明.md web/templates/config.html
git commit -m "$(cat <<'EOF'
feat: 奖励参数复查默认不缓存(rewards_cache_ttl_sec=0)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## 发版提醒

这批改动是行为改变，按 `docs/版本号规范.md` 走主版本号。发版公告必须写明两件事：

1. 在挂单市场的每日奖励一旦跌破门槛会被立即撤单，且最长 4 小时内不会重挂（等下一轮市场发现刷新候选池）。
2. `rewards_cache_ttl_sec` 的默认值变更只对没在配置页动过该键的用户生效。动过的用户需要手动把它改成 0，才能启用实时复查。
