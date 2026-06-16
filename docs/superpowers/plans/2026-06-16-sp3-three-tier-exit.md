# SP3 三段式离场 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 v4 §7 把「止盈 + 百分比止损」两套合并成一个三段式主动限损离场:A 成本≤买一挂卖一/市价 · B 浮亏 FAK 扫到最低卖出价(成本−θ_loss) · B0 亏损≥θ_stop 市价清仓 · B_park 整仓挂卖一。

**Architecture:** 新纯函数 `plan_exit`(放 `engine/take_profit.py`,全单测)给出离场动作;`monitor` 合并 `check_take_profit`+`check_stop_loss` 为一个 `check_exit` 执行(复用 `_cost_lots` 成本重建 + 裸奔跳过 + `plan_take_profit` 对账);新 API `place_marketable_limit_sell`(FAK 限价)。退役 `stop_loss_pct`/`stop_loss_triggered`/`take_profit_price`。

**Tech Stack:** Python 3.12 / pytest(临时库、MagicMock 桩 API) / py-clob-client-v2(OrderType.FAK)。

**关键执行顺序(每提交保持绿):** 纯 `plan_exit`(T1,附加)→ API 原语(T2,附加)→ config 加 θ 键但**保留 stop_loss_pct**(T3,旧 monitor/routes 仍能读)→ monitor 换 `check_exit`(T4,改读 θ、删旧 TP/SL 方法与测试)→ 最后退役 stop_loss_pct/take_profit_price/risk(T5,此时已无人用)。

---

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `engine/take_profit.py` | 加 `plan_exit`;T5 删 `take_profit_price` | 修改 |
| `api/polymarket_api.py` | 加 `place_marketable_limit_sell`(FAK 限价) | 修改 |
| `config.py` | 加 θ_loss/θ_stop/case_a_mode;T5 删 stop_loss_pct | 修改 |
| `engine/monitor.py` | `check_take_profit`+`check_stop_loss` → `check_exit`;`_sell_book` 加 best_ask | 修改 |
| `engine/manager.py` | `_tick` 两行换 `check_exit` | 修改 |
| `engine/risk.py` | `stop_loss_triggered` 退役 | T5 删除 |
| `web/routes.py` | `api_get_positions` 止损价显示改 θ_stop | T5 修改 |
| `tests/test_exit_plan.py` | plan_exit 纯函数单测 | **新建** |
| `tests/test_monitor.py` | 删 TP/SL 测试类,加 check_exit 测试 | 修改 |
| `tests/test_risk.py` / `tests/test_take_profit.py` | 删 stop_loss_triggered / take_profit_price 用例 | T5 修改/删 |

---

## Task 1: plan_exit 纯函数（核心 IP）

**Files:** Modify `engine/take_profit.py`(追加 `plan_exit`)。Create `tests/test_exit_plan.py`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_exit_plan.py`:

```python
"""tests/test_exit_plan.py — 三段式离场决策纯函数(不触网)。"""

from engine.take_profit import plan_exit


def _p(cost, best_bid, best_ask, tier, action, price=None, theta_loss=0.02,
       theta_stop=0.05, mode="ask", size=100, tick=0.01):
    out = plan_exit(cost, best_bid, best_ask, tick, theta_loss, theta_stop, mode, size)
    assert out["tier"] == tier and out["action"] == action
    if price is not None:
        assert abs(out["price"] - price) < 1e-9
    return out


def test_case_a_rest_at_ask():
    # 成本 0.30 <= 买一 0.31 -> A 挂卖一(best_ask 0.33)
    _p(0.30, 0.31, 0.33, "A", "rest", price=0.33)


def test_case_a_market_mode():
    out = plan_exit(0.30, 0.31, 0.33, 0.01, 0.02, 0.05, "market", 100)
    assert out["tier"] == "A" and out["action"] == "market"


def test_case_a_boundary_cost_equals_bid():
    # 成本恰等买一 -> 仍属 A(无损)
    _p(0.30, 0.30, 0.32, "A", "rest", price=0.32)


def test_b0_force_clear_when_loss_ge_theta_stop():
    # 成本 0.40 买一 0.35 -> 亏损 0.05 >= θ_stop 0.05 -> B0 市价
    _p(0.40, 0.35, 0.36, "B0", "market", theta_stop=0.05)


def test_b_sweep_when_bid_ge_floor():
    # 成本 0.40 买一 0.39 亏损 0.01 < θ_stop;最低卖出价=0.40-0.02=0.38;买一 0.39>=0.38
    # -> B_sweep @ ceil_to_tick(0.38)=0.38
    _p(0.40, 0.39, 0.41, "B_sweep", "sweep", price=0.38)


def test_b_park_when_bid_below_floor():
    # 成本 0.40 买一 0.37 亏损 0.03 < θ_stop;最低卖出价 0.38;买一 0.37<0.38
    # -> B_park 挂卖一 best_ask 0.41
    _p(0.40, 0.37, 0.41, "B_park", "rest", price=0.41)


def test_no_bid_parks_at_ask():
    out = plan_exit(0.40, None, 0.41, 0.01, 0.02, 0.05, "ask", 100)
    assert out["action"] == "rest" and abs(out["price"] - 0.41) < 1e-9


def test_no_book_at_all_noop():
    out = plan_exit(0.40, None, None, 0.01, 0.02, 0.05, "ask", 100)
    assert out["action"] == "noop"


def test_rest_falls_back_to_cost_when_no_ask():
    # 无 best_ask 时 rest 价回退 ceil_to_tick(cost)
    out = plan_exit(0.30, 0.31, None, 0.01, 0.02, 0.05, "ask", 100)
    assert out["action"] == "rest" and abs(out["price"] - 0.30) < 1e-9
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_exit_plan.py -v`
Expected: FAIL（`cannot import name 'plan_exit'`）

- [ ] **Step 3: 实现**

在 `engine/take_profit.py` 末尾追加（`ceil_to_tick` 已在本模块定义）:

```python
def plan_exit(cost, best_bid, best_ask, tick, theta_loss, theta_stop, case_a_mode, size):
    """三段式离场决策(v4 §7)。theta_loss/theta_stop 为价位单位(=¢/100)。

    返回 {"tier","action","price","size"};action ∈ {"rest","market","sweep","noop"}。
    cost>0、size>0 由调用方在调用前保证(成本取不到 -> 裸奔跳过,不进此函数)。
    - rest 价 = 卖一(best_ask),无则回退 ceil_to_tick(cost)。
    - sweep 价 = ceil_to_tick(最低卖出价)(限价卖向上取整,绝不卖穿到下限以下)。
    """
    def _rest_price():
        if best_ask is not None and best_ask > 0:
            return best_ask
        return ceil_to_tick(cost, tick)

    if best_bid is None:
        if best_ask is not None and best_ask > 0:
            return {"tier": "B_park", "action": "rest", "price": _rest_price(), "size": size}
        return {"tier": "none", "action": "noop", "price": None, "size": 0.0}

    if cost <= best_bid:
        if case_a_mode == "market":
            return {"tier": "A", "action": "market", "price": None, "size": size}
        return {"tier": "A", "action": "rest", "price": _rest_price(), "size": size}

    loss = cost - best_bid
    if loss >= theta_stop:
        return {"tier": "B0", "action": "market", "price": None, "size": size}
    floor = cost - theta_loss
    if best_bid >= floor:
        return {"tier": "B_sweep", "action": "sweep",
                "price": ceil_to_tick(floor, tick), "size": size}
    return {"tier": "B_park", "action": "rest", "price": _rest_price(), "size": size}
```

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_exit_plan.py -v`
Expected: PASS（9 个）

- [ ] **Step 5: Commit（不 stage .claude/settings.local.json）**

```bash
git add engine/take_profit.py tests/test_exit_plan.py
git commit -m "feat(exit): plan_exit 三段式离场决策纯函数(A/B0/B_sweep/B_park)"
```

---

## Task 2: place_marketable_limit_sell（FAK 限价卖原语）

**Files:** Modify `api/polymarket_api.py`（紧跟 `place_market_sell` 之后）。Create/append `tests/test_marketable_limit_sell.py`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_marketable_limit_sell.py`:

```python
"""tests/test_marketable_limit_sell.py — FAK 限价卖原语(mock client,不触网)。"""

from unittest.mock import MagicMock
from py_clob_client_v2.clob_types import OrderType
from api.polymarket_api import PolymarketAPI


def test_marketable_limit_sell_uses_fak_order_type():
    api = object.__new__(PolymarketAPI)  # 跳过重构造(funder 派生等)
    api.client = MagicMock()
    api.client.create_and_post_order.return_value = {"orderID": "x", "status": "matched"}
    api.place_marketable_limit_sell("tok1", 0.09, 100, tick_size="0.01")
    args, kwargs = api.client.create_and_post_order.call_args
    # 第三个位置参数是 OrderType
    assert args[2] == OrderType.FAK
    order_args = args[0]
    assert getattr(order_args, "side", None) == "SELL"
    assert abs(float(getattr(order_args, "price")) - 0.09) < 1e-9
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_marketable_limit_sell.py -v`
Expected: FAIL（`place_marketable_limit_sell` 不存在）

- [ ] **Step 3: 实现**

在 `api/polymarket_api.py` 的 `place_market_sell` 方法之后（`# --- Order Management ---` 之前）追加:

```python
    def place_marketable_limit_sell(
        self,
        token_id: str,
        price: float,
        size: int,
        tick_size: str = "0.01",
        neg_risk: bool | None = None,
    ) -> dict:
        """限价卖但 FAK(fill-and-kill):吃掉所有 >= price 的买单后 kill 剩余,永不挂簿。

        用于 v4 §7 B 段逐级扫单——以「最低卖出价」为限价的市价单,绝不成交在该价以下
        (盘口瞬时假跌也卖不穿,自带防错杀)。neg_risk=None 走自动解析(与其它卖单一致,
        防负风险仓卖不出)。
        """
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=float(size),
            side="SELL",
        )
        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        res = self.client.create_and_post_order(order_args, options, OrderType.FAK)
        return _check_order_resp(res, "限价扫单(FAK)")
```

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_marketable_limit_sell.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/polymarket_api.py tests/test_marketable_limit_sell.py
git commit -m "feat(api): place_marketable_limit_sell FAK 限价扫单原语"
```

---

## Task 3: config θ 模板键（保留 stop_loss_pct）

**Files:** Modify `config.py`（`TEMPLATE_DEFAULTS`）。Test: `tests/test_database.py`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_database.py` 加:

```python
def test_template_defaults_has_exit_keys():
    from config import TEMPLATE_DEFAULTS
    assert TEMPLATE_DEFAULTS["theta_loss_cents"] == 2
    assert TEMPLATE_DEFAULTS["theta_stop_cents"] == 5
    assert TEMPLATE_DEFAULTS["case_a_mode"] == "ask"
    # 本任务暂保留 stop_loss_pct(下个任务退役),旧 monitor/routes 仍读
    assert "stop_loss_pct" in TEMPLATE_DEFAULTS
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_database.py::test_template_defaults_has_exit_keys -v`
Expected: FAIL（θ 键不存在）

- [ ] **Step 3: 改 config.py**

在 `config.py` 的 `TEMPLATE_DEFAULTS` 里、`stop_loss_pct` 行之后追加三行:

```python
    "stop_loss_pct": 15.0,
    "theta_loss_cents": 2,
    "theta_stop_cents": 5,
    "case_a_mode": "ask",
```

（保留 `stop_loss_pct` 不动；其余键不变。）

- [ ] **Step 4: 运行确认 PASS + 无回归**

Run: `python -m pytest tests/test_database.py -q`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_database.py
git commit -m "feat(config): 离场模板键 theta_loss/theta_stop/case_a_mode(暂留 stop_loss_pct)"
```

---

## Task 4: monitor check_exit（合并 TP+SL）

**Files:** Modify `engine/monitor.py`、`engine/manager.py`、`tests/test_monitor.py`。

**说明:** 把 `check_take_profit`/`_reconcile_take_profit`/`check_stop_loss`/`_check_pos_sl` 四个方法删掉,换成一个 `check_exit` + `_exit_position`;`_sell_book` 加返回 best_ask;`_tick`(manager)两行换一行;删 monitor 顶部 `take_profit_price`/`stop_loss_triggered` 的 import。删 test_monitor 的 TP/SL 测试类,加 `TestCheckExit`。`stop_loss_pct` 仍在 config(T5 才删),但 check_exit 不再读它。

- [ ] **Step 1: 删旧测试类 + 写新测试**

在 `tests/test_monitor.py`:

(a) 删除整类 `TestCheckTakeProfit`、`TestStopLoss`、`TestStep2ActionLog`（都测已删方法）。

(b) 把 `TestMonitorReadsTemplate`（测 `check_stop_loss` 调 `get_template_for`）整类替换为:

```python
class TestMonitorReadsTemplate:
    def test_check_exit_reads_template(self):
        from engine.monitor import OrderMonitor
        from unittest.mock import MagicMock
        db = MagicMock()
        db.get_template_for.return_value = {
            "theta_loss_cents": 2, "theta_stop_cents": 5, "case_a_mode": "ask",
        }
        db.get_settings.return_value = {"cooldown_minutes": 20, "rewards_cache_ttl_sec": 600}
        api = MagicMock()
        api.get_user_positions.return_value = []
        mon = OrderMonitor(api, db, "0xW")
        mon.check_exit()
        db.get_template_for.assert_called_with("0xW")
```

(c) 追加新类 `TestCheckExit`（用 monitor._cost_lots 覆盖来控制成本，api 桩持仓/挂单/订单簿）:

```python
class TestCheckExit:
    def _setup(self, cost, size, bids, asks, sells=None, mode="ask",
               theta_loss=2, theta_stop=5):
        monitor, api, db = _make_monitor(
            settings={"theta_loss_cents": theta_loss, "theta_stop_cents": theta_stop,
                      "case_a_mode": mode}
        )
        api.get_user_positions.return_value = [
            {"asset": "A-y", "size": size, "curPrice": (bids[0][0] if bids else 0),
             "conditionId": "A"}
        ]
        api.get_open_orders.return_value = sells or []
        api.get_orderbook.return_value = {
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
            "tick_size": "0.01",
        }
        monitor._cost_lots = lambda a, s, c: (cost, [
            {"price": cost, "take": s, "ts": 0, "trade_id": "t"}
        ])
        return monitor, api, db

    def test_case_a_rests_one_sell_at_ask(self):
        monitor, api, db = self._setup(0.30, 100, [(0.31, 500)], [(0.33, 500)])
        monitor.check_exit()
        api.place_limit_sell.assert_called_once()
        a, k = api.place_limit_sell.call_args
        assert a[0] == "A-y" and abs(a[1] - 0.33) < 1e-9 and a[2] == 100
        api.place_market_sell.assert_not_called()

    def test_case_a_market_mode_clears(self):
        monitor, api, db = self._setup(0.30, 100, [(0.31, 500)], [(0.33, 500)], mode="market")
        monitor.check_exit()
        api.place_market_sell.assert_called_once_with("A-y", 100)
        api.place_limit_sell.assert_not_called()

    def test_b0_market_clear_and_record(self):
        # 成本 0.40 买一 0.35 亏损 0.05 >= θ_stop 0.05 -> B0
        monitor, api, db = self._setup(0.40, 100, [(0.35, 500)], [(0.36, 500)])
        monitor.check_exit()
        api.place_market_sell.assert_called_once_with("A-y", 100)
        db.record_trade.assert_called_once()

    def test_b_sweep_marketable_limit_at_floor(self):
        # 成本 0.40 买一 0.39 亏损 0.01;最低卖出价 0.38;买一>=0.38 -> sweep @ 0.38
        monitor, api, db = self._setup(0.40, 100, [(0.39, 500)], [(0.41, 500)])
        monitor.check_exit()
        api.place_marketable_limit_sell.assert_called_once()
        a, k = api.place_marketable_limit_sell.call_args
        assert a[0] == "A-y" and abs(a[1] - 0.38) < 1e-9 and a[2] == 100

    def test_b_park_rests_at_ask(self):
        # 成本 0.40 买一 0.37 亏损 0.03;最低卖出价 0.38;买一<0.38 -> 挂卖一 0.41
        monitor, api, db = self._setup(0.40, 100, [(0.37, 500)], [(0.41, 500)])
        monitor.check_exit()
        api.place_limit_sell.assert_called_once()
        a, k = api.place_limit_sell.call_args
        assert abs(a[1] - 0.41) < 1e-9
        api.place_market_sell.assert_not_called()

    def test_naked_skips_no_sell(self):
        monitor, api, db = self._setup(0.30, 100, [(0.31, 500)], [(0.33, 500)])
        monitor._cost_lots = lambda a, s, c: (None, [])  # 成本取不到
        monitor.check_exit()
        api.place_limit_sell.assert_not_called()
        api.place_market_sell.assert_not_called()
        api.place_marketable_limit_sell.assert_not_called()

    def test_rest_keeps_existing_matching_sell(self):
        # 已有一笔卖单恰在 want 价(0.33)、量(100) -> 不重挂
        sells = [{"id": "s1", "asset_id": "A-y", "side": "SELL", "price": "0.33",
                  "original_size": "100", "size_matched": "0"}]
        monitor, api, db = self._setup(0.30, 100, [(0.31, 500)], [(0.33, 500)], sells=sells)
        monitor.check_exit()
        api.place_limit_sell.assert_not_called()
        api.cancel_orders.assert_not_called()
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_monitor.py::TestCheckExit -v`
Expected: FAIL（`check_exit` 不存在）

- [ ] **Step 3: 改 monitor.py**

(a) 顶部 import：把

```python
from engine.take_profit import (
    plan_take_profit,
    position_cost_with_lots,
    describe_cost_basis,
    take_profit_price,
)
from engine.eligibility import recheck_resting_buy
from engine.risk import stop_loss_triggered
```
改为（去掉 `take_profit_price`、`stop_loss_triggered`，加 `plan_exit`）：
```python
from engine.take_profit import (
    plan_take_profit,
    position_cost_with_lots,
    describe_cost_basis,
    plan_exit,
)
from engine.eligibility import recheck_resting_buy
```

(b) `_sell_book`：把返回从 `(float(tick_str), tick_str, best_bid)` 改为同时返回 best_ask。替换整个方法体的 return 区:

```python
    def _sell_book(self, asset_id: str):
        """(tick_float, tick_str, best_bid, best_ask);失败/空时缺的位回 None。"""
        try:
            ob = self.api.get_orderbook(asset_id)
            tick_str = ob.get("tick_size", "0.01")
            bids = sorted(ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
            asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            return float(tick_str), tick_str, best_bid, best_ask
        except Exception as e:
            logger.warning("orderbook for %s failed (exit tick=0.01): %s", asset_id, e)
            return 0.01, "0.01", None, None
```

(c) 删除四个方法：`check_take_profit`、`_reconcile_take_profit`、`check_stop_loss`、`_check_pos_sl`。在它们的位置（`_sell_book` 之后、`check_sell_orders` 之前合适处）新增:

```python
    def check_exit(self):
        """三段式离场(v4 §7):合并原止盈+止损。每持仓按 plan_exit 决策执行。"""
        tmpl = self.db.get_template_for(self.wallet_address)
        theta_loss = float(tmpl.get("theta_loss_cents", 2)) / 100.0
        theta_stop = float(tmpl.get("theta_stop_cents", 5)) / 100.0
        case_a_mode = tmpl.get("case_a_mode", "ask")
        try:
            positions = self.api.get_user_positions(self._funder())
        except Exception as e:
            logger.warning("Data API positions failed (skip exit): %s", e)
            return
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed (skip exit): %s", e)
            return
        for pos in positions:
            try:
                self._exit_position(pos, open_orders, theta_loss, theta_stop, case_a_mode)
            except Exception as e:
                logger.error("Exit error on %s: %s", pos.get("asset"), e)

    def _exit_position(self, pos, open_orders, theta_loss, theta_stop, case_a_mode):
        asset_id = pos.get("asset", "")
        size = float(pos.get("size", 0) or 0)
        cur = float(pos.get("curPrice", 0) or 0)
        cid = pos.get("conditionId", "")
        if size <= 0:
            return
        cost, lots = self._cost_lots(asset_id, size, cid)
        if cost is None or cost <= 0:
            logger.warning("Exit skipped (no buy fills) asset=%s size=%s — UNPROTECTED",
                           asset_id, size)
            self._status_add(market=cid, side="卖出", price="-", size=str(size), matched="-",
                             stage="离场", action="⚠️跳过·裸奔",
                             detail="get_trades 无买入成交、无法算成本，未离场，该持仓未受保护")
            return
        tick, tick_str, best_bid, best_ask = self._sell_book(asset_id)
        plan = plan_exit(cost, best_bid, best_ask, tick, theta_loss, theta_stop,
                         case_a_mode, size)
        sells = [o for o in open_orders
                 if o.get("asset_id") == asset_id and o.get("side") == "SELL"]
        action = plan["action"]
        basis = describe_cost_basis(cost, lots)

        if action == "noop":
            self._status_add(market=cid, side="卖出", price="-", size=str(size), matched="-",
                             stage="离场", action="跳过(盘口空)", detail="无买盘无卖盘，本轮跳过")
            return

        if action == "rest":
            want = plan["price"]
            p = plan_take_profit(size, want, tick, sells)
            if p["action"] in ("noop", "keep"):
                self._status_add(market=cid, side="卖出", price=f"{want:.4f}", size=str(size),
                                 matched="-", stage="离场",
                                 action=f"保持({plan['tier']})",
                                 detail=f"成本{cost:.4f} 挂卖一{want:.4f}")
                return
            if p["cancel_ids"]:
                try:
                    self.api.cancel_orders(p["cancel_ids"])
                    self._record_action(market_id=cid, action_type="exit_recancel", side="-",
                                        price=-1, size=size,
                                        reason="撤与持仓不符的旧卖单，改按持仓挂一笔",
                                        price_basis=f"撤 {len(p['cancel_ids'])} 笔 SELL")
                except Exception as e:
                    logger.warning("Cancel stale sells %s failed: %s", asset_id, e)
                    return
            self.api.place_limit_sell(asset_id, want, size, tick_size=tick_str)
            self._record_action(market_id=cid, action_type="exit_rest", side="卖出",
                                price=want, size=size,
                                reason=f"{plan['tier']}：挂卖一离场",
                                price_basis=f"{basis}；卖一{want:.4f}；来源：CLOB get_trades+get_orderbook")
            self._status_add(market=cid, side="卖出", price=f"{want:.4f}", size=str(size),
                             matched="-", stage="离场", action=f"挂卖一({plan['tier']})",
                             detail=f"成本{cost:.4f}")
            return

        # market / sweep：先撤该 asset 全部挂卖单
        sell_ids = [o["id"] for o in sells]
        if sell_ids:
            try:
                self.api.cancel_orders(sell_ids)
                self._record_action(market_id=cid, action_type="exit_cancel_sell", side="-",
                                    price=-1, size=size,
                                    reason=f"{plan['tier']}：先撤全部止盈卖单以便清仓",
                                    price_basis=f"撤 {len(sell_ids)} 笔 SELL")
            except Exception as e:
                logger.warning("Cancel sells %s failed: %s", asset_id, e)

        if action == "market":
            self.api.place_market_sell(asset_id, size)
            if plan["tier"] == "B0":
                self.db.record_trade(wallet=self.wallet_address, market_id=cid,
                                     market_name="", side="stop_loss", price=cur,
                                     size=size, pnl=(cur - cost) * size)
            self._record_action(market_id=cid, action_type="exit_market", side="卖出",
                                price=cur, size=size,
                                reason=f"{plan['tier']}：市价清仓离场",
                                price_basis=f"{basis}；现价{cur:.4f}；来源：CLOB get_trades+Data API")
            self._status_add(market=cid, side="卖出", price=f"{cur:.4f}", size=str(size),
                             matched="-", stage="离场", action=f"市价清仓({plan['tier']})",
                             detail=f"成本{cost:.4f}")
            return

        if action == "sweep":
            want = plan["price"]
            self.api.place_marketable_limit_sell(asset_id, want, size, tick_size=tick_str)
            self._record_action(market_id=cid, action_type="exit_sweep", side="卖出",
                                price=want, size=size,
                                reason="B：以最低卖出价 FAK 扫单(剩余下轮再评估)",
                                price_basis=f"{basis}；最低卖出价{want:.4f}；来源：CLOB get_trades+get_orderbook")
            self._status_add(market=cid, side="卖出", price=f"{want:.4f}", size=str(size),
                             matched="-", stage="离场", action="FAK扫单(B)",
                             detail=f"成本{cost:.4f} 扫到{want:.4f}")
            return
```

(d) `engine/manager.py` `_tick`：把

```python
        self.monitor.check_buy_orders()
        self.monitor.check_take_profit()
        self.monitor.check_stop_loss()
        self.monitor.check_sell_orders()
```
改为:
```python
        self.monitor.check_buy_orders()
        self.monitor.check_exit()
        self.monitor.check_sell_orders()
```
并把该方法 docstring 里提到 `check_take_profit` 的描述顺手改成 `check_exit`（若有）。

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_monitor.py::TestCheckExit tests/test_monitor.py::TestMonitorReadsTemplate -v`
Expected: PASS

- [ ] **Step 5: 全套测试**

Run: `python -m pytest -q`
Expected: ALL PASS。`stop_loss_pct` 仍在 config（routes 仍读，绿）；`risk.py`/`take_profit_price` 定义仍在（其自身测试 test_risk.py/test_take_profit.py 仍绿，T5 才删）。若 `tests/test_monitor_status.py` 等引用了已删的 TP/SL 方法名，按需改为 check_exit。

- [ ] **Step 6: Commit**

```bash
git add engine/monitor.py engine/manager.py tests/test_monitor.py
git commit -m "feat(monitor): check_exit 三段式离场(合并 check_take_profit+check_stop_loss)"
```

---

## Task 5: 退役 stop_loss_pct / stop_loss_triggered / take_profit_price

**Files:** Modify `config.py`、`web/routes.py`、`engine/take_profit.py`；删 `engine/risk.py`、`tests/test_risk.py`；改 `tests/test_take_profit.py`、`tests/test_database.py`。

- [ ] **Step 1: 确认无生产引用**

Run:
```
grep -rn "stop_loss_pct\|stop_loss_triggered\|take_profit_price\|engine.risk" engine/ web/ api/ --include=*.py
```
Expected: 仅 `engine/risk.py`(定义)、`engine/take_profit.py`(take_profit_price 定义)、`web/routes.py`(api_get_positions 读 stop_loss_pct)。若 monitor 仍有引用，停（T4 漏了）。

- [ ] **Step 2: 改 routes + config + 删函数 + 改测试**

(a) `web/routes.py` `api_get_positions`：把读 `stop_loss_pct` 的那段改用 θ_stop。原:
```python
        tmpl = db.get_template_for(addr)["stop_loss_pct"] / 100.0
        ...
        "stop_price": avg * (1 - sl),
```
（实际行见文件——T4 后 routes 仍是 SP1 形态：`sl = db.get_template_for(addr)["stop_loss_pct"] / 100.0`，`stop_price = avg*(1-sl)`。）改为按 θ_stop 美分给近似止损价:
```python
        theta_stop = float(db.get_template_for(addr).get("theta_stop_cents", 5)) / 100.0
        ...
        "stop_price": max(0.0, avg - theta_stop),
```
（把循环内 `sl = ...` 改成 `theta_stop = ...`，`stop_price` 行改成 `avg - theta_stop`。）

(b) `config.py`：从 `TEMPLATE_DEFAULTS` 删 `"stop_loss_pct": 15.0,` 一行。

(c) `engine/take_profit.py`：删 `take_profit_price` 函数（保留 `plan_take_profit`/`ceil_to_tick`/`position_cost_with_lots`/`describe_cost_basis`/`plan_exit`）。

(d) 删文件 `engine/risk.py`（`git rm engine/risk.py`）与 `tests/test_risk.py`（`git rm tests/test_risk.py`）。

(e) `tests/test_take_profit.py`：删其中 `take_profit_price` 的用例（保留其余）。`grep -n "take_profit_price" tests/test_take_profit.py` 找到后删除对应 test 函数。

(f) `tests/test_database.py`：SP1 迁移测试 `test_strategy_keys_move_to_default_template` 若断言 `stop_loss_pct` 迁移，删该断言（stop_loss_pct 不再在 TEMPLATE_DEFAULTS → `_migrate` 不再搬它）。把 Task 3 的 `test_template_defaults_has_exit_keys` 里 `assert "stop_loss_pct" in TEMPLATE_DEFAULTS` 改成 `assert "stop_loss_pct" not in TEMPLATE_DEFAULTS`。

- [ ] **Step 3: 确认无残留**

Run:
```
grep -rn "stop_loss_pct\|stop_loss_triggered\|take_profit_price\|engine.risk" . --include=*.py
```
Expected: 无（手动脚本若有注释除外）。

- [ ] **Step 4: 全套测试 + 冒烟**

Run: `python -m pytest -q`
Expected: ALL PASS。
Run: `python -c "import app; print('import ok')"`
Expected: `import ok`

- [ ] **Step 5: Commit**

```bash
git add -A
git status --short   # 确认未含 .claude/settings.local.json;若有:git restore --staged .claude/settings.local.json
git commit -m "refactor: 退役 stop_loss_pct/stop_loss_triggered/take_profit_price(被三段式离场取代)"
```

---

## 验收 checkpoint（对应 spec §七）

1. 成本≤买一：`TestCheckExit::test_case_a_rests_one_sell_at_ask`（挂卖一）+ `test_case_a_market_mode_clears`（市价）。
2. 浮亏、best_bid≥最低卖出价：`test_b_sweep_marketable_limit_at_floor`（FAK 扫到下限）。
3. 浮亏、亏损≥θ_stop：`test_b0_market_clear_and_record`（市价清仓 + record_trade）。
4. 浮亏、best_bid<下限：`test_b_park_rests_at_ask`（整仓挂卖一）。
5. 裸奔：`test_naked_skips_no_sell`（跳过、不下卖单）。
6. `pytest -q` 全绿；`stop_loss_pct`/`stop_loss_triggered`/`take_profit_price` 无残留（T5 Step 3 grep）。

## 范围之外（留后续）

SP4 单份奖励阈值+取档 · SP5 三档节奏+观察名单+成交后单侧暂停+撤改收敛 · SP6 模板 UI（θ/case_a_mode 编辑、退役死字段收口）。
