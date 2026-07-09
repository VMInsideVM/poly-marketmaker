# 操作记录持久化 + 历史卖单理由 + 监控状态钱包筛选 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增持久化 `actions` 表记录监控的每个改单/撤单/卖单动作（含原因与价格依据）；Step1 把止盈卖单补记进主 `trades` 表使方向列完整；历史页加"卖单理由"区，监控状态页加钱包下拉与可查"操作记录"区。

**Architecture:** `models/database.py` 新增 `actions` 表 + `record_action`/`get_actions`；`engine/monitor.py` 加 try/except 包裹的 `_record_action` 助手，在 Step1/2/3 的 API 调用成功后埋点，并在 Step1 补一条 `record_trade(side="sell")`；`web/routes.py` 加 `/api/actions`；`history.html`/`logs.html` 加只读展示区。no-op 状态不持久化，仍走内存快照。

**Tech Stack:** Python / SQLite / pytest / unittest.mock；Flask + Jinja2 + 原生 JS。

参考 spec：`docs/superpowers/specs/2026-05-19-action-log-sell-reasons-design.md`

---

### Task 1: `actions` 表 + `record_action` / `get_actions`

**Files:**
- Modify: `models/database.py`（`_create_tables` 内追加表；`record_trade`/`get_trade_history` 之后加两方法）
- Test: `tests/test_database.py`（追加 `TestActions`）

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_database.py` (file already has `import pytest`, the `db` fixture):

```python
class TestActions:
    def test_record_and_get_action(self, db):
        db.record_action(
            wallet="0xABC",
            market_id="mkt1",
            action_type="take_profit_sell",
            side="卖出",
            price=0.33,
            size=120.0,
            reason="买单成交，按成交价挂等价止盈卖单",
            price_basis="卖价=买入成交价 0.3300；来源：CLOB get_trades",
        )
        rows = db.get_actions()
        assert len(rows) == 1
        r = rows[0]
        assert r["wallet"] == "0xABC"
        assert r["action_type"] == "take_profit_sell"
        assert r["side"] == "卖出"
        assert r["price"] == 0.33
        assert r["size"] == 120.0
        assert "止盈" in r["reason"]
        assert "成交价" in r["price_basis"]
        assert r["created_at"] > 0

    def test_get_actions_filters_by_wallet(self, db):
        db.record_action("0xA", "m", "cancel_remainder", "-", -1, 0,
                         "r", "b")
        db.record_action("0xB", "m", "cancel_remainder", "-", -1, 0,
                         "r", "b")
        assert len(db.get_actions(wallet="0xA")) == 1
        assert db.get_actions(wallet="0xA")[0]["wallet"] == "0xA"

    def test_get_actions_filters_by_action_types(self, db):
        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.3, 1,
                         "r", "b")
        db.record_action("0xA", "m", "step3_replace_new", "买入", 0.4, 1,
                         "r", "b")
        db.record_action("0xA", "m", "stoploss_market_sell", "卖出", 0.2,
                         1, "r", "b")
        rows = db.get_actions(
            action_types=["take_profit_sell", "stoploss_market_sell"]
        )
        assert len(rows) == 2
        assert {r["action_type"] for r in rows} == {
            "take_profit_sell", "stoploss_market_sell"
        }

    def test_get_actions_filters_by_time_range(self, db):
        import time
        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.3, 1,
                         "r", "b")
        now = time.time()
        assert len(db.get_actions(start=now - 3600, end=now + 3600)) == 1
        assert len(db.get_actions(start=now + 3600)) == 0

    def test_get_actions_orders_desc_by_created_at(self, db):
        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.1, 1,
                         "first", "b")
        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.2, 1,
                         "second", "b")
        rows = db.get_actions()
        assert rows[0]["reason"] == "second"
        assert rows[1]["reason"] == "first"
```

- [ ] **Step 2: Run the tests to verify they FAIL**

Run: `python -m pytest tests/test_database.py::TestActions -v`
Expected: FAIL — `AttributeError: ... no attribute 'record_action'`.

- [ ] **Step 3: Add the `actions` table**

In `models/database.py`, inside `_create_tables`'s `c.executescript("""...""")`, add this block immediately AFTER the `CREATE TABLE IF NOT EXISTS trades (...)` statement (before `CREATE TABLE IF NOT EXISTS cooldowns`):

```sql
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL DEFAULT -1,
                size REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                price_basis TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
```

- [ ] **Step 4: Add `record_action` and `get_actions`**

In `models/database.py`, immediately AFTER the `get_trade_history` method (right before the `# --- Cooldowns ---` comment), add:

```python
    # --- Actions (monitor order-mutating actions log) ---

    def record_action(
        self,
        wallet: str,
        market_id: str,
        action_type: str,
        side: str,
        price: float,
        size: float,
        reason: str,
        price_basis: str,
    ):
        c = self.conn.cursor()
        c.execute(
            """INSERT INTO actions
            (wallet, market_id, action_type, side, price, size,
             reason, price_basis)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                wallet,
                market_id,
                action_type,
                side,
                price,
                size,
                reason,
                price_basis,
            ),
        )
        self.conn.commit()

    def get_actions(
        self,
        wallet: str = None,
        start: float = None,
        end: float = None,
        action_types: list = None,
    ) -> list[dict]:
        c = self.conn.cursor()
        query = "SELECT * FROM actions WHERE 1=1"
        params = []
        if wallet:
            query += " AND wallet = ?"
            params.append(wallet)
        if start:
            query += " AND created_at >= ?"
            params.append(start)
        if end:
            query += " AND created_at <= ?"
            params.append(end)
        if action_types:
            placeholders = ",".join("?" * len(action_types))
            query += f" AND action_type IN ({placeholders})"
            params.extend(action_types)
        query += " ORDER BY created_at DESC"
        c.execute(query, params)
        return [dict(row) for row in c.fetchall()]
```

- [ ] **Step 5: Run the tests to verify they PASS**

Run: `python -m pytest tests/test_database.py -v`
Expected: PASS — `TestActions` (5) plus all pre-existing `test_database` tests green.

- [ ] **Step 6: Commit**

```bash
git add models/database.py tests/test_database.py
git commit -m "feat: actions table + record_action/get_actions

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: monitor `_record_action` 助手 + Step1 补记卖单与埋点

**Files:**
- Modify: `engine/monitor.py`（加 `_record_action`；改 `_handle_fill`）
- Test: `tests/test_monitor.py`（改 `test_filled_order_triggers_sell`；加新用例）

- [ ] **Step 1: Update the existing fill test + write new failing tests**

In `tests/test_monitor.py`, REPLACE the body of `test_filled_order_triggers_sell` (the assertions after `monitor.check_buy_orders()`) so the four trailing assert lines:

```python
        api.place_limit_sell.assert_called_with("tok1", 0.25, 1000.0)
        db.record_trade.assert_called_once()
        db.set_cooldown.assert_called_with("0xABC", "mkt1", 20)
        api.cancel_orders.assert_called_with(["ord1"])
```

become:

```python
        api.place_limit_sell.assert_called_with("tok1", 0.25, 1000.0)
        assert db.record_trade.call_count == 2
        sides = [c.kwargs["side"] for c in db.record_trade.call_args_list]
        assert sides == ["buy", "sell"]
        for c in db.record_trade.call_args_list:
            assert c.kwargs["price"] == 0.25
            assert c.kwargs["size"] == 1000.0
        db.set_cooldown.assert_called_with("0xABC", "mkt1", 20)
        api.cancel_orders.assert_called_with(["ord1"])
        action_types = [
            c.kwargs["action_type"] for c in db.record_action.call_args_list
        ]
        assert "take_profit_sell" in action_types
        assert "cancel_remainder" in action_types
        tp = next(
            c for c in db.record_action.call_args_list
            if c.kwargs["action_type"] == "take_profit_sell"
        )
        assert tp.kwargs["side"] == "卖出"
        assert tp.kwargs["price"] == 0.25
        assert "成交价" in tp.kwargs["price_basis"]
        assert "止盈" in tp.kwargs["reason"]
```

Then append a new test class at end of `tests/test_monitor.py`:

```python
class TestStep1ActionLog:
    def test_cancel_remainder_not_recorded_when_no_order_id(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        api.place_limit_sell.return_value = {}
        with patch("engine.monitor.select_new_buy_fills") as mf:
            mf.return_value = [
                {
                    "trade_id": "t1",
                    "order_id": None,
                    "asset_id": "tok1",
                    "price": 0.25,
                    "size": 100.0,
                    "market": "mkt1",
                    "ts": 1.0,
                }
            ]
            monitor.check_buy_orders()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "take_profit_sell" in ats
        assert "cancel_remainder" not in ats

    def test_record_action_never_breaks_fill(self):
        monitor, api, db = _make_monitor()
        api.get_trades.return_value = []
        api.place_limit_sell.return_value = {}
        db.record_action.side_effect = RuntimeError("db down")
        with patch("engine.monitor.select_new_buy_fills") as mf:
            mf.return_value = [
                {
                    "trade_id": "t1",
                    "order_id": "o1",
                    "asset_id": "tok1",
                    "price": 0.25,
                    "size": 100.0,
                    "market": "mkt1",
                    "ts": 1.0,
                }
            ]
            monitor.check_buy_orders()  # must not raise
        api.place_limit_sell.assert_called_once()
```

- [ ] **Step 2: Run the tests to verify they FAIL**

Run: `python -m pytest tests/test_monitor.py::TestCheckBuyOrders::test_filled_order_triggers_sell tests/test_monitor.py::TestStep1ActionLog -v`
Expected: FAIL — `record_trade.call_count == 2` fails (currently 1); `record_action` attribute exists on MagicMock but is never called so `action_types` empty; `_record_action` not defined.

- [ ] **Step 3: Add the `_record_action` helper**

In `engine/monitor.py`, add this method immediately AFTER `publish_status` (right before `def init_watermark`):

```python
    def _record_action(
        self, market_id, action_type, side, price, size, reason, price_basis
    ) -> None:
        """Persist one order-mutating action. Never breaks Step1/2/3."""
        try:
            self.db.record_action(
                wallet=self.wallet_address,
                market_id=market_id,
                action_type=action_type,
                side=side,
                price=price,
                size=size,
                reason=reason,
                price_basis=price_basis,
            )
        except Exception as e:
            logger.warning("record_action failed: %s", e)
```

- [ ] **Step 4: Modify `_handle_fill` to record the sell + actions**

In `engine/monitor.py` `_handle_fill`, the current block is:

```python
        # Take-profit sell at the fill price (our resting maker buy filled here)
        self.api.place_limit_sell(asset_id, price, size)
        self.db.record_trade(
            wallet=self.wallet_address,
            market_id=market_id,
            market_name="",
            side="buy",
            price=price,
            size=size,
        )
        self.db.set_cooldown(
            self.wallet_address, market_id, self.db.get_settings()["cooldown_minutes"]
        )
        if order_id and order_id not in cancelled_orders:
            try:
                self.api.cancel_orders([order_id])
                cancelled_orders.add(order_id)
            except Exception as e:
                logger.warning("Cancel remainder of %s failed: %s", order_id, e)
```

Replace it with:

```python
        # Take-profit sell at the fill price (our resting maker buy filled here)
        self.api.place_limit_sell(asset_id, price, size)
        self.db.record_trade(
            wallet=self.wallet_address,
            market_id=market_id,
            market_name="",
            side="buy",
            price=price,
            size=size,
        )
        # Main history also records the take-profit sell so the direction
        # column is complete (buy / sell / stop_loss).
        self.db.record_trade(
            wallet=self.wallet_address,
            market_id=market_id,
            market_name="",
            side="sell",
            price=price,
            size=size,
            pnl=0.0,
        )
        self._record_action(
            market_id=market_id,
            action_type="take_profit_sell",
            side="卖出",
            price=price,
            size=size,
            reason="买单成交，按成交价挂等价止盈卖单（赚流动性奖励，原价卖出不亏本金）",
            price_basis=f"卖价=买入成交价 {price:.4f}；来源：CLOB get_trades 的 maker_orders 成交价",
        )
        self.db.set_cooldown(
            self.wallet_address, market_id, self.db.get_settings()["cooldown_minutes"]
        )
        if order_id and order_id not in cancelled_orders:
            try:
                self.api.cancel_orders([order_id])
                cancelled_orders.add(order_id)
                self._record_action(
                    market_id=market_id,
                    action_type="cancel_remainder",
                    side="-",
                    price=-1,
                    size=size,
                    reason="该买单已成交，撤销同一买单剩余未成交量，避免超买",
                    price_basis=f"撤 order_id={order_id}；撤单操作无价格",
                )
            except Exception as e:
                logger.warning("Cancel remainder of %s failed: %s", order_id, e)
```

- [ ] **Step 5: Run the tests to verify they PASS**

Run: `python -m pytest tests/test_monitor.py -v`
Expected: PASS — modified `test_filled_order_triggers_sell`, new `TestStep1ActionLog` (2), and all other `test_monitor` tests green (the snapshot/partial/exception tests don't assert `record_trade` count so they still pass).

- [ ] **Step 6: Commit**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat: Step1 records take-profit sell into trades + action log

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Step2 止损动作埋点

**Files:**
- Modify: `engine/monitor.py`（`_check_pos_sl`）
- Test: `tests/test_monitor.py`（追加 `TestStep2ActionLog`）

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_monitor.py`:

```python
class TestStep2ActionLog:
    def _pos(self):
        return [
            {
                "asset": "tok1",
                "size": 1000.0,
                "avgPrice": 0.30,
                "curPrice": 0.24,
                "conditionId": "mkt1",
            }
        ]

    def test_stop_loss_records_cancel_and_market_sell(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        api.get_user_positions.return_value = self._pos()
        api.get_open_orders.return_value = [
            {"id": "sell1", "asset_id": "tok1", "side": "SELL"},
        ]
        with patch("engine.monitor.stop_loss_triggered", return_value=True):
            monitor.check_stop_loss()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "stoploss_cancel_sell" in ats
        assert "stoploss_market_sell" in ats
        ms = next(
            c for c in db.record_action.call_args_list
            if c.kwargs["action_type"] == "stoploss_market_sell"
        )
        assert ms.kwargs["side"] == "卖出"
        assert ms.kwargs["price"] == 0.24
        assert "avgPrice=0.3000" in ms.kwargs["price_basis"]
        assert "Data API" in ms.kwargs["price_basis"]
        assert "止损阈值" in ms.kwargs["reason"]

    def test_no_cancel_action_when_no_sell_orders(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        api.get_user_positions.return_value = self._pos()
        api.get_open_orders.return_value = []
        with patch("engine.monitor.stop_loss_triggered", return_value=True):
            monitor.check_stop_loss()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "stoploss_cancel_sell" not in ats
        assert "stoploss_market_sell" in ats

    def test_no_action_when_not_triggered(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        api.get_user_positions.return_value = self._pos()
        api.get_open_orders.return_value = []
        with patch("engine.monitor.stop_loss_triggered", return_value=False):
            monitor.check_stop_loss()
        db.record_action.assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they FAIL**

Run: `python -m pytest tests/test_monitor.py::TestStep2ActionLog -v`
Expected: FAIL — `record_action` never called in stop-loss path; `action_types` empty.

- [ ] **Step 3: Modify `_check_pos_sl`**

In `engine/monitor.py` `_check_pos_sl`, the current block is:

```python
        sell_ids = [
            o["id"]
            for o in open_orders
            if o.get("asset_id") == asset_id and o.get("side") == "SELL"
        ]
        if sell_ids:
            try:
                self.api.cancel_orders(sell_ids)
            except Exception as e:
                logger.warning("Cancel sell orders for %s failed: %s", asset_id, e)
        self.api.place_market_sell(asset_id, size)
        self.db.record_trade(
            wallet=self.wallet_address,
            market_id=pos.get("conditionId", ""),
            market_name="",
            side="stop_loss",
            price=cur,
            size=size,
            pnl=(cur - avg) * size,
        )
        logger.warning(
            "Stop-loss executed: asset=%s size=%s cur=%.4f avg=%.4f",
            asset_id,
            size,
            cur,
            avg,
        )
```

Replace it with:

```python
        sell_ids = [
            o["id"]
            for o in open_orders
            if o.get("asset_id") == asset_id and o.get("side") == "SELL"
        ]
        cid = pos.get("conditionId", "")
        if sell_ids:
            try:
                self.api.cancel_orders(sell_ids)
                self._record_action(
                    market_id=cid,
                    action_type="stoploss_cancel_sell",
                    side="-",
                    price=-1,
                    size=size,
                    reason="触发止损，先撤该持仓全部止盈卖单以便市价平仓",
                    price_basis=f"撤 {len(sell_ids)} 笔 SELL；来源：CLOB get_open_orders（asset={asset_id} 的 SELL）",
                )
            except Exception as e:
                logger.warning("Cancel sell orders for %s failed: %s", asset_id, e)
        self.api.place_market_sell(asset_id, size)
        self.db.record_trade(
            wallet=self.wallet_address,
            market_id=cid,
            market_name="",
            side="stop_loss",
            price=cur,
            size=size,
            pnl=(cur - avg) * size,
        )
        self._record_action(
            market_id=cid,
            action_type="stoploss_market_sell",
            side="卖出",
            price=cur,
            size=size,
            reason=f"现价 {cur:.4f} 跌破成本价 {avg:.4f} 的止损阈值 avg×(1-止损比例{settings['stop_loss_pct']}%)，市价平仓止损",
            price_basis=f"成本价 avgPrice={avg:.4f}、现价 curPrice={cur:.4f}；来源：Polymarket Data API /positions",
        )
        logger.warning(
            "Stop-loss executed: asset=%s size=%s cur=%.4f avg=%.4f",
            asset_id,
            size,
            cur,
            avg,
        )
```

- [ ] **Step 4: Run the tests to verify they PASS**

Run: `python -m pytest tests/test_monitor.py -v`
Expected: PASS — `TestStep2ActionLog` (3) plus all pre-existing `TestStopLoss` tests green (`test_triggers_stop_loss_when_price_drops` still asserts `record_trade.assert_called_once()` — unchanged, only `record_action` is new).

- [ ] **Step 5: Commit**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat: Step2 stop-loss records cancel-sell + market-sell actions

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Step3 合规动作埋点 + no-op 不持久化

**Files:**
- Modify: `engine/monitor.py`（`_check_compliance` 尾部）
- Test: `tests/test_monitor.py`（追加 `TestStep3ActionLog`）

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_monitor.py`:

```python
class TestStep3ActionLog:
    def _ob(self):
        return {
            "bids": [{"price": "0.48", "size": "1000"}],
            "asks": [{"price": "0.52", "size": "1000"}],
            "tick_size": "0.01",
        }

    def _order(self, price="0.40"):
        return {
            "id": "o1",
            "side": "BUY",
            "asset_id": "tok1",
            "market": "cid1",
            "size_matched": "0",
            "price": price,
            "original_size": "500",
            "neg_risk": False,
        }

    def test_replace_records_cancel_old_and_replace_new(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        api.place_limit_buy.return_value = {"orderID": "o2"}
        with patch("engine.monitor.needs_replace", return_value="replace"), patch(
            "engine.monitor.determine_order_price", return_value=0.48
        ):
            monitor.check_sell_orders()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["step3_cancel_old", "step3_replace_new"]
        new = db.record_action.call_args_list[1]
        assert new.kwargs["side"] == "买入"
        assert new.kwargs["price"] == 0.48
        assert "determine_order_price" in new.kwargs["price_basis"]
        assert "奖励区间" in new.kwargs["reason"]

    def test_cancel_nocompliant_records_single_action(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        with patch("engine.monitor.needs_replace", return_value="cancel"), patch(
            "engine.monitor.determine_order_price", return_value=None
        ):
            monitor.check_sell_orders()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert ats == ["step3_cancel_nocompliant"]
        api.place_limit_buy.assert_not_called()

    def test_keep_records_no_action(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order(price="0.48")]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{"rewards_max_spread": 3}]
        with patch("engine.monitor.needs_replace", return_value="keep"), patch(
            "engine.monitor.determine_order_price", return_value=0.48
        ):
            monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_empty_orderbook_records_no_action(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = {
            "bids": [], "asks": [], "tick_size": "0.01"
        }
        monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_no_max_spread_records_no_action(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [self._order()]
        api.get_orderbook.return_value = self._ob()
        api.get_rewards_for_market.return_value = [{}]
        monitor.check_sell_orders()
        db.record_action.assert_not_called()

    def test_partial_fill_records_no_action(self):
        monitor, api, db = _make_monitor()
        api.get_open_orders.return_value = [
            {
                "id": "o1",
                "side": "BUY",
                "asset_id": "tok1",
                "market": "cid1",
                "size_matched": "100",
                "price": "0.48",
                "original_size": "500",
            }
        ]
        monitor.check_sell_orders()
        db.record_action.assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they FAIL**

Run: `python -m pytest tests/test_monitor.py::TestStep3ActionLog -v`
Expected: FAIL — replace/cancel paths don't call `record_action`; `ats` empty so `ats == [...]` fails.

- [ ] **Step 3: Modify `_check_compliance` tail**

In `engine/monitor.py` `_check_compliance`, the current tail block is:

```python
        if action == "keep":
            return
        try:
            self.api.cancel_orders([o["id"]])
        except Exception as e:
            logger.warning("Cancel %s failed: %s", o.get("id"), e)
            return
        if action == "replace":
            size = int(float(o.get("original_size", 0) or 0))
            neg_risk = bool(o.get("neg_risk", False))
            self.api.place_limit_buy(
                token_id, want, size, tick_size=tick_str, neg_risk=neg_risk
            )
            logger.info("Replaced buy %s -> %.4f", o.get("id"), want)
        else:
            logger.info("Cancelled non-compliant buy %s (no valid price)", o.get("id"))
```

Replace it with:

```python
        if action == "keep":
            return
        old_price = float(o.get("price", 0) or 0)
        osize = int(float(o.get("original_size", 0) or 0))
        cid = o.get("market", "")
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

(Note: `rmin`, `rmax`, `midpoint`, `max_spread`, `tick` are already in scope — computed earlier in `_check_compliance` before this block. The original `size` local is renamed `osize` and now also used by the cancel-record branch.)

- [ ] **Step 4: Run the tests to verify they PASS**

Run: `python -m pytest tests/test_monitor.py -v`
Expected: PASS — `TestStep3ActionLog` (6) plus all pre-existing `TestCheckSellOrders` / `TestMonitorStatusSnapshot` tests green (they don't assert `record_action`; `place_limit_buy` still called with the same args, just via `osize`).

- [ ] **Step 5: Commit**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat: Step3 records cancel-old/replace-new/cancel-nocompliant actions

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `/api/actions` 路由

**Files:**
- Modify: `web/routes.py`（`/api/history` 之后加 `/api/actions`）

- [ ] **Step 1: Add the route**

In `web/routes.py`, immediately AFTER the `api_get_history` function (the `/api/history` route ending with `return jsonify(trades)`), add:

```python
@app.route("/api/actions", methods=["GET"])
@login_required
def api_get_actions():
    wallet = request.args.get("wallet")
    start = request.args.get("start", type=float)
    end = request.args.get("end", type=float)
    types = request.args.get("types")
    action_types = types.split(",") if types else None
    return jsonify(db.get_actions(wallet, start, end, action_types))
```

- [ ] **Step 2: Verify import + route registered**

Run:
```bash
python -c "import web.routes; print(sorted(str(r) for r in web.routes.app.url_map.iter_rules() if 'actions' in str(r)))"
```
Expected: stdout contains `/api/actions` (exit 0, no traceback).

- [ ] **Step 3: Run full suite (no regression)**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add web/routes.py
git commit -m "feat: /api/actions endpoint (wallet/date/types filter)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 历史页"卖单理由"区

**Files:**
- Modify: `web/templates/history.html`

- [ ] **Step 1: Add the sell-reasons table markup**

In `web/templates/history.html`, immediately BEFORE the `{% endblock %}` that closes the `content` block (after the closing `</table>` of the existing history table), add:

```html
<h2 style="margin-top:2rem;">卖单理由</h2>
<table class="data-table">
    <thead>
        <tr>
            <th>时间</th><th>钱包</th><th>市场</th><th>动作</th>
            <th>价格</th><th>原因</th><th>价格依据/来源</th>
        </tr>
    </thead>
    <tbody id="sell-reasons-body"></tbody>
</table>
```

- [ ] **Step 2: Add the fetch + render JS**

In `web/templates/history.html`, inside the `{% block scripts %}`'s `<script>`, add this function definition immediately AFTER the closing `}` of `refreshHistory` and BEFORE the `fetch('/api/wallets')` block:

```javascript
const ACTION_LABELS = {
    take_profit_sell: '止盈挂卖单',
    stoploss_market_sell: '止损市价卖',
    cancel_remainder: '撤成交剩余',
    stoploss_cancel_sell: '止损撤止盈卖',
    step3_cancel_old: 'Step3撤旧单',
    step3_replace_new: 'Step3重挂买',
    step3_cancel_nocompliant: 'Step3撤单(无合规价)',
};

function refreshSellReasons() {
    const wallet = document.getElementById('wallet-filter').value;
    const startDate = document.getElementById('start-date').value;
    const endDate = document.getElementById('end-date').value;
    const params = new URLSearchParams();
    params.set('types', 'take_profit_sell,stoploss_market_sell');
    if (wallet) params.set('wallet', wallet);
    if (startDate) params.set('start', new Date(startDate).getTime() / 1000);
    if (endDate) params.set('end', new Date(endDate + 'T23:59:59').getTime() / 1000);

    fetch(`/api/actions?${params}`).then(r => r.json()).then(rows => {
        document.getElementById('sell-reasons-body').innerHTML = rows.map(a => `
            <tr>
                <td>${new Date(a.created_at * 1000).toLocaleString('zh-CN')}</td>
                <td title="${a.wallet}">${a.wallet.slice(0,6)}...${a.wallet.slice(-4)}</td>
                <td>${a.market_id}</td>
                <td>${ACTION_LABELS[a.action_type] || a.action_type}</td>
                <td>${a.price < 0 ? '-' : a.price.toFixed(4)}</td>
                <td>${a.reason}</td>
                <td>${a.price_basis}</td>
            </tr>
        `).join('');
    });
}
```

- [ ] **Step 3: Call it from `refreshHistory`**

In `web/templates/history.html`, the `refreshHistory` function currently ends with this `fetch` (the last statement before its closing `}`):

```javascript
    fetch(`/api/history?${params}`).then(r => r.json()).then(trades => {
        const sideLabels = {buy: '买入', sell: '卖出', stop_loss: '止损'};
        document.getElementById('history-body').innerHTML = trades.map(t => `
            <tr>
                <td>${new Date(t.created_at * 1000).toLocaleString('zh-CN')}</td>
                <td title="${t.wallet}">${t.wallet.slice(0,6)}...${t.wallet.slice(-4)}</td>
                <td>${t.market_name}</td>
                <td class="${t.side === 'buy' ? 'buy-side' : t.side === 'stop_loss' ? 'loss' : 'sell-side'}">${sideLabels[t.side] || t.side}</td>
                <td>${t.price.toFixed(4)}</td>
                <td>${t.size}</td>
                <td class="${t.pnl >= 0 ? 'profit' : 'loss'}">${t.pnl.toFixed(2)}</td>
            </tr>
        `).join('');
    });
```

Add one line immediately AFTER that `fetch(...)` statement (still inside `refreshHistory`, before its closing `}`):

```javascript
    refreshSellReasons();
```

- [ ] **Step 4: Verify template renders**

Run: `python -c "import web.routes"`
Expected: exit 0, no output.

Run: `python -m pytest -q`
Expected: all pass (same count as Task 5 — no backend change).

- [ ] **Step 5: Commit**

```bash
git add web/templates/history.html
git commit -m "feat: history page adds 卖单理由 section from /api/actions

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: 监控状态页 — 钱包下拉 + 操作记录区

**Files:**
- Modify: `web/templates/logs.html`

- [ ] **Step 1: Replace the filter bar + add the actions section markup**

In `web/templates/logs.html`, REPLACE the existing filter bar:

```html
<div class="filter-bar">
    <span id="updated">最后更新：-</span>
    <button class="btn btn-sm" onclick="refreshStatus()">刷新</button>
</div>
```

with:

```html
<div class="filter-bar">
    <label>钱包：</label>
    <select id="wallet-filter" onchange="refreshStatus(); refreshActions();">
        <option value="">全部</option>
    </select>
    <span id="updated">最后更新：-</span>
    <button class="btn btn-sm" onclick="refreshStatus()">刷新</button>
</div>
```

Then immediately BEFORE the `{% endblock %}` that closes the `content` block (after the existing status `</table>`), add:

```html
<h2 style="margin-top:2rem;">操作记录</h2>
<div class="filter-bar">
    <label>开始日期：</label>
    <input type="date" id="act-start" onchange="refreshActions()">
    <label>结束日期：</label>
    <input type="date" id="act-end" onchange="refreshActions()">
</div>
<table class="data-table">
    <thead>
        <tr>
            <th>时间</th><th>钱包</th><th>市场</th><th>动作</th>
            <th>方向</th><th>价格</th><th>数量</th>
            <th>原因</th><th>价格依据/来源</th>
        </tr>
    </thead>
    <tbody id="actions-body"></tbody>
</table>
```

- [ ] **Step 2: Filter the live snapshot by selected wallet**

In `web/templates/logs.html`, the `refreshStatus` function currently has:

```javascript
        const rows = data.rows || [];
        const body = document.getElementById('status-body');
        if (!rows.length) {
```

Replace those three lines with:

```javascript
        const sel = document.getElementById('wallet-filter').value;
        const rows = (data.rows || []).filter(r => !sel || r.wallet === sel);
        const body = document.getElementById('status-body');
        if (!rows.length) {
```

- [ ] **Step 3: Add wallet dropdown population + actions fetch/render**

In `web/templates/logs.html`, immediately AFTER the closing `}` of `refreshStatus` and BEFORE the `refreshStatus();` call line at the bottom of the script, add:

```javascript
const ACTION_LABELS = {
    take_profit_sell: '止盈挂卖单',
    cancel_remainder: '撤成交剩余',
    stoploss_cancel_sell: '止损撤止盈卖',
    stoploss_market_sell: '止损市价卖',
    step3_cancel_old: 'Step3撤旧单',
    step3_replace_new: 'Step3重挂买',
    step3_cancel_nocompliant: 'Step3撤单(无合规价)',
};

function refreshActions() {
    const wallet = document.getElementById('wallet-filter').value;
    const s = document.getElementById('act-start').value;
    const e = document.getElementById('act-end').value;
    const params = new URLSearchParams();
    if (wallet) params.set('wallet', wallet);
    if (s) params.set('start', new Date(s).getTime() / 1000);
    if (e) params.set('end', new Date(e + 'T23:59:59').getTime() / 1000);

    fetch(`/api/actions?${params}`).then(r => r.json()).then(rows => {
        const body = document.getElementById('actions-body');
        if (!rows.length) {
            body.innerHTML = '<tr><td colspan="9">暂无操作记录</td></tr>';
            return;
        }
        body.innerHTML = rows.map(a => `
            <tr>
                <td>${escapeHtml(new Date(a.created_at * 1000).toLocaleString('zh-CN'))}</td>
                <td title="${escapeHtml(a.wallet)}">${escapeHtml(shortWallet(a.wallet))}</td>
                <td>${escapeHtml(a.market_id)}</td>
                <td class="${actionClass(a.action_type)}">${escapeHtml(ACTION_LABELS[a.action_type] || a.action_type)}</td>
                <td>${escapeHtml(a.side)}</td>
                <td>${a.price < 0 ? '-' : escapeHtml(a.price.toFixed(4))}</td>
                <td>${escapeHtml(a.size)}</td>
                <td>${escapeHtml(a.reason)}</td>
                <td>${escapeHtml(a.price_basis)}</td>
            </tr>
        `).join('');
    });
}

fetch('/api/wallets').then(r => r.json()).then(wallets => {
    const select = document.getElementById('wallet-filter');
    wallets.forEach(w => {
        const opt = document.createElement('option');
        opt.value = w.address;
        opt.textContent = `${w.address.slice(0,6)}...${w.address.slice(-4)}`;
        select.appendChild(opt);
    });
});
```

- [ ] **Step 4: Drive `refreshActions` from initial load + the 4s tick**

In `web/templates/logs.html`, the bottom of the script currently is:

```javascript
refreshStatus();
setInterval(refreshStatus, 4000);
```

Replace it with:

```javascript
refreshStatus();
refreshActions();
setInterval(() => { refreshStatus(); refreshActions(); }, 4000);
```

- [ ] **Step 5: Verify template renders + suite**

Run: `python -c "import web.routes"`
Expected: exit 0, no output.

Run: `python -m pytest -q`
Expected: all pass (same count as Task 6).

- [ ] **Step 6: Commit**

```bash
git add web/templates/logs.html
git commit -m "feat: 监控状态 adds wallet filter + persisted actions log section

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: 全套验证 + 收尾

- [ ] **Step 1: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass. New tests added: `test_database` +5, `test_monitor` +11 (TestStep1ActionLog 2, TestStep2ActionLog 3, TestStep3ActionLog 6) plus the modified `test_filled_order_triggers_sell`.

- [ ] **Step 2: Confirm commit scope**

Run: `git log --oneline main..HEAD` and `git diff --stat main..HEAD`
Expected: commits touch only `models/database.py`, `engine/monitor.py`, `web/routes.py`, `web/templates/history.html`, `web/templates/logs.html`, `tests/test_database.py`, `tests/test_monitor.py`, and the spec/plan docs. NOT `docs/superpowers/specs/2026-05-19-test-place-orders-design.md` or any leftover untracked plan files.

- [ ] **Step 3: Finish the branch**

Use the `superpowers:finishing-a-development-branch` skill (verify tests → present options → execute choice).

---

## Self-Review

**Spec coverage:**
- 新建 `actions` 表（不动 trades 结构）→ Task 1 ✓
- `record_action` / `get_actions`（wallet/start/end/action_types 筛选、DESC）→ Task 1 ✓
- `_record_action` try/except 不打断 Step1/2/3 → Task 2 Step3 + `test_record_action_never_breaks_fill` ✓
- 主表补记止盈卖单（Step1 record_trade side="sell" pnl=0.0）→ Task 2 ✓；既有 `test_filled_order_triggers_sell` 改两次断言 ✓
- Step1 take_profit_sell + cancel_remainder（含 order_id 缺失不记）→ Task 2 ✓
- Step2 stoploss_cancel_sell（有 SELL 才记）+ stoploss_market_sell（含 avgPrice/curPrice/Data API 文案）→ Task 3 ✓
- Step3 step3_cancel_old + step3_replace_new / step3_cancel_nocompliant，撤成功后才记，replace 用 osize → Task 4 ✓
- no-op（keep/空盘口/取不到 ms/部分成交）不持久化 → Task 4 五个 `*_no_action` 用例 ✓
- `/api/actions`（wallet/start/end/types 拆分）→ Task 5 ✓
- 历史页保留主表 + 加"卖单理由"区（types=take_profit_sell,stoploss_market_sell；price<0 显示 -）→ Task 6 ✓
- 监控页上区加钱包下拉 + 客户端按 wallet 过滤快照；下区"操作记录"全 action_type、wallet/日期可查、随 4s 刷新 → Task 7 ✓
- 不改 fills/scanner/strategy/monitor_status 结构、不改 /api/history、止损主表仍 stop_loss → 计划未触及 ✓

**Placeholder scan:** 无 TBD/TODO；每步含完整代码与精确命令、预期输出。

**Type/name consistency:** `record_action(wallet, market_id, action_type, side, price, size, reason, price_basis)`（Task 1 定义）与 `_record_action(market_id, action_type, side, price, size, reason, price_basis)`（Task 2 定义，内部以 `wallet=self.wallet_address` 调 `db.record_action`）签名一致；Task 2/3/4 各埋点均用关键字参数与之匹配；`get_actions(wallet, start, end, action_types)`（Task 1）与 `/api/actions`（Task 5 `db.get_actions(wallet, start, end, action_types)`）位置参数一致；前端 `ACTION_LABELS` 键与 `action_type` 取值一致；`price<0 → '-'` 在 Task 6/7 渲染一致；Task 4 把原 `size` 局部改名 `osize` 并同步 `place_limit_buy(token_id, want, osize, ...)`。
