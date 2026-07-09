# 监控 Step 3 真实 rewards_max_spread Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 监控 Step 3 合规检查改用每个市场真实的 `rewards_max_spread`（带 TTL 缓存），取不到时安全跳过，消除"启动监控后误撤单"。

**Architecture:** 新增纯函数 `extract_max_spread` 解析 `/rewards/markets/{condition_id}` 返回；`OrderMonitor` 加进程内 TTL 缓存 + `_market_max_spread`；`_check_compliance` 用真实值替换写死的 `max_spread=2`，取不到则本轮跳过该单。新增设置项 `rewards_cache_ttl_sec`。

**Tech Stack:** Python 3 / pytest / unittest.mock；不直接触碰 `py_clob_client_v2`（全程 mock）。

参考 spec：`docs/superpowers/specs/2026-05-19-monitor-step3-real-max-spread-design.md`

---

### Task 1: 纯函数 `extract_max_spread`

**Files:**
- Create: `engine/rewards.py`
- Test: `tests/test_rewards.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rewards.py`:

```python
"""tests/test_rewards.py"""

from engine.rewards import extract_max_spread


def test_top_level_rewards_max_spread():
    items = [{"condition_id": "0x1", "rewards_max_spread": 99,
              "rewards_config": [{"rate_per_day": 0.25}]}]
    assert extract_max_spread(items) == 99


def test_string_and_float_values_coerced_to_int():
    assert extract_max_spread([{"rewards_max_spread": "3"}]) == 3
    assert extract_max_spread([{"rewards_max_spread": 3.0}]) == 3


def test_empty_list_returns_none():
    assert extract_max_spread([]) is None
    assert extract_max_spread(None) is None


def test_item_without_field_returns_none():
    assert extract_max_spread([{"condition_id": "0x1"}]) is None


def test_first_item_missing_second_has_value():
    items = [{"condition_id": "0x1"}, {"rewards_max_spread": 4}]
    assert extract_max_spread(items) == 4


def test_unparseable_value_skipped():
    assert extract_max_spread([{"rewards_max_spread": "abc"}]) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rewards.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.rewards'`

- [ ] **Step 3: Create `engine/rewards.py`**

```python
# engine/rewards.py
"""Pure parsing of /rewards/markets/{condition_id} response (no IO)."""

from typing import Optional


def extract_max_spread(rewards_items: list) -> Optional[int]:
    """Parse rewards_max_spread from get_rewards_for_market()'s return.

    The wrapper returns the response's ``data`` list; ``rewards_max_spread``
    sits at each item's top level (verified against a live response).

    Returns the first item's valid int rewards_max_spread, or None when the
    list is empty / no item carries the field / the value can't be int()'d.
    Callers treat None as "couldn't determine — skip safely".
    """
    for it in rewards_items or []:
        if not isinstance(it, dict):
            continue
        v = it.get("rewards_max_spread")
        if v is None:
            continue
        try:
            return int(float(v))
        except (TypeError, ValueError):
            continue
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rewards.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add engine/rewards.py tests/test_rewards.py
git commit -m "feat: pure extract_max_spread parser for per-market rewards"
```
Commit message must end with footer line: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

### Task 2: 新增设置项 `rewards_cache_ttl_sec`

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Add the default**

In `config.py`, the `DEFAULTS` dict currently ends:

```python
    "scan_interval_sec": 30,
    "fill_check_interval_sec": 5,
    "cooldown_minutes": 20,
}
```

Change to:

```python
    "scan_interval_sec": 30,
    "fill_check_interval_sec": 5,
    "cooldown_minutes": 20,
    "rewards_cache_ttl_sec": 600,
}
```

- [ ] **Step 2: Verify it loads**

Run: `python -c "from config import DEFAULTS; print(DEFAULTS['rewards_cache_ttl_sec'])"`
Expected: `600`

- [ ] **Step 3: Run full suite (no regressions from the new default)**

Run: `python -m pytest -q`
Expected: all pass (current 74 passed; unchanged count)

- [ ] **Step 4: Commit**

```bash
git add config.py
git commit -m "feat: add rewards_cache_ttl_sec setting (default 600s)"
```
Footer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

### Task 3: OrderMonitor 用真实 max_spread + TTL 缓存

**Files:**
- Modify: `engine/monitor.py` (imports, `OrderMonitor.__init__`, new `_market_max_spread`, `_check_compliance`)
- Test: `tests/test_monitor.py` (`_make_monitor` helper + `TestCheckSellOrders`)

- [ ] **Step 1: Update existing Step 3 tests + `_make_monitor`, add new failing tests**

In `tests/test_monitor.py`, the `_make_monitor` helper's `default_settings` is:

```python
    default_settings = {
        "stop_loss_pct": 15.0,
        "cooldown_minutes": 20,
    }
```

Change to:

```python
    default_settings = {
        "stop_loss_pct": 15.0,
        "cooldown_minutes": 20,
        "rewards_cache_ttl_sec": 600,
    }
```

In `class TestCheckSellOrders`, replace the three tests `test_keep_compliant_order`, `test_replace_non_compliant_order`, `test_cancel_non_compliant_order` with these versions (add `"market"` + mock `get_rewards_for_market`):

```python
    def test_keep_compliant_order(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]

        with patch("engine.monitor.needs_replace", return_value="keep"):
            monitor.check_sell_orders()

        api.cancel_orders.assert_not_called()

    def test_replace_non_compliant_order(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
                "neg_risk": False,
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.place_limit_buy.return_value = {"orderID": "o2"}

        with patch("engine.monitor.needs_replace", return_value="replace"), patch(
            "engine.monitor.determine_order_price", return_value=0.48
        ):
            monitor.check_sell_orders()

        api.cancel_orders.assert_called_with(["o1"])
        api.place_limit_buy.assert_called_once()

    def test_cancel_non_compliant_order(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.40",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]

        with patch("engine.monitor.needs_replace", return_value="cancel"), patch(
            "engine.monitor.determine_order_price", return_value=0.48
        ):
            monitor.check_sell_orders()

        api.cancel_orders.assert_called_with(["o1"])
        api.place_limit_buy.assert_not_called()
```

Then append these four NEW tests to `class TestCheckSellOrders`:

```python
    def test_uses_real_max_spread_from_rewards_api(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "0",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]

        with patch("engine.monitor.needs_replace", return_value="keep"), patch(
            "engine.monitor.determine_order_price", return_value=0.48
        ) as dop:
            monitor.check_sell_orders()

        assert dop.call_args.kwargs["max_spread"] == 3

    def test_rewards_cache_hit_single_api_call(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {"id": "o1", "side": "BUY", "asset_id": "tok1", "market": "cid1",
             "size_matched": "0", "price": "0.48", "original_size": "500"},
            {"id": "o2", "side": "BUY", "asset_id": "tok2", "market": "cid1",
             "size_matched": "0", "price": "0.48", "original_size": "500"},
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]

        with patch("engine.monitor.needs_replace", return_value="keep"):
            monitor.check_sell_orders()

        assert api.get_rewards_for_market.call_count == 1

    def test_skip_when_rewards_api_fails(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {"id": "o1", "side": "BUY", "asset_id": "tok1", "market": "cid1",
             "size_matched": "0", "price": "0.48", "original_size": "500"},
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.side_effect = Exception("boom")

        monitor.check_sell_orders()

        api.cancel_orders.assert_not_called()
        api.place_limit_buy.assert_not_called()

    def test_skip_when_max_spread_unparseable(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {"id": "o1", "side": "BUY", "asset_id": "tok1", "market": "cid1",
             "size_matched": "0", "price": "0.48", "original_size": "500"},
        ]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{}]

        monitor.check_sell_orders()

        api.cancel_orders.assert_not_called()
        api.place_limit_buy.assert_not_called()
```

- [ ] **Step 2: Run Step 3 tests to verify the new/updated ones fail**

Run: `python -m pytest tests/test_monitor.py::TestCheckSellOrders -v`
Expected: FAIL — `test_uses_real_max_spread_from_rewards_api`, `test_rewards_cache_hit_single_api_call`, `test_skip_when_rewards_api_fails`, `test_skip_when_max_spread_unparseable` fail because `_check_compliance` still hardcodes `max_spread=2` and never calls `get_rewards_for_market`. (`test_replace/cancel/keep` may still pass since market is currently ignored.)

- [ ] **Step 3: Modify `engine/monitor.py`**

(3a) Imports — the top of `engine/monitor.py` is:

```python
"""engine/monitor.py — API-driven fill detection, stop-loss, strategy compliance."""

import logging
from py_clob_client_v2.clob_types import TradeParams
from engine.fills import select_new_buy_fills
from engine.strategy_check import needs_replace
from engine.risk import stop_loss_triggered
from engine.strategy import determine_order_price
```

Change to:

```python
"""engine/monitor.py — API-driven fill detection, stop-loss, strategy compliance."""

import logging
import time
from py_clob_client_v2.clob_types import TradeParams
from engine.fills import select_new_buy_fills
from engine.strategy_check import needs_replace
from engine.risk import stop_loss_triggered
from engine.strategy import determine_order_price
from engine.rewards import extract_max_spread
```

(3b) `__init__` — currently:

```python
    def __init__(self, api, db, wallet_address: str):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address
        # Dedup processed buy fills by (trade_id, order_id).
        self._seen_fill_keys: set = set()
        # Watermark: lower bound for get_trades(after=) — bounds fetch size;
        # real idempotency is _seen_fill_keys.
        self._after_ts: float = 0.0
```

Add the cache field at the end of `__init__`:

```python
    def __init__(self, api, db, wallet_address: str):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address
        # Dedup processed buy fills by (trade_id, order_id).
        self._seen_fill_keys: set = set()
        # Watermark: lower bound for get_trades(after=) — bounds fetch size;
        # real idempotency is _seen_fill_keys.
        self._after_ts: float = 0.0
        # condition_id -> (max_spread, fetched_at) TTL cache for Step 3.
        self._max_spread_cache: dict = {}
```

(3c) Add the `_market_max_spread` method immediately BEFORE `def _check_compliance(self, o: dict):`:

```python
    def _market_max_spread(self, condition_id: str) -> int | None:
        """Real rewards_max_spread for a market, TTL-cached. None if unknown."""
        if not condition_id:
            return None
        ttl = self.db.get_settings()["rewards_cache_ttl_sec"]
        now = time.time()
        hit = self._max_spread_cache.get(condition_id)
        if hit and (now - hit[1]) < ttl:
            return hit[0]
        try:
            items = self.api.get_rewards_for_market(condition_id)
        except Exception as e:
            logger.warning(
                "get_rewards_for_market(%s) failed: %s", condition_id, e
            )
            return None
        ms = extract_max_spread(items)
        if ms is None:
            return None
        self._max_spread_cache[condition_id] = (ms, now)
        return ms
```

(3d) In `_check_compliance`, replace exactly these two lines:

```python
        # rewards_max_spread is not on the order; recover from settings default
        max_spread = 2
```

with:

```python
        max_spread = self._market_max_spread(o.get("market", ""))
        if max_spread is None:
            return  # can't determine real max_spread: skip this tick, never mis-cancel
```

Leave every other line of `_check_compliance` and `check_sell_orders` unchanged (the empty-orderbook check still runs before this, so `test_empty_orderbook_skips_compliance` is unaffected).

- [ ] **Step 4: Run Step 3 tests to verify they pass**

Run: `python -m pytest tests/test_monitor.py::TestCheckSellOrders -v`
Expected: PASS (all TestCheckSellOrders tests, including the 4 new ones)

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (74 prior + 6 from Task 1 + 4 new Step 3 = 84 passed; treat the exact number as whatever `pytest -q` reports with 0 failures)

- [ ] **Step 6: Commit**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "fix: Step 3 uses real per-market rewards_max_spread (TTL cache, skip if unknown)"
```
Footer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

### Task 4: 设置页输入框

**Files:**
- Modify: `web/templates/config.html`

- [ ] **Step 1: Add the input field**

In `web/templates/config.html`, the 「运行参数」grid contains this last group:

```html
            <div class="form-group">
                <label>成交后冷却时间 (分钟)</label>
                <input type="number" name="cooldown_minutes" step="1">
            </div>
        </div>
```

Change to (insert the new form-group before the `</div>` that closes `form-grid`):

```html
            <div class="form-group">
                <label>成交后冷却时间 (分钟)</label>
                <input type="number" name="cooldown_minutes" step="1">
            </div>
            <div class="form-group">
                <label>奖励参数缓存 (秒)</label>
                <input type="number" name="rewards_cache_ttl_sec" step="1">
            </div>
        </div>
```

No JS change: `loadSettings` fills inputs by `name` from `/api/settings` (which returns merged `DEFAULTS`), and submit serializes the form generically.

- [ ] **Step 2: Verify template renders / app imports**

Run: `python -c "import web.routes"`
Expected: exit 0, no output.

- [ ] **Step 3: Run full suite (sanity)**

Run: `python -m pytest -q`
Expected: all pass (same count as end of Task 3).

- [ ] **Step 4: Commit**

```bash
git add web/templates/config.html
git commit -m "feat: 奖励参数缓存 (秒) setting input on config page"
```
Footer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

## Self-Review

**Spec coverage:**
- 纯函数 `extract_max_spread`（顶层字段、空/缺失→None、coerce int）→ Task 1 ✓
- 设置项 `rewards_cache_ttl_sec` 默认 600 → Task 2 ✓；config.html 输入框 → Task 4 ✓
- `OrderMonitor` TTL 缓存 + `_market_max_spread` + `_check_compliance` 用真实值、取不到 return → Task 3 ✓
- 取不到（API 异常 / 解析 None / market 空）跳过不撤不重挂 → Task 3 tests `test_skip_when_rewards_api_fails` / `test_skip_when_max_spread_unparseable`（market 空由 `_market_max_spread` 首行 `if not condition_id` 覆盖，行为等价、已被 None 路径测试覆盖）✓
- 缓存命中不重复请求 → Task 3 `test_rewards_cache_hit_single_api_call` ✓
- 用真实值而非 2 → Task 3 `test_uses_real_max_spread_from_rewards_api` ✓
- 不改 place_orders / strategy.py / strategy_check.py / Step1/2 / are_orders_scoring → 计划未触碰 ✓
- 现有 Step 3 用例更新（原假设 max_spread=2）→ Task 3 Step 1 重写 keep/replace/cancel 三例 ✓

**Placeholder scan:** 无 TBD/TODO；每个代码步骤含完整代码与精确命令、预期输出。测试计数 84 为估算，已注明以 `pytest -q` 实际为准。

**Type consistency:** `extract_max_spread(list) -> Optional[int]`（Task 1）被 `_market_max_spread`（Task 3）调用并返回 `int | None`；`_check_compliance` 对 `None` 提前 `return`；`db.get_settings()["rewards_cache_ttl_sec"]`（Task 3）由 Task 2 的 DEFAULTS 保证恒存在；测试 `_make_monitor` 同步加该键避免 MagicMock 比较报错。一致。
