# 测试挂单（Test Place Orders）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在仪表盘新增"测试挂单"按钮，用第一个启用钱包遍历 eligible 市场直到成功挂出 3 个符合策略的买单。

**Architecture:** 给 `WalletWorker.place_orders` 加可选 `limit` 早停参数（默认 `None` = 现状不变）；新增 `EngineManager.test_place_orders()` 选第一个启用且 running 的 worker 调用 `place_orders(sorted, limit=3)`；新增 POST 路由与仪表盘按钮。复用已启动的 worker，使成交后现有监控自动挂止盈、写历史。

**Tech Stack:** Python 3 / Flask / pytest / unittest.mock；`py_clob_client_v2`（不直接触碰，全程 mock）。

参考 spec：`docs/superpowers/specs/2026-05-19-test-place-orders-design.md`

---

### Task 1: `WalletWorker.place_orders` 增加 `limit` 早停参数

**Files:**
- Modify: `engine/manager.py:72-150`（`WalletWorker.place_orders`）
- Test: `tests/test_place_orders.py`（Create）

- [ ] **Step 1: Write the failing tests**

Create `tests/test_place_orders.py`:

```python
"""tests/test_place_orders.py"""

from unittest.mock import MagicMock, patch
from engine.manager import WalletWorker


def _worker(api, db):
    return WalletWorker(api, db, "0xWALLET", {"fill_check_interval_sec": 5})


def _market(i):
    return {
        "market_id": f"m{i}",
        "market_name": f"Market {i}",
        "token_id": f"t{i}",
        "outcome": "YES",
        "order_size": 10,
        "rewards_max_spread": 2,
        "neg_risk": False,
        "market_competitiveness": 0.0,
    }


def _ok_orderbook():
    return {
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.42", "size": "100"}],
        "tick_size": "0.01",
    }


def test_limit_stops_after_n_successful_placements():
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    markets = [_market(i) for i in range(5)]
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders(markets, limit=3)
    assert api.place_limit_buy.call_count == 3


def test_no_limit_places_on_all_markets_regression():
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    markets = [_market(i) for i in range(5)]
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders(markets)
    assert api.place_limit_buy.call_count == 5


def test_skipped_markets_do_not_count_toward_limit():
    api = MagicMock()
    db = MagicMock()
    # m0 in cooldown (skip, no count); m1 price None (skip, no count);
    # m2..m6 placeable. limit=3 -> place on m2,m3,m4 then stop.
    db.is_in_cooldown.side_effect = lambda w, mid: mid == "m0"
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    markets = [_market(i) for i in range(7)]

    # m0 is skipped by cooldown (determine_order_price never called for it).
    # determine_order_price is called for m1.. — first returns None (skip,
    # no count), the rest return a valid price.
    prices = [None, 0.40, 0.40, 0.40, 0.40, 0.40]
    with patch("engine.strategy.determine_order_price", side_effect=prices):
        worker.place_orders(markets, limit=3)
    assert api.place_limit_buy.call_count == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_place_orders.py -v`
Expected: FAIL — `test_limit_stops_after_n_successful_placements` and `test_skipped_markets_do_not_count_toward_limit` fail because `place_orders` ignores `limit` (places 5 / >3 times); `test_no_limit_places_on_all_markets_regression` should PASS.

- [ ] **Step 3: Add `limit` parameter and early-stop**

In `engine/manager.py`, change the `place_orders` signature and add a success counter. Current signature line:

```python
    def place_orders(self, eligible_markets: list[dict]):
        """Place orders on eligible markets; price recomputed at placement time."""
        from engine.strategy import determine_order_price
```

Replace with:

```python
    def place_orders(self, eligible_markets: list[dict], limit: int | None = None):
        """Place orders on eligible markets; price recomputed at placement time.

        If ``limit`` is set, stop after that many *successful* placements
        (a placement counts only when ``place_limit_buy`` succeeds).
        """
        from engine.strategy import determine_order_price

        placed = 0
```

Then locate the successful-placement branch (the `try:` that calls `self.api.place_limit_buy(...)` and logs "Placed buy ..."). It currently ends:

```python
            except Exception as e:
                logger.error("Error placing order for %s: %s", market["market_name"], e)
```

Change that `try/except` block so the counter increments only on success and breaks at the limit:

```python
            try:
                self.api.place_limit_buy(
                    market["token_id"],
                    order_price,
                    market["order_size"],
                    tick_size=tick_str,
                    neg_risk=market.get("neg_risk", False),
                )
                logger.info(
                    "Placed buy %s [%s] @ %.4f x %d",
                    market["market_name"],
                    market["outcome"],
                    order_price,
                    market["order_size"],
                )
                placed += 1
                if limit is not None and placed >= limit:
                    break
            except Exception as e:
                logger.error("Error placing order for %s: %s", market["market_name"], e)
```

Leave every other line of `place_orders` unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_place_orders.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the full suite (regression)**

Run: `pytest -q`
Expected: all pass (previous count + 3 new = 68 passed)

- [ ] **Step 6: Commit**

```bash
git add engine/manager.py tests/test_place_orders.py
git commit -m "feat: place_orders accepts limit for early-stop after N successful placements"
```

---

### Task 2: `EngineManager.test_place_orders()`

**Files:**
- Modify: `engine/manager.py`（在 `place_all_orders` 之后新增方法）
- Test: `tests/test_manager.py`（Modify — 追加用例）

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_manager.py`:

```python
class TestTestPlaceOrders:
    def test_no_eligible_markets_returns_scan_hint(self):
        manager, db = _make_manager()
        manager.eligible_markets = []
        result = manager.test_place_orders()
        assert result == {"ok": False, "message": "请先扫描市场"}

    def test_no_running_worker_returns_monitor_hint(self):
        manager, db = _make_manager()
        manager.eligible_markets = [{"market_competitiveness": 0.1}]
        # No workers started -> engines empty
        result = manager.test_place_orders()
        assert result == {"ok": False, "message": "请先启动监控"}

    def test_places_on_first_enabled_running_worker_with_limit_3(self):
        manager, db = _make_manager()
        manager.eligible_markets = [
            {"market_competitiveness": 0.9, "name": "high"},
            {"market_competitiveness": 0.1, "name": "low"},
        ]
        worker = MagicMock()
        worker.running = True
        # db.list_wallets()[0] is 0xABC (enabled) per _make_manager
        manager.engines = {"0xABC": worker}
        result = manager.test_place_orders()
        assert result["ok"] is True
        worker.place_orders.assert_called_once()
        args, kwargs = worker.place_orders.call_args
        passed_markets = args[0]
        # sorted ascending by competitiveness: low (0.1) before high (0.9)
        assert [m["name"] for m in passed_markets] == ["low", "high"]
        assert kwargs.get("limit") == 3

    def test_skips_disabled_or_not_running_picks_first_valid(self):
        manager, db = _make_manager()
        db.list_wallets.return_value = [
            {"address": "0xABC", "encrypted_key": "e1", "enabled": 0},
            {"address": "0xDEF", "encrypted_key": "e2", "enabled": 1},
        ]
        manager.eligible_markets = [{"market_competitiveness": 0.5, "name": "m"}]
        stopped = MagicMock()
        stopped.running = False
        good = MagicMock()
        good.running = True
        manager.engines = {"0xABC": stopped, "0xDEF": good}
        result = manager.test_place_orders()
        assert result["ok"] is True
        good.place_orders.assert_called_once()
        stopped.place_orders.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_manager.py::TestTestPlaceOrders -v`
Expected: FAIL with `AttributeError: 'EngineManager' object has no attribute 'test_place_orders'`

- [ ] **Step 3: Implement `test_place_orders`**

In `engine/manager.py`, immediately after the `place_all_orders` method (it ends with the `logger.info("Distributed %d eligible markets ...")` call, before `def start_wallet`), add:

```python
    def test_place_orders(self) -> dict:
        """Place up to 3 strategy-compliant test buys on the first enabled,
        running wallet, iterating eligible markets until 3 succeed."""
        if not self.eligible_markets:
            return {"ok": False, "message": "请先扫描市场"}

        sorted_markets = sorted(
            self.eligible_markets,
            key=lambda m: float(m.get("market_competitiveness", 0) or 0),
        )

        worker = None
        for w in self.db.list_wallets():
            if not w["enabled"]:
                continue
            candidate = self.engines.get(w["address"])
            if candidate and candidate.running:
                worker = candidate
                break
        if worker is None:
            return {"ok": False, "message": "请先启动监控"}

        worker.place_orders(sorted_markets, limit=3)
        return {
            "ok": True,
            "message": "已对符合策略的市场提交最多 3 个测试买单，请到订单管理查看",
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_manager.py::TestTestPlaceOrders -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all pass (72 passed)

- [ ] **Step 6: Commit**

```bash
git add engine/manager.py tests/test_manager.py
git commit -m "feat: EngineManager.test_place_orders (first enabled running wallet, limit 3)"
```

---

### Task 3: 路由 + 仪表盘按钮

**Files:**
- Modify: `web/routes.py`（在 `api_cancel_all_buy` 路由附近新增路由）
- Modify: `web/templates/dashboard.html`（按钮 + JS）

- [ ] **Step 1: Add the route**

In `web/routes.py`, immediately after the `api_cancel_all_buy` function (the block ending with `return jsonify({"ok": True})` for `/api/engine/cancel-all`), add:

```python
@app.route("/api/engine/test-place-orders", methods=["POST"])
@login_required
def api_test_place_orders():
    if not manager:
        return jsonify({"ok": False, "message": "引擎未启动"})
    return jsonify(manager.test_place_orders())
```

- [ ] **Step 2: Verify the route imports cleanly**

Run: `python -c "import web.routes"`
Expected: no output, exit 0 (no syntax/import error)

- [ ] **Step 3: Add the dashboard button**

In `web/templates/dashboard.html`, find the existing button:

```html
        <button class="btn btn-sm btn-success" onclick="placeOrders()">分发挂单</button>
```

Add a button right after it:

```html
        <button class="btn btn-sm btn-success" onclick="placeOrders()">分发挂单</button>
        <button class="btn btn-sm btn-warning" onclick="testPlaceOrders()">测试挂单</button>
```

- [ ] **Step 4: Add the JS handler**

In `web/templates/dashboard.html`, find the existing `placeOrders` function. It looks like:

```javascript
    fetch('/api/engine/place-orders', {method: 'POST'}).then(r => r.json()).then(data => {
```

Locate the full `function placeOrders() { ... }` enclosing that line. Immediately after that function's closing `}`, add:

```javascript
function testPlaceOrders() {
    if (!confirm('将用第一个启用钱包的真实资金，最多挂出 3 个测试买单，确认？')) return;
    fetch('/api/engine/test-place-orders', {method: 'POST'})
        .then(r => r.json())
        .then(data => { alert(data.message || (data.ok ? '已提交' : '失败')); });
}
```

(If the page uses a shared status/toast element instead of `alert` in `placeOrders`, match that pattern instead — read the existing `placeOrders` body and mirror its success/error display mechanism with `data.message`.)

- [ ] **Step 5: Manual smoke check (no automated UI test)**

Run: `python -c "import web.routes"` again to confirm still imports.
Expected: exit 0.

Note: full manual verification requires the running app with an unlocked wallet and live network — out of scope for automated tests; the route is exercised via `manager.test_place_orders()` unit tests in Task 2.

- [ ] **Step 6: Commit**

```bash
git add web/routes.py web/templates/dashboard.html
git commit -m "feat: 测试挂单 button + /api/engine/test-place-orders route"
```

---

## Self-Review

**Spec coverage:**
- 目标/前 3 单遍历早停 → Task 1 ✓
- 前置条件（先扫描、先启动监控）+ 第一个启用 running 钱包选择 + 竞争度升序排序 → Task 2 ✓
- 路由 `POST /api/engine/test-place-orders` → Task 3 ✓
- 仪表盘按钮 + 确认框 + message 展示 → Task 3 ✓
- "成功"定义/skip 不计数 → Task 1 测试 `test_skipped_markets_do_not_count_toward_limit` ✓
- 不改 `place_all_orders`、不写 DB、不加配置（YAGNI）→ 计划未触碰这些 ✓
- 回归保护（`limit=None` 行为不变）→ Task 1 `test_no_limit_places_on_all_markets_regression` ✓
- 可见性说明属背景信息（订单管理实时 API / 历史需成交），无需代码任务 ✓

**Placeholder scan:** Task 3 Step 4 含一处条件性说明（若页面用 toast 而非 alert），已给出明确的镜像指令而非占位符——实现者读现有 `placeOrders` 即可决定，可接受。其余步骤均含完整代码/命令/预期输出。

**Type consistency:** `place_orders(eligible_markets, limit=None)` 在 Task 1 定义，Task 2 以 `worker.place_orders(sorted_markets, limit=3)` 调用，签名一致；`test_place_orders()` 返回 `{"ok": bool, "message": str}`，Task 2 测试与 Task 3 路由消费方式一致。

测试计数（68→72）为估算，以实际 `pytest -q` 输出为准。
