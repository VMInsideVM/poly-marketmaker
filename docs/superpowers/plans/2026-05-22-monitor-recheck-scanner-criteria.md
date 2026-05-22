# 已挂买单复查 Market Scanner 条件 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 monitor 的 Step 3 在「挂单价合规」之外，对每个完全未成交的 resting BUY 单额外复查 Market Scanner 的四项初筛（奖励金额 / 结算天数 / 价格区间 / 买一卖一价差）；确认不合格只撤该买单，数据缺失则逐维度保守保留。

**Architecture:** 闸门判定抽成纯函数 `engine/eligibility.py`（仿 `engine/strategy_check.py`）。`api/polymarket_api.py` 增加按 condition_id 取结算时间的 `get_market_end_ts`。`engine/monitor.py` 把 `_market_max_spread` 升级为 `_market_rewards_info`（一次返回 max_spread / reward_total / end_ts，分别 TTL 缓存），并在 `_check_compliance` 价格合规之前插入复查。

**Tech Stack:** Python 3.12, pytest, py-clob-client-v2, requests。

**关键设计（务必遵守）：**
- `recheck_resting_buy` 对**缺失的阈值**采用 no-op 安全默认（缺失=不启用该维度）：`min_reward_usd`→0、`min_settlement_days`→0、`min_price_cents`→0、`max_price_cents`→100、`max_spread_cents`→`inf`。这样现有 `tests/test_monitor.py` 的 `default_settings`（不含这四项阈值）会让所有闸门变成 no-op，**现有 Step3 测试无需改动**。
- 任一输入为 `None`（数据未知）→ 跳过该维度（保守保留）。
- `reward_total` 仅当至少有一个 `rewards_config` 项带可解析的 `rate_per_day` 时才是数字；否则为 `None`（未知）。这样现有测试 fixture `[{"rewards_max_spread": 3}]`（无 rate）→ reward_total None → 奖励维度跳过。
- `end_ts` 必须是数值才采用；`MagicMock` 等非数值一律当 `None`。这样现有测试里 `api.get_market_end_ts`（未配置的 MagicMock）不会触发结算维度。

---

### Task 1: 纯函数 `engine/eligibility.py`

**Files:**
- Create: `engine/eligibility.py`
- Test: `tests/test_eligibility.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_eligibility.py`:

```python
"""tests/test_eligibility.py — pure scanner-eligibility re-check for resting buys."""

from engine.eligibility import recheck_resting_buy

# Full thresholds present (mirrors config.DEFAULTS for the relevant keys).
S = {
    "min_reward_usd": 100.0,
    "min_settlement_days": 4,
    "min_price_cents": 10.0,
    "max_price_cents": 50.0,
    "max_spread_cents": 3.0,
}


def test_keeps_when_all_pass():
    cancel, reason = recheck_resting_buy(150.0, 10.0, 30.0, 2.0, S)
    assert cancel is False
    assert reason is None


def test_cancels_when_reward_below_threshold():
    cancel, reason = recheck_resting_buy(50.0, 10.0, 30.0, 2.0, S)
    assert cancel is True
    assert "奖励" in reason


def test_keeps_when_reward_unknown():
    cancel, reason = recheck_resting_buy(None, 10.0, 30.0, 2.0, S)
    assert cancel is False


def test_cancels_when_near_settlement():
    cancel, reason = recheck_resting_buy(150.0, 2.0, 30.0, 2.0, S)
    assert cancel is True
    assert "结算" in reason


def test_keeps_when_days_left_negative():
    # negative days_left = no end date / already passed -> scanner does NOT exclude
    cancel, reason = recheck_resting_buy(150.0, -1.0, 30.0, 2.0, S)
    assert cancel is False


def test_keeps_when_days_left_unknown():
    cancel, reason = recheck_resting_buy(150.0, None, 30.0, 2.0, S)
    assert cancel is False


def test_cancels_when_bid_below_band():
    cancel, reason = recheck_resting_buy(150.0, 10.0, 5.0, 2.0, S)
    assert cancel is True
    assert "区间" in reason


def test_cancels_when_bid_above_band():
    cancel, reason = recheck_resting_buy(150.0, 10.0, 60.0, 2.0, S)
    assert cancel is True
    assert "区间" in reason


def test_keeps_when_bid_at_band_edges():
    assert recheck_resting_buy(150.0, 10.0, 10.0, 2.0, S)[0] is False
    assert recheck_resting_buy(150.0, 10.0, 50.0, 2.0, S)[0] is False


def test_keeps_when_bid_unknown():
    cancel, reason = recheck_resting_buy(150.0, 10.0, None, 2.0, S)
    assert cancel is False


def test_cancels_when_spread_at_or_over_threshold():
    cancel, reason = recheck_resting_buy(150.0, 10.0, 30.0, 3.0, S)  # 3.0 >= 3.0
    assert cancel is True
    assert "价差" in reason


def test_keeps_when_spread_unknown():
    cancel, reason = recheck_resting_buy(150.0, 10.0, 30.0, None, S)
    assert cancel is False


def test_reward_reason_wins_when_multiple_fail():
    # reward checked first
    cancel, reason = recheck_resting_buy(50.0, 2.0, 5.0, 9.0, S)
    assert cancel is True
    assert "奖励" in reason


def test_missing_thresholds_are_noops():
    # No thresholds in settings -> every dimension is a no-op (keep).
    cancel, reason = recheck_resting_buy(0.0, 0.0, 99.0, 99.0, {})
    assert cancel is False
    assert reason is None
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_eligibility.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.eligibility'`

- [ ] **Step 3: 实现纯函数**

Create `engine/eligibility.py`:

```python
# engine/eligibility.py
"""Pure re-check of Market Scanner eligibility for a resting buy (no network/IO).

Mirrors the scanner's filters (engine/scanner.py) so a resting BUY that no
longer meets them can be cancelled. Any unknown input (None) skips that
dimension (conservative keep). A missing threshold in settings makes that
dimension a no-op (keep), so callers that pass a partial settings dict do not
trip gates they didn't configure.
"""

from typing import Optional, Tuple


def recheck_resting_buy(
    reward_total: Optional[float],
    days_left: Optional[float],
    best_bid_cents: Optional[float],
    spread_cents: Optional[float],
    settings: dict,
) -> Tuple[bool, Optional[str]]:
    """Re-check whether a resting BUY still meets scanner eligibility.

    Returns (cancel, reason):
      cancel=True  -> no longer eligible; reason is a zh string for the log.
      cancel=False -> keep; reason is None.

    Inputs (None = unknown -> skip that dimension):
      reward_total   : sum of rate_per_day for the market (USD/day)
      days_left      : days until settlement (negative = no end date / passed)
      best_bid_cents : current best bid * 100
      spread_cents   : (best_ask - best_bid) * 100

    Check order matches scanner: reward -> settlement -> price band -> spread.
    First failing dimension wins.
    """
    min_reward = float(settings.get("min_reward_usd", 0.0))
    min_days = float(settings.get("min_settlement_days", 0))
    min_price_cents = float(settings.get("min_price_cents", 0.0))
    max_price_cents = float(settings.get("max_price_cents", 100.0))
    max_spread_cents = float(settings.get("max_spread_cents", float("inf")))

    # 1. Reward amount (scanner.py:128)
    if reward_total is not None and reward_total < min_reward:
        return True, f"市场日奖励 ${reward_total:.0f} 跌破阈值 ${min_reward:.0f}，撤买单"

    # 2. Settlement days (scanner.py:92) — only the [0, min_days) window excludes;
    #    negative days_left passes, identical to the scanner.
    if days_left is not None and 0 <= days_left < min_days:
        return True, f"距结算仅 {days_left:.1f} 天 < 阈值 {min_days:.0f} 天，撤买单避结算风险"

    # 3. Price band on best_bid (scanner.py:194)
    if best_bid_cents is not None and not (
        min_price_cents <= best_bid_cents <= max_price_cents
    ):
        return True, (
            f"最优买价 {best_bid_cents:.1f}c 跑出区间 "
            f"[{min_price_cents:.0f}c, {max_price_cents:.0f}c]，撤买单"
        )

    # 4. Bid-ask spread (scanner.py:163)
    if spread_cents is not None and spread_cents >= max_spread_cents:
        return True, f"买一卖一价差 {spread_cents:.1f}c ≥ 阈值 {max_spread_cents:.0f}c，撤买单"

    return False, None
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_eligibility.py -v`
Expected: PASS — 14 passed.

- [ ] **Step 5: 提交**

```bash
git add engine/eligibility.py tests/test_eligibility.py
git commit -m "feat: 纯函数 recheck_resting_buy 复查 scanner 初筛(奖励/结算/价格/价差)"
```

---

### Task 2: 结算时间数据源 `get_market_end_ts`

**Files:**
- Modify: `api/polymarket_api.py`（新增模块级 `_end_ts_from_market` + 类方法 `get_market` / `get_market_end_ts`）
- Test: `tests/test_market_end_ts.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_market_end_ts.py`:

```python
"""tests/test_market_end_ts.py — pure parsing of CLOB market end date."""

from api.polymarket_api import _end_ts_from_market


def test_parses_iso_z():
    ts = _end_ts_from_market({"end_date_iso": "2099-01-01T00:00:00Z"})
    assert ts is not None
    assert ts > 4_000_000_000  # well past 2096


def test_parses_iso_offset():
    ts = _end_ts_from_market({"end_date_iso": "2099-01-01T00:00:00+00:00"})
    assert ts is not None
    assert ts > 4_000_000_000


def test_parses_date_only():
    ts = _end_ts_from_market({"end_date_iso": "2099-01-01"})
    assert ts is not None
    assert ts > 4_000_000_000


def test_none_when_field_missing():
    assert _end_ts_from_market({}) is None
    assert _end_ts_from_market({"end_date_iso": ""}) is None


def test_none_when_unparseable():
    assert _end_ts_from_market({"end_date_iso": "not-a-date"}) is None


def test_none_when_not_a_dict():
    assert _end_ts_from_market(None) is None
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_market_end_ts.py -v`
Expected: FAIL — `ImportError: cannot import name '_end_ts_from_market'`

- [ ] **Step 3: 实现**

In `api/polymarket_api.py`, ensure datetime import exists near the top (add this line with the other stdlib imports if not already present):

```python
from datetime import datetime, timezone
```

Add this module-level function near the top of the file (after imports, before the class):

```python
def _end_ts_from_market(info) -> "float | None":
    """Parse a CLOB market's end_date_iso into a Unix timestamp.

    Returns None when info isn't a dict, the field is absent/empty, or the
    value can't be parsed. Callers treat None as 'settlement date unknown'.
    """
    if not isinstance(info, dict):
        return None
    s = (info.get("end_date_iso") or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None
```

Add these two methods to the `PolymarketAPI` class (place them right after `get_order` / near the other CLOB read methods, e.g. after `get_open_orders`):

```python
    def get_market(self, condition_id: str) -> dict:
        """CLOB market info by condition_id (includes end_date_iso)."""
        return self.client.get_market(condition_id)

    def get_market_end_ts(self, condition_id: str) -> "float | None":
        """Settlement time (Unix seconds) for a market; None if unavailable.

        Reads end_date_iso via CLOB get_market(condition_id). Any failure /
        missing field returns None (caller treats as 'settlement unknown ->
        skip that dimension').
        """
        try:
            return _end_ts_from_market(self.get_market(condition_id))
        except Exception as e:
            logger.warning("get_market_end_ts(%s) failed: %s", condition_id, e)
            return None
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_market_end_ts.py -v`
Expected: PASS — 6 passed.

- [ ] **Step 5: 手测确认真实字段名（重要）**

CLOB `get_market` 的结算字段名需对真实 API 确认。运行一次性脚本（需已安装依赖；不依赖钱包私钥，因为只调公共读接口——若 `get_market` 需要客户端实例，用任一已配置钱包跑）：

```python
# 临时脚本（确认后删除），示例：
# from py_clob_client_v2.client import ClobClient
# c = ClobClient(host=..., chain_id=...)
# print(c.get_market("<某个真实 condition_id>"))
```

确认返回里结算字段确为 `end_date_iso`。若字段名不同，改 `_end_ts_from_market` 里的 key 并更新 `tests/test_market_end_ts.py`。若 `get_market` 不可用，保留实现（异常→None 自动降级，仅结算维度失效），并在提交信息中注明。

- [ ] **Step 6: 提交**

```bash
git add api/polymarket_api.py tests/test_market_end_ts.py
git commit -m "feat: PolymarketAPI.get_market_end_ts 按 condition_id 取结算时间"
```

---

### Task 3: monitor 新增 `_market_rewards_info`（不删旧方法）

**Files:**
- Modify: `engine/monitor.py`（`OrderMonitor.__init__` 加两个缓存；新增 `_market_rewards_info`，**保留** `_market_max_spread` 直到 Task 4）
- Test: `tests/test_monitor.py`（新增类 `TestMarketRewardsInfo`）

- [ ] **Step 1: 写失败测试**

In `tests/test_monitor.py`, append a new test class at the end of the file:

```python
class TestMarketRewardsInfo:
    def test_returns_max_spread_and_reward_total(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 40}, {"rate_per_day": 60}]}
        ]
        api.get_market_end_ts.return_value = 4_100_000_000.0
        info = monitor._market_rewards_info("cid1")
        assert info["max_spread"] == 3
        assert info["reward_total"] == 100.0
        assert info["end_ts"] == 4_100_000_000.0

    def test_reward_total_none_when_no_rate_present(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.get_market_end_ts.return_value = None
        info = monitor._market_rewards_info("cid1")
        assert info["reward_total"] is None
        assert info["max_spread"] == 3

    def test_reward_total_none_when_items_empty(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = []
        api.get_market_end_ts.return_value = None
        info = monitor._market_rewards_info("cid1")
        assert info["reward_total"] is None
        assert info["max_spread"] is None

    def test_end_ts_none_when_non_numeric(self):
        # A bare MagicMock api (get_market_end_ts unset) returns a MagicMock;
        # it must be treated as None, not a number.
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        # deliberately do NOT set api.get_market_end_ts.return_value
        info = monitor._market_rewards_info("cid1")
        assert info["end_ts"] is None

    def test_rewards_failure_yields_none_fields(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.side_effect = Exception("boom")
        api.get_market_end_ts.return_value = None
        info = monitor._market_rewards_info("cid1")
        assert info["max_spread"] is None
        assert info["reward_total"] is None

    def test_caches_within_ttl_single_api_call(self):
        monitor, api, db = _make_monitor()
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 200}]}
        ]
        api.get_market_end_ts.return_value = 4_100_000_000.0
        monitor._market_rewards_info("cid1")
        monitor._market_rewards_info("cid1")
        assert api.get_rewards_for_market.call_count == 1
        assert api.get_market_end_ts.call_count == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_monitor.py::TestMarketRewardsInfo -v`
Expected: FAIL — `AttributeError: 'OrderMonitor' object has no attribute '_market_rewards_info'`

- [ ] **Step 3: 实现**

In `engine/monitor.py`, in `OrderMonitor.__init__`, add the two caches next to the existing `_max_spread_cache` line:

```python
        # condition_id -> (max_spread, fetched_at) TTL cache for Step 3.
        self._max_spread_cache: dict = {}
        # condition_id -> ((max_spread, reward_total), fetched_at) and
        # condition_id -> (end_ts, fetched_at): independent TTL caches for the
        # Step 3 scanner-eligibility re-check.
        self._rewards_cache: dict = {}
        self._end_ts_cache: dict = {}
```

Add the new method right after `_market_max_spread` (do NOT remove `_market_max_spread` yet — Task 4 removes it):

```python
    def _market_rewards_info(self, condition_id: str) -> dict:
        """Per-market {"max_spread", "reward_total", "end_ts"}, each None when
        unavailable. max_spread + reward_total share one rewards cache; end_ts
        has its own. Only successful fetches are cached (TTL=rewards_cache_ttl_sec)
        so a transient failure is retried next tick rather than stuck for the TTL.
        reward_total is None unless at least one rewards_config carries a parseable
        rate_per_day (so 'no rate info' is treated as unknown, not zero)."""
        out = {"max_spread": None, "reward_total": None, "end_ts": None}
        if not condition_id:
            return out
        ttl = self.db.get_settings()["rewards_cache_ttl_sec"]
        now = time.time()

        rhit = self._rewards_cache.get(condition_id)
        if rhit and (now - rhit[1]) < ttl:
            out["max_spread"], out["reward_total"] = rhit[0]
        else:
            try:
                items = self.api.get_rewards_for_market(condition_id)
            except Exception as e:
                logger.warning("get_rewards_for_market(%s) failed: %s", condition_id, e)
                items = None
            if items:
                ms = extract_max_spread(items)
                rt, seen = 0.0, False
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    for rc in (it.get("rewards_config") or []):
                        v = rc.get("rate_per_day")
                        if v is None:
                            continue
                        try:
                            rt += float(v)
                            seen = True
                        except (TypeError, ValueError):
                            continue
                reward_total = rt if seen else None
                out["max_spread"], out["reward_total"] = ms, reward_total
                self._rewards_cache[condition_id] = ((ms, reward_total), now)

        ehit = self._end_ts_cache.get(condition_id)
        if ehit and (now - ehit[1]) < ttl:
            out["end_ts"] = ehit[0]
        else:
            try:
                ets = self.api.get_market_end_ts(condition_id)
            except Exception as e:
                logger.warning("get_market_end_ts(%s) failed: %s", condition_id, e)
                ets = None
            if isinstance(ets, (int, float)) and not isinstance(ets, bool):
                out["end_ts"] = float(ets)
                self._end_ts_cache[condition_id] = (float(ets), now)

        return out
```

- [ ] **Step 4: 运行确认通过（并确认未碰坏其它监控测试）**

Run: `pytest tests/test_monitor.py -v`
Expected: PASS — `TestMarketRewardsInfo`（6）全绿，且既有 `TestCheckSellOrders` / `TestStep3ActionLog` / `TestMonitorStatusSnapshot` 等全部仍绿（本任务只新增方法，未改 `_check_compliance`）。

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat: OrderMonitor._market_rewards_info(max_spread+reward_total+end_ts, 分别TTL缓存)"
```

---

### Task 4: 把复查并入 `_check_compliance`

**Files:**
- Modify: `engine/monitor.py`（重写 `_check_compliance`，使用 `_market_rewards_info` + `recheck_resting_buy`；删除现已无用的 `_market_max_spread` 和 `_max_spread_cache`；加 import）
- Test: `tests/test_monitor.py`（新增类 `TestStep3EligibilityRecheck`）

- [ ] **Step 1: 写失败测试**

In `tests/test_monitor.py`, append a new test class at the end:

```python
class TestStep3EligibilityRecheck:
    # Full scanner thresholds so the gates are active.
    THRESH = {
        "min_reward_usd": 100.0,
        "min_settlement_days": 4,
        "min_price_cents": 10.0,
        "max_price_cents": 50.0,
        "max_spread_cents": 3.0,
    }

    def _ob(self, best_bid="0.30", best_ask="0.31", tick="0.01"):
        return {
            "bids": [{"price": best_bid, "size": "1000"}],
            "asks": [{"price": best_ask, "size": "1000"}],
            "tick_size": tick,
        }

    def _buy(self):
        return [{
            "id": "o1", "side": "BUY", "asset_id": "tok1", "market": "cid1",
            "size_matched": "0", "price": "0.30", "original_size": "500",
        }]

    def _far_future(self):
        import time as _t
        return _t.time() + 60 * 86400  # 60 days out

    def test_cancels_when_reward_below_threshold(self):
        monitor, api, db = _make_monitor(self.THRESH)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 50}]}
        ]
        api.get_market_end_ts.return_value = self._far_future()
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])
        api.place_limit_buy.assert_not_called()

    def test_cancels_when_near_settlement(self):
        import time as _t
        monitor, api, db = _make_monitor(self.THRESH)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 500}]}
        ]
        api.get_market_end_ts.return_value = _t.time() + 1 * 86400  # 1 day < 4
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])

    def test_cancels_when_bid_out_of_band(self):
        monitor, api, db = _make_monitor(self.THRESH)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(best_bid="0.60", best_ask="0.61")
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 500}]}
        ]
        api.get_market_end_ts.return_value = self._far_future()
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])

    def test_cancels_when_spread_too_wide(self):
        monitor, api, db = _make_monitor(self.THRESH)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob(best_bid="0.30", best_ask="0.35")  # 5c >= 3c
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 500}]}
        ]
        api.get_market_end_ts.return_value = self._far_future()
        monitor.check_sell_orders()
        api.cancel_orders.assert_called_once_with(["o1"])

    def test_records_eligibility_cancel_action(self):
        monitor, api, db = _make_monitor(self.THRESH)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 50}]}
        ]
        api.get_market_end_ts.return_value = self._far_future()
        monitor.check_sell_orders()
        ats = [c.kwargs.get("action_type") for c in db.record_action.call_args_list]
        assert "eligibility_cancel" in ats

    def test_keeps_and_runs_compliance_when_all_pass(self):
        monitor, api, db = _make_monitor(self.THRESH)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob()  # bid 30c in band, spread 1c
        api.get_rewards_for_market.return_value = [
            {"rewards_max_spread": 3, "rewards_config": [{"rate_per_day": 500}]}
        ]
        api.get_market_end_ts.return_value = self._far_future()
        with patch("engine.monitor.needs_replace", return_value="keep"):
            monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()

    def test_keeps_when_reward_and_end_ts_unknown(self):
        # rewards items lack rate, end_ts unknown -> those gates skipped;
        # bid in band & spread narrow -> keep -> falls to compliance.
        monitor, api, db = _make_monitor(self.THRESH)
        api.get_open_orders.return_value = self._buy()
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.get_market_end_ts.return_value = None
        with patch("engine.monitor.needs_replace", return_value="keep"):
            monitor.check_sell_orders()
        api.cancel_orders.assert_not_called()
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_monitor.py::TestStep3EligibilityRecheck -v`
Expected: FAIL — 当前 `_check_compliance` 不做复查；`test_cancels_when_*` 因 `cancel_orders` 未被调用（或被 `needs_replace` 真实逻辑影响）而失败。

- [ ] **Step 3: 实现 — 加 import**

In `engine/monitor.py`, add the import next to the other engine imports (after the `from engine.strategy_check import needs_replace` line):

```python
from engine.eligibility import recheck_resting_buy
```

- [ ] **Step 4: 实现 — 重写 `_check_compliance`**

Replace the entire `_check_compliance` method (the current `def _check_compliance(self, o):` body) with:

```python
    def _check_compliance(self, o: dict):
        token_id = o.get("asset_id", "")
        cid = o.get("market", "")
        settings = self.db.get_settings()
        ob = self.api.get_orderbook(token_id)
        bids = sorted(ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        best_bid_cents = best_bid * 100 if best_bid is not None else None
        spread_cents = (
            (best_ask - best_bid) * 100
            if (best_bid is not None and best_ask is not None)
            else None
        )

        # Scanner-eligibility re-check (runs even if the book is empty: reward /
        # settlement still apply). reward_total / max_spread / end_ts are fetched
        # once here and the max_spread is reused by the price-compliance path.
        info = self._market_rewards_info(cid)
        max_spread = info["max_spread"]
        end_ts = info["end_ts"]
        days_left = ((end_ts - time.time()) / 86400) if end_ts else None
        cancel, reason = recheck_resting_buy(
            info["reward_total"], days_left, best_bid_cents, spread_cents, settings
        )
        if cancel:
            old_price = float(o.get("price", 0) or 0)
            osize = int(float(o.get("original_size", 0) or 0))
            try:
                self.api.cancel_orders([o["id"]])
            except Exception as e:
                logger.warning("Eligibility cancel %s failed: %s", o.get("id"), e)
                return
            self._record_action(
                market_id=cid,
                action_type="eligibility_cancel",
                side="-",
                price=-1,
                size=osize,
                reason=reason,
                price_basis=(
                    "市场已不满足扫描初筛(奖励/结算/价格/价差)；"
                    "来源：CLOB get_orderbook + get_rewards_for_market + get_market"
                ),
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{old_price:.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="复查撤单(市场不合格)",
                detail=reason,
            )
            logger.info("[Step3] eligibility cancel %s market %s: %s", o.get("id"), cid, reason)
            return

        # --- price compliance (unchanged behavior) ---
        if not bids or not asks:
            logger.info(
                "[Step3] 单 %s 市场 %s | 盘口为空，本轮跳过",
                o.get("id"),
                cid,
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{float(o.get('price', 0) or 0):.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="跳过(盘口为空)",
                detail="盘口为空",
            )
            return
        midpoint = (best_bid + best_ask) / 2
        tick = float(ob.get("tick_size", "0.01"))
        tick_str = ob.get("tick_size", "0.01")
        if max_spread is None:
            logger.info(
                "[Step3] 单 %s 市场 %s 现价 %.4f | 取不到 rewards_max_spread，"
                "本轮跳过（不撤不重挂）",
                o.get("id"),
                cid,
                float(o.get("price", 0) or 0),
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{float(o.get('price', 0) or 0):.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="跳过(取不到max_spread)",
                detail="取不到 rewards_max_spread",
            )
            return
        rmin = midpoint - max_spread * tick
        rmax = midpoint + max_spread * tick
        try:
            want = determine_order_price(
                bids=bids,
                max_spread=max_spread,
                tick_size=tick,
                reward_range_min=rmin,
                reward_range_max=rmax,
            )
        except Exception as e:
            logger.warning("determine_order_price failed for %s: %s", o.get("id"), e)
            return
        action = needs_replace(float(o.get("price", 0)), want, tick)
        want_str = "无" if want is None else f"{want:.4f}"
        action_zh = {
            "keep": "keep → 保持不动",
            "replace": f"replace → 撤单并重挂 {want_str}",
            "cancel": "cancel → 撤单不重挂",
        }.get(action, action)
        logger.info(
            "[Step3] 单 %s 市场 %s 现价 %.4f | 盘口 bid %.4f ask %.4f mid %.4f "
            "tick %.4f | max_spread=%d 区间[%.4f,%.4f] | 应挂价 %s | 判定 %s",
            o.get("id"),
            cid,
            float(o.get("price", 0) or 0),
            best_bid,
            best_ask,
            midpoint,
            tick,
            max_spread,
            rmin,
            rmax,
            ("无" if want is None else f"{want:.4f}"),
            action_zh,
        )
        self._status_add(
            market=cid,
            side="买入",
            price=f"{float(o.get('price', 0) or 0):.4f}",
            size=str(o.get("original_size", "")),
            matched=str(o.get("size_matched", "0")),
            stage="Step3",
            action=action_zh,
            detail=(
                f"bid{best_bid:.4f} ask{best_ask:.4f} mid{midpoint:.4f} "
                f"ms{max_spread} 区间[{rmin:.4f},{rmax:.4f}] 应挂{want_str}"
            ),
        )
        if action == "keep":
            return
        old_price = float(o.get("price", 0) or 0)
        osize = int(float(o.get("original_size", 0) or 0))
        basis = (
            f"旧价 {old_price:.4f}；区间[{rmin:.4f},{rmax:.4f}] "
            f"mid{midpoint:.4f} ms{max_spread} tick{tick:.4f}；"
            f"来源：CLOB get_orderbook + get_rewards_for_market"
        )
        try:
            self.api.cancel_orders([o["id"]])
        except Exception as e:
            logger.warning("Cancel %s failed: %s", o.get("id"), e)
            return
        if action == "replace":
            self._record_action(
                market_id=cid,
                action_type="step3_cancel_old",
                side="-",
                price=-1,
                size=osize,
                reason=f"挂单价 {old_price:.4f} 不在最新奖励区间内，撤旧买单准备重挂",
                price_basis=basis,
            )
            neg_risk = bool(o.get("neg_risk", False))
            self.api.place_limit_buy(
                token_id, want, osize, tick_size=tick_str, neg_risk=neg_risk
            )
            self._record_action(
                market_id=cid,
                action_type="step3_replace_new",
                side="买入",
                price=want,
                size=osize,
                reason="按策略在奖励区间内重挂买单（贴最优买价深度，最大化奖励占比）",
                price_basis=(
                    f"应挂价 {want:.4f}=determine_order_price(bids, "
                    f"ms{max_spread}, tick{tick:.4f}, "
                    f"区间[{rmin:.4f},{rmax:.4f}])；"
                    f"来源：CLOB get_orderbook + get_rewards_for_market"
                ),
            )
            logger.info("Replaced buy %s -> %.4f", o.get("id"), want)
        else:
            self._record_action(
                market_id=cid,
                action_type="step3_cancel_nocompliant",
                side="-",
                price=-1,
                size=osize,
                reason="奖励区间内无合规价，撤该买单（不重挂）",
                price_basis=basis,
            )
            logger.info("Cancelled non-compliant buy %s (no valid price)", o.get("id"))
```

- [ ] **Step 5: 实现 — 删除旧的 `_market_max_spread` 与 `_max_spread_cache`**

Delete the now-unused `_market_max_spread` method entirely. In `__init__`, delete the `self._max_spread_cache: dict = {}` line and its comment (keep `_rewards_cache` / `_end_ts_cache` added in Task 3).

Verify nothing else references them:

Run: `grep -rn "_market_max_spread\|_max_spread_cache" engine/ tests/`
Expected: no matches (all references removed).

- [ ] **Step 6: 运行新测试确认通过**

Run: `pytest tests/test_monitor.py::TestStep3EligibilityRecheck -v`
Expected: PASS — 7 passed.

- [ ] **Step 7: 运行整个 monitor 测试确认无回归**

Run: `pytest tests/test_monitor.py -v`
Expected: PASS — `TestStep3EligibilityRecheck`（7）+ `TestMarketRewardsInfo`（6）全绿，且既有 `TestCheckSellOrders` / `TestStep3ActionLog` / `TestMonitorStatusSnapshot` 等全部仍绿（它们的 `default_settings` 不含四项阈值 → 闸门皆 no-op；fixture 无 rate → reward_total None；`get_market_end_ts` 未配置 MagicMock → end_ts None）。

- [ ] **Step 8: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat: Step3 在价格合规前复查 scanner 初筛(不合格撤单,数据缺失保留)"
```

---

### Task 5: 全量测试 + 文档更新

**Files:**
- Modify: `CLAUDE.md`（更新 Step 3 行为描述）

- [ ] **Step 1: 全量测试**

Run: `pytest -q`
Expected: PASS — 全部通过（基线为 Task 0 前的数量 + 本计划新增用例：eligibility 14 + market_end_ts 6 + TestMarketRewardsInfo 6 + TestStep3EligibilityRecheck 7）。

- [ ] **Step 2: 更新 CLAUDE.md 的 Step 3 描述**

In `CLAUDE.md`, find the sentence describing Step 3 (in the "Pipeline" paragraph):

> Step 3 re-runs `determine_order_price` on resting buys and replaces/cancels any whose price no longer matches the current tick.

Replace it with:

> Step 3 first re-checks Market Scanner eligibility on each resting buy (reward ≥ threshold, settlement days, price band, bid-ask spread — `engine/eligibility.py`, sourced from live orderbook + `get_rewards_for_market` + `get_market_end_ts`) and cancels (cancel-only, no cooldown) any market that no longer qualifies; unknown data for a dimension is skipped (conservative keep). Surviving buys then go through `determine_order_price` and are replaced/cancelled if their price no longer matches the current tick.

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md 说明 Step3 复查 scanner 初筛"
```

---

## 自审清单（实现者完成后自查）

- [ ] `recheck_resting_buy` 四维度阈值键名与 `config.py` DEFAULTS 一致：`min_reward_usd` / `min_settlement_days` / `min_price_cents` / `max_price_cents` / `max_spread_cents`。
- [ ] 缺失阈值 = no-op；None 输入 = 跳过。
- [ ] `_market_rewards_info` 始终返回 dict，三字段独立 None，仅成功写缓存。
- [ ] `reward_total` 仅在有可解析 `rate_per_day` 时为数字，否则 None。
- [ ] `end_ts` 仅接受数值（排除 bool / MagicMock）。
- [ ] `_check_compliance` 重写后：复查在价格合规之前；复查通过才走原逻辑；空盘口/无 max_spread 的跳过分支与原行为一致。
- [ ] `_market_max_spread` 与 `_max_spread_cache` 已删除且无残留引用。
- [ ] 既有 Step3 测试未改动且全绿。
