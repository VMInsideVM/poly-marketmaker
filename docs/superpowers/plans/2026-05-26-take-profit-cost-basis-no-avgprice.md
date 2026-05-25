# 止盈/止损成本严格 get_trades 逐笔重建、弃用 avgPrice 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 止盈/止损的成本只来自 CLOB `get_trades` 真实买入成交的逐笔加权；彻底移除 Data API `avgPrice` 兜底；取不到成交则跳过并醒目告警；把成本逐笔构成（时间+价格×份额+trade_id）写进卖单理由。

**Architecture:** 纯函数 + 实时 API、无 DB、无新状态。`engine/fills.py` 的 fill 多带 `trade_id`；`engine/take_profit.py` 新增 `cost_basis_with_lots`（成本+消耗的逐笔明细）与 `describe_cost_basis`（中文构成片段）；`engine/monitor.py` 用新方法 `_cost_lots` 取 `(成本, lots)`，删掉 `_cost`/`_cost_with_source` 的 avgPrice 兜底，取不到成交时跳过并写 ⚠️ 状态行。

**Tech Stack:** Python 3、pytest、`unittest.mock`、`py-clob-client-v2`（仅 monitor 间接用，纯函数与测试不碰网络）。

参考 spec：`docs/superpowers/specs/2026-05-26-take-profit-cost-basis-no-avgprice-design.md`

---

### Task 1: `extract_buy_fills` 每条 fill 增带 `trade_id`

**Files:**
- Modify: `engine/fills.py`（`extract_buy_fills`，约 48-94 行）
- Test: `tests/test_fills.py`

- [ ] **Step 1: 更新既有精确相等断言 + 新增一条 trade_id 断言（先红）**

`tests/test_fills.py` 把下列对 `extract_buy_fills` 的精确字典断言改为带 `trade_id`（`select_new_buy_fills` 的断言不动）：

```python
def test_extract_buy_fills_only_our_buys_on_asset():
    fills = extract_buy_fills([TRADE_TWO_OURS_BUY], FUNDER, "ASSET_B1")
    assert fills == [
        {"price": 0.4, "size": 20.0, "ts": 1779030000.0, "trade_id": "trade-2"}
    ]


def test_extract_buy_fills_case_insensitive_funder():
    fills = extract_buy_fills([TRADE_TWO_OURS_BUY], FUNDER.lower(), "ASSET_B2")
    assert fills == [
        {"price": 0.41, "size": 30.0, "ts": 1779030000.0, "trade_id": "trade-2"}
    ]


def test_extract_buy_fills_aggregates_across_trades_same_asset():
    t2 = {
        **TRADE_TWO_OURS_BUY,
        "id": "trade-3",
        "match_time": "1779031111",
        "maker_orders": [
            {
                "order_id": "ord-b3",
                "maker_address": FUNDER,
                "side": "BUY",
                "matched_amount": "10",
                "price": "0.42",
                "asset_id": "ASSET_B1",
            }
        ],
    }
    fills = extract_buy_fills([TRADE_TWO_OURS_BUY, t2], FUNDER, "ASSET_B1")
    assert {
        "price": 0.4, "size": 20.0, "ts": 1779030000.0, "trade_id": "trade-2"
    } in fills
    assert {
        "price": 0.42, "size": 10.0, "ts": 1779031111.0, "trade_id": "trade-3"
    } in fills
    assert len(fills) == 2


def test_extract_buy_fills_includes_taker_buy():
    fills = extract_buy_fills([TRADE_TAKER_BUY], FUNDER, "ASSET_T")
    assert fills == [
        {"price": 0.33, "size": 100.0, "ts": 1779040000.0, "trade_id": "trade-taker-1"}
    ]


def test_extract_buy_fills_maker_and_taker_no_double_count():
    maker_fills = extract_buy_fills([TRADE_TWO_OURS_BUY], FUNDER, "ASSET_B1")
    taker_fills = extract_buy_fills([TRADE_TAKER_BUY], FUNDER, "ASSET_T")
    assert maker_fills == [
        {"price": 0.4, "size": 20.0, "ts": 1779030000.0, "trade_id": "trade-2"}
    ]
    assert taker_fills == [
        {"price": 0.33, "size": 100.0, "ts": 1779040000.0, "trade_id": "trade-taker-1"}
    ]
    assert extract_buy_fills(
        [TRADE_TWO_OURS_BUY, TRADE_TAKER_BUY], FUNDER, "ASSET_T"
    ) == [
        {"price": 0.33, "size": 100.0, "ts": 1779040000.0, "trade_id": "trade-taker-1"}
    ]
```

在文件末尾新增一条聚焦断言：

```python
def test_extract_buy_fills_carries_trade_id_maker_and_taker():
    maker = extract_buy_fills([TRADE_TWO_OURS_BUY], FUNDER, "ASSET_B1")
    assert maker[0]["trade_id"] == "trade-2"
    taker = extract_buy_fills([TRADE_TAKER_BUY], FUNDER, "ASSET_T")
    assert taker[0]["trade_id"] == "trade-taker-1"
```

- [ ] **Step 2: 运行测试确认变红**

Run: `pytest tests/test_fills.py -q`
Expected: FAIL —— 上述断言里 fill 字典缺 `trade_id` 键。

- [ ] **Step 3: 实现——`extract_buy_fills` 两个分支都附 `trade_id`**

`engine/fills.py` 中，maker 分支与 taker 分支 append 的字典各加一个 `"trade_id"`。maker 分支当前在循环内，先在 trade 顶层取出 trade_id：

maker 分支（把现有 `out.append({...})` 改为带 trade_id）：

```python
    for tr in trades:
        ts = float(tr.get("match_time", 0) or 0)
        trade_id = tr.get("id")
        we_are_maker = False
        for mo in tr.get("maker_orders", []) or []:
            if str(mo.get("maker_address", "")).lower() != f:
                continue
            we_are_maker = True
            if str(mo.get("side", "")).upper() != "BUY":
                continue
            if str(mo.get("asset_id", "")) != a:
                continue
            out.append(
                {
                    "price": float(mo.get("price", 0) or 0),
                    "size": float(mo.get("matched_amount", 0) or 0),
                    "ts": ts,
                    "trade_id": trade_id,
                }
            )
```

taker 分支：

```python
        if (
            not we_are_maker
            and str(tr.get("trader_side", "")).upper() == "TAKER"
            and str(tr.get("side", "")).upper() == "BUY"
            and str(tr.get("asset_id", "")) == a
        ):
            out.append(
                {
                    "price": float(tr.get("price", 0) or 0),
                    "size": float(tr.get("size", 0) or 0),
                    "ts": ts,
                    "trade_id": trade_id,
                }
            )
```

同时更新 `extract_buy_fills` docstring 末句，把"返回 [{price, size, ts}, ...]"改为"返回 [{price, size, ts, trade_id}, ...]"。

- [ ] **Step 4: 运行测试确认变绿**

Run: `pytest tests/test_fills.py -q`
Expected: PASS（全部）。

- [ ] **Step 5: 提交**

```bash
git add engine/fills.py tests/test_fills.py
git commit -m "feat: extract_buy_fills 每条买入成交带 trade_id（供成本构成溯源）"
```

---

### Task 2: `cost_basis_with_lots` —— 成本 + 消耗的逐笔明细（纯函数）

**Files:**
- Modify: `engine/take_profit.py`（`cost_basis_from_buy_fills`，72-97 行）
- Test: `tests/test_take_profit.py`

- [ ] **Step 1: 写失败测试（先红）**

`tests/test_take_profit.py` 顶部 import 加入 `cost_basis_with_lots`：

```python
from engine.take_profit import (
    ceil_to_tick,
    plan_take_profit,
    cost_basis_from_buy_fills,
    cost_basis_with_lots,
    take_profit_price,
)
```

在 `TestCostBasisFromBuyFills` 类后新增带 trade_id 的 helper 与测试类：

```python
def _bft(price, size, ts, tid):
    return {"price": price, "size": size, "ts": ts, "trade_id": tid}


class TestCostBasisWithLots:
    def test_single_buy_one_lot(self):
        cost, lots = cost_basis_with_lots([_bft(0.28, 361, 100, "T1")], 361)
        assert cost == pytest.approx(0.28)
        assert lots == [{"price": 0.28, "take": 361.0, "ts": 100.0, "trade_id": "T1"}]

    def test_multi_buy_weighted_and_lots(self):
        fills = [_bft(0.20, 200, 1, "A"), _bft(0.28, 200, 2, "B")]
        cost, lots = cost_basis_with_lots(fills, 400)
        assert cost == pytest.approx(0.24)
        assert len(lots) == 2
        assert sum(l["take"] for l in lots) == pytest.approx(400.0)

    def test_partial_lot_take_is_consumed_amount(self):
        # 最新 161@0.28(ts2) 全取,更早 200@0.20(ts1) 只取 39 凑满 200
        fills = [_bft(0.20, 200, 1, "OLD"), _bft(0.28, 161, 2, "NEW")]
        cost, lots = cost_basis_with_lots(fills, 200)
        takes = {l["trade_id"]: l["take"] for l in lots}
        assert takes["NEW"] == pytest.approx(161.0)
        assert takes["OLD"] == pytest.approx(39.0)
        assert cost == pytest.approx((161 * 0.28 + 39 * 0.20) / 200)

    def test_insufficient_fills_graceful(self):
        cost, lots = cost_basis_with_lots([_bft(0.28, 100, 1, "X")], 361)
        assert cost == pytest.approx(0.28)
        assert lots[0]["take"] == pytest.approx(100.0)

    def test_empty_fills(self):
        assert cost_basis_with_lots([], 361) == (None, [])

    def test_zero_size(self):
        assert cost_basis_with_lots([_bft(0.28, 361, 1, "X")], 0) == (None, [])
```

- [ ] **Step 2: 运行测试确认变红**

Run: `pytest tests/test_take_profit.py::TestCostBasisWithLots -q`
Expected: FAIL —— `cannot import name 'cost_basis_with_lots'`。

- [ ] **Step 3: 实现——新增 `cost_basis_with_lots`，`cost_basis_from_buy_fills` 退化为薄包装**

`engine/take_profit.py` 中把现有 `cost_basis_from_buy_fills`（72-97 行）整段替换为：

```python
def cost_basis_with_lots(buy_fills: list[dict], size: float):
    """当前持仓的加权成本 + 被消耗的逐笔买入明细,来自我们的真实买入成交。

    算法同 cost_basis_from_buy_fills:按 ts 从新到旧累计取份额直至覆盖 size。额外
    返回每笔被消耗份额 {price, take(本笔实取量), ts, trade_id},供卖单理由溯源。
    返回 (cost_or_None, lots)。size<=0 或无成交 -> (None, [])。
    """
    if size <= 0 or not buy_fills:
        return None, []
    fills = sorted(buy_fills, key=lambda f: f.get("ts", 0) or 0, reverse=True)
    remaining = size
    cost_sum = 0.0
    qty_sum = 0.0
    lots: list[dict] = []
    for f in fills:
        if remaining <= 0:
            break
        fsize = float(f.get("size", 0) or 0)
        if fsize <= 0:
            continue
        take = min(fsize, remaining)
        price = float(f.get("price", 0) or 0)
        cost_sum += price * take
        qty_sum += take
        remaining -= take
        lots.append(
            {
                "price": price,
                "take": take,
                "ts": float(f.get("ts", 0) or 0),
                "trade_id": f.get("trade_id", ""),
            }
        )
    if qty_sum <= 0:
        return None, []
    return cost_sum / qty_sum, lots


def cost_basis_from_buy_fills(buy_fills: list[dict], size: float) -> float | None:
    """当前持仓的加权成本(仅成本,不含明细)。见 cost_basis_with_lots。"""
    return cost_basis_with_lots(buy_fills, size)[0]
```

- [ ] **Step 4: 运行测试确认变绿（含既有 cost_basis_from_buy_fills 回归）**

Run: `pytest tests/test_take_profit.py -q`
Expected: PASS（`TestCostBasisWithLots` 与既有 `TestCostBasisFromBuyFills` 全绿）。

- [ ] **Step 5: 提交**

```bash
git add engine/take_profit.py tests/test_take_profit.py
git commit -m "feat: cost_basis_with_lots 返回成本+逐笔消耗明细;旧函数退化为薄包装"
```

---

### Task 3: `describe_cost_basis` —— 成本构成中文片段（纯函数）

**Files:**
- Modify: `engine/take_profit.py`（顶部 import；文件末尾新增函数）
- Test: `tests/test_take_profit.py`

- [ ] **Step 1: 写失败测试（先红）**

import 加入 `describe_cost_basis`：

```python
from engine.take_profit import (
    ceil_to_tick,
    plan_take_profit,
    cost_basis_from_buy_fills,
    cost_basis_with_lots,
    describe_cost_basis,
    take_profit_price,
)
```

新增测试类（时间串只验结构 `-`/`:`，不验时区相关数值）：

```python
class TestDescribeCostBasis:
    def _lot(self, price, take, ts, tid):
        return {"price": price, "take": take, "ts": ts, "trade_id": tid}

    def test_lists_each_buy_with_price_share_and_trade_id(self):
        lots = [
            self._lot(0.27, 200.0, 1779030000, "0xabcdef1234567890ff"),
            self._lot(0.29, 161.0, 1779031111, "0x9c00000000000000d1"),
        ]
        s = describe_cost_basis(0.28, lots)
        assert s.startswith("成本=0.2800")
        assert "加权自2笔买入成交" in s
        assert "0.2700×200股" in s
        assert "0.2900×161股" in s
        assert "共取361股" in s
        # trade_id 缩写为 首6+'..'+末4
        assert "[trade 0xabcd..90ff]" in s
        assert "[trade 0x9c00..00d1]" in s
        # 时间被格式化(含分隔符),不校验具体时区数值
        assert "-" in s and ":" in s

    def test_short_trade_id_kept_as_is(self):
        s = describe_cost_basis(0.30, [self._lot(0.30, 1000.0, 1, "t")])
        assert "[trade t]" in s

    def test_fractional_share_two_decimals(self):
        s = describe_cost_basis(0.30, [self._lot(0.30, 222.08, 1, "t")])
        assert "×222.08股" in s
        assert "共取222.08股" in s

    def test_caps_lots_and_summarizes_overflow(self):
        lots = [self._lot(0.20 + i * 0.01, 10.0, i, f"t{i}") for i in range(8)]
        s = describe_cost_basis(0.235, lots)
        assert "…等共8笔" in s
        assert "共取80股" in s

    def test_none_cost(self):
        assert "无买入成交" in describe_cost_basis(None, [])
```

- [ ] **Step 2: 运行测试确认变红**

Run: `pytest tests/test_take_profit.py::TestDescribeCostBasis -q`
Expected: FAIL —— `cannot import name 'describe_cost_basis'`。

- [ ] **Step 3: 实现**

`engine/take_profit.py` 顶部把 `import math` 改为：

```python
import math
import time
```

在文件末尾追加：

```python
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def _fmt_share(x: float) -> str:
    """份额去掉无意义的 .0;非整数保留两位小数。"""
    return str(int(round(x))) if abs(x - round(x)) < 1e-9 else f"{x:.2f}"


def _short_tid(tid) -> str:
    tid = str(tid or "")
    return tid if len(tid) <= 12 else f"{tid[:6]}..{tid[-4:]}"


def describe_cost_basis(cost, lots: list[dict], max_lots: int = 6) -> str:
    """成本构成的中文片段(纯函数),供止盈/止损卖单理由引用。

    lots 来自 cost_basis_with_lots,按 ts 正序(最早->最新)逐笔列:
    "①时间 价格×份额股 [trade 缩写id]"。超过 max_lots 笔时列前 max_lots 笔
    + "…等共N笔"。时间用本地时区 MM-DD HH:MM。cost 为 None 时给降级文案。
    """
    n = len(lots)
    if cost is None or n == 0:
        return "成本=无（无买入成交）"
    ordered = sorted(lots, key=lambda l: l.get("ts", 0) or 0)
    total_take = sum(float(l.get("take", 0) or 0) for l in ordered)
    parts = []
    for i, l in enumerate(ordered[:max_lots]):
        mark = _CIRCLED[i] if i < len(_CIRCLED) else f"{i + 1}."
        t = time.strftime("%m-%d %H:%M", time.localtime(float(l.get("ts", 0) or 0)))
        parts.append(
            f"{mark}{t} {float(l.get('price', 0) or 0):.4f}"
            f"×{_fmt_share(float(l.get('take', 0) or 0))}股 "
            f"[trade {_short_tid(l.get('trade_id', ''))}]"
        )
    more = f" …等共{n}笔" if n > max_lots else ""
    return (
        f"成本={cost:.4f}（加权自{n}笔买入成交："
        f"{' '.join(parts)}{more} 共取{_fmt_share(total_take)}股）"
    )
```

- [ ] **Step 4: 运行测试确认变绿**

Run: `pytest tests/test_take_profit.py -q`
Expected: PASS（含 `TestDescribeCostBasis` 全部）。

- [ ] **Step 5: 提交**

```bash
git add engine/take_profit.py tests/test_take_profit.py
git commit -m "feat: describe_cost_basis 生成成本逐笔构成文案(时间+价格×份额+trade_id)"
```

---

### Task 4: monitor 止盈接入 `_cost_lots` —— 无成交跳过+告警、理由含成本构成

**Files:**
- Modify: `engine/monitor.py`（import 7-11 行；新增 `_cost_lots`；重写 `_reconcile_take_profit` 246-330 行）
- Test: `tests/test_monitor.py`

本任务**新增** `_cost_lots`、**不动** `_cost`/`_cost_with_source`（止损 Task 5 再切换），保证全程绿。

- [ ] **Step 1: 改/写测试（先红）**

`tests/test_monitor.py` 的 `TestCheckTakeProfit` 类内：

(a) **删掉** `test_falls_back_to_avgprice_when_no_trades`（380-398 行），替换为跳过+告警：

```python
    def test_skips_and_warns_when_no_buy_fills(self):
        # get_trades 取不到买入成交 -> 不挂卖单,写 ⚠️裸奔 状态行(avgPrice 不再兜底)
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(size=222.08, avg=0.30)]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = []  # 无成交
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.begin_status_tick()
        monitor.check_take_profit()
        monitor.publish_status()

        api.place_limit_sell.assert_not_called()
        api.cancel_orders.assert_not_called()
        rows = get_snapshot()["rows"]
        assert any("裸奔" in r.get("action", "") for r in rows)
```

(b) **改** `test_no_fallback_when_get_trades_has_data`（400-421 行）的理由断言，从 `"get_trades加权"` 改为校验逐笔构成、且不含 avgPrice 兜底：

```python
    def test_uses_get_trades_cost_with_composition_in_basis(self):
        # get_trades 有成交 -> 用加权成本(0.30),理由含逐笔构成,绝不出现 avgPrice
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(size=222.08, avg=0.99)]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = [self._buy(price=0.30, size=222.08)]
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        api.place_limit_sell.assert_called_once_with(
            "tok1", 0.30, 222.08, tick_size="0.01"  # 0.30 来自 get_trades,非 avg 0.99
        )
        tp = next(
            c
            for c in db.record_action.call_args_list
            if c.kwargs["action_type"] == "take_profit_sell"
        )
        basis = tp.kwargs["price_basis"]
        assert "加权自1笔买入成交" in basis
        assert "×" in basis and "trade" in basis and "共取" in basis
        assert "avgPrice" not in basis
```

(c) **改** `test_skips_when_both_sources_unavailable`（367-378 行）注释与名（行为不变：get_trades 空即跳过）：

```python
    def test_skips_when_no_buy_fills_and_no_avg(self):
        # get_trades 无成交 -> 跳过(不动卖单)。avgPrice 已不参与,此处 avg=0 仅占位
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(avg=0.0)]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = []
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        api.place_limit_sell.assert_not_called()
        api.cancel_orders.assert_not_called()
```

- [ ] **Step 2: 运行测试确认变红**

Run: `pytest tests/test_monitor.py::TestCheckTakeProfit -q`
Expected: FAIL —— 新 `test_skips_and_warns_when_no_buy_fills` 因当前会回落 avgPrice 而仍挂卖单（place_limit_sell 被调用）；`test_uses_get_trades_cost_with_composition_in_basis` 因 basis 仍是旧 `成本=get_trades加权 ...` 格式、无"加权自1笔"。

- [ ] **Step 3: 实现——monitor import + 新增 `_cost_lots` + 重写 `_reconcile_take_profit`**

`engine/monitor.py` 顶部 import（7-11 行）改为（保留 `cost_basis_from_buy_fills`，Task 5 才删；`_cost` 仍在用它）：

```python
from engine.take_profit import (
    plan_take_profit,
    cost_basis_from_buy_fills,
    cost_basis_with_lots,
    describe_cost_basis,
    take_profit_price,
)
```

在 `_cost_with_source`（116-127 行）之后新增方法：

```python
    def _cost_lots(self, asset_id: str, size: float):
        """该持仓的加权成本 + 逐笔构成(本 tick 缓存)。只来自 CLOB get_trades 的真实
        买入成交(maker∪taker),绝不回落 Data API avgPrice。取不到 -> (None, [])。"""
        key = ("lots", asset_id)
        if key in self._cost_cache:
            return self._cost_cache[key]
        funder = self._funder()
        try:
            trades = self.api.get_trades(TradeParams(asset_id=asset_id))
        except Exception as e:
            logger.warning("get_trades(asset=%s) for cost failed: %s", asset_id, e)
            self._cost_cache[key] = (None, [])
            return None, []
        fills = extract_buy_fills(trades, funder, asset_id)
        result = cost_basis_with_lots(fills, size)
        self._cost_cache[key] = result
        return result
```

把 `_reconcile_take_profit`（246-330 行）整段替换为：

```python
    def _reconcile_take_profit(self, pos: dict, open_orders: list):
        asset_id = pos.get("asset", "")
        size = float(pos.get("size", 0) or 0)
        cid = pos.get("conditionId", "")
        if size <= 0:
            return
        cost, lots = self._cost_lots(asset_id, size)
        if cost is None or cost <= 0:
            logger.warning(
                "Take-profit skipped (no buy fills) asset=%s size=%s — UNPROTECTED",
                asset_id,
                size,
            )
            self._status_add(
                market=cid,
                side="卖出",
                price="-",
                size=str(size),
                matched="-",
                stage="止盈卖单",
                action="⚠️跳过·裸奔",
                detail="get_trades 无买入成交、无法算成本，未挂止盈（绝不用 avgPrice 兜底），该持仓未受保护",
            )
            return
        tick, tick_str, best_bid = self._sell_book(asset_id)
        want = take_profit_price(cost, best_bid, tick)
        sells = [
            o
            for o in open_orders
            if o.get("asset_id") == asset_id and o.get("side") == "SELL"
        ]
        plan = plan_take_profit(size, want, tick, sells)
        if plan["action"] in ("noop", "keep"):
            self._status_add(
                market=cid,
                side="卖出",
                price=f"{want:.4f}",
                size=str(size),
                matched="-",
                stage="止盈卖单",
                action="保持(成本/护栏价)",
                detail=f"成本{cost:.4f} 持仓{size} 已挂一笔 {want:.4f}",
            )
            return
        if plan["cancel_ids"]:
            try:
                self.api.cancel_orders(plan["cancel_ids"])
                self._record_action(
                    market_id=cid,
                    action_type="take_profit_recancel",
                    side="-",
                    price=-1,
                    size=size,
                    reason="撤销与持仓不符的旧止盈卖单（价格/数量不符或被拆成多笔），改为按持仓挂单一笔",
                    price_basis=(
                        f"撤 {len(plan['cancel_ids'])} 笔 SELL；"
                        f"来源：CLOB get_open_orders（asset={asset_id} 的 SELL）"
                    ),
                )
            except Exception as e:
                logger.warning("Cancel stale sells for %s failed: %s", asset_id, e)
                return
        try:
            self.api.place_limit_sell(asset_id, want, size, tick_size=tick_str)
        except Exception as e:
            logger.warning("Place take-profit sell for %s failed: %s", asset_id, e)
            return
        self._record_action(
            market_id=cid,
            action_type="take_profit_sell",
            side="卖出",
            price=want,
            size=size,
            reason="按真实成交加权成本挂止盈卖单，并加穿价护栏（不亏本金、不穿价市价清仓、赚流动性奖励）",
            price_basis=(
                f"{describe_cost_basis(cost, lots)}；"
                f"卖价=max(成本,买一+1tick)={want:.4f}；来源：CLOB get_trades + get_orderbook"
            ),
        )
        self._status_add(
            market=cid,
            side="卖出",
            price=f"{want:.4f}",
            size=str(size),
            matched="-",
            stage="止盈卖单",
            action="按成本挂单",
            detail=f"成本{cost:.4f} 持仓{size} 挂卖{want:.4f}",
        )
```

> 注：`_cost_lots` 用 `("lots", asset_id)` 作缓存键，与遗留 `_cost` 的纯 `asset_id` 键互不冲突，Task 5 删除 `_cost` 后可简化，但当前两套并存不影响正确性。

- [ ] **Step 4: 运行测试确认变绿**

Run: `pytest tests/test_monitor.py::TestCheckTakeProfit -q`
Expected: PASS（含新 `test_skips_and_warns_when_no_buy_fills`、`test_uses_get_trades_cost_with_composition_in_basis`、`test_skips_when_no_buy_fills_and_no_avg`，及既有 taker/护栏/keep 等用例）。

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat: 止盈成本只认 get_trades 逐笔;无成交则跳过+裸奔告警,理由含成本构成"
```

---

### Task 5: monitor 止损接入 `_cost_lots` + 删除 avgPrice 兜底残留

**Files:**
- Modify: `engine/monitor.py`（重写 `_check_pos_sl` 355-429 行；删除 `_cost` 96-114 行与 `_cost_with_source` 116-127 行；清理 import）
- Test: `tests/test_monitor.py`（`TestStopLoss`）

- [ ] **Step 1: 改/写测试（先红）**

`tests/test_monitor.py` 的 `TestStopLoss` 类内：

(a) **删掉** `test_stop_loss_falls_back_to_avgprice`（598-624 行），替换为跳过+告警：

```python
    def test_stop_loss_skips_and_warns_when_no_buy_fills(self):
        # get_trades 取不到买入成交 -> 不止损(不撤单/不市价平仓),写 ⚠️ 状态行
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        api.get_user_positions.return_value = [
            {
                "asset": "tok1",
                "size": 1000.0,
                "avgPrice": 0.30,
                "curPrice": 0.24,
                "conditionId": "mkt1",
            }
        ]
        api.get_open_orders.return_value = [
            {"id": "sell1", "asset_id": "tok1", "side": "SELL"},
        ]
        api.get_trades.return_value = []  # 无成交 -> 不再回落 avgPrice

        monitor.begin_status_tick()
        monitor.check_stop_loss()
        monitor.publish_status()

        api.place_market_sell.assert_not_called()
        api.cancel_orders.assert_not_called()
        rows = get_snapshot()["rows"]
        assert any("无成本" in r.get("action", "") for r in rows)
```

(b) **改** `test_triggers_stop_loss_when_price_drops`（479-517 行）末尾加断言：成本来自 get_trades、理由含构成且无 avgPrice：

在该测试 `db.record_trade.assert_called_once()` 之后追加：

```python
        sl = next(
            c
            for c in db.record_action.call_args_list
            if c.kwargs["action_type"] == "stoploss_market_sell"
        )
        assert "加权自1笔买入成交" in sl.kwargs["price_basis"]
        assert "avgPrice" not in sl.kwargs["price_basis"]
```

(c) **改** `test_skips_stop_loss_when_both_sources_unavailable`（580-596 行）注释（行为不变：get_trades 空即跳过）：

```python
    def test_skips_stop_loss_when_no_buy_fills(self):
        # get_trades 无成交 -> 不止损(avgPrice 不再参与)
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        api.get_user_positions.return_value = [
            {
                "asset": "tok1",
                "size": 1000.0,
                "avgPrice": 0.0,
                "curPrice": 0.10,
                "conditionId": "mkt1",
            }
        ]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = []

        monitor.check_stop_loss()

        api.place_market_sell.assert_not_called()
```

- [ ] **Step 2: 运行测试确认变红**

Run: `pytest tests/test_monitor.py::TestStopLoss -q`
Expected: FAIL —— `test_stop_loss_skips_and_warns_when_no_buy_fills` 因当前止损仍回落 avgPrice 而市价平仓（place_market_sell 被调用）；`test_triggers_stop_loss_when_price_drops` 的新 basis 断言因旧格式无"加权自1笔"而失败。

- [ ] **Step 3: 实现——重写 `_check_pos_sl`，删除 `_cost`/`_cost_with_source`，清理 import**

把 `_check_pos_sl`（355-429 行）整段替换为：

```python
    def _check_pos_sl(self, pos: dict, open_orders: list, settings: dict):
        # Confirmed Data API position fields: asset / size / avgPrice /
        # curPrice / conditionId. avgPrice 仅供占位,不参与成本计算。
        asset_id = pos.get("asset", "")
        size = float(pos.get("size", 0) or 0)
        cur = float(pos.get("curPrice", 0) or 0)
        cid = pos.get("conditionId", "")
        if size <= 0:
            return
        cost, lots = self._cost_lots(asset_id, size)
        # 成本只认 get_trades 加权;取不到 -> 不做止损(绝不用 avgPrice 兜底)。
        if cost is None or cost <= 0:
            logger.warning(
                "Stop-loss skipped (no buy fills) asset=%s size=%s — no cost basis",
                asset_id,
                size,
            )
            self._status_add(
                market=cid,
                side="卖出",
                price="-",
                size=str(size),
                matched="-",
                stage="Step2",
                action="⚠️跳过·无成本",
                detail="get_trades 无买入成交、无法算成本，未做止损保护",
            )
            return
        if not stop_loss_triggered(cur, cost, settings["stop_loss_pct"]):
            return
        sell_ids = [
            o["id"]
            for o in open_orders
            if o.get("asset_id") == asset_id and o.get("side") == "SELL"
        ]
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
            pnl=(cur - cost) * size,
        )
        self._record_action(
            market_id=cid,
            action_type="stoploss_market_sell",
            side="卖出",
            price=cur,
            size=size,
            reason=f"现价 {cur:.4f} 跌破成本价 {cost:.4f} 的止损阈值 成本×(1-止损比例{settings['stop_loss_pct']}%)，市价平仓止损",
            price_basis=(
                f"{describe_cost_basis(cost, lots)}、现价 curPrice={cur:.4f}；"
                f"来源：CLOB get_trades + Data API /positions"
            ),
        )
        logger.warning(
            "Stop-loss executed: asset=%s size=%s cur=%.4f cost=%.4f",
            asset_id,
            size,
            cur,
            cost,
        )
        self._status_add(
            market=cid,
            side="卖出",
            price=f"{cur:.4f}",
            size=str(size),
            matched="-",
            stage="Step2",
            action="止损→市价平仓",
            detail=f"cur{cur:.4f}<成本{cost:.4f} 触发",
        )
```

删除现已无用的 `_cost`（96-114 行）与 `_cost_with_source`（116-127 行）两个方法整段。

清理 import（7-11 行）——`cost_basis_from_buy_fills` 不再被 monitor 引用，删掉：

```python
from engine.take_profit import (
    plan_take_profit,
    cost_basis_with_lots,
    describe_cost_basis,
    take_profit_price,
)
```

把 `_cost_lots` 的缓存键从 `("lots", asset_id)` 简化回 `asset_id`（`_cost` 已删，无冲突）：

```python
    def _cost_lots(self, asset_id: str, size: float):
        """该持仓的加权成本 + 逐笔构成(本 tick 缓存)。只来自 CLOB get_trades 的真实
        买入成交(maker∪taker),绝不回落 Data API avgPrice。取不到 -> (None, [])。"""
        if asset_id in self._cost_cache:
            return self._cost_cache[asset_id]
        funder = self._funder()
        try:
            trades = self.api.get_trades(TradeParams(asset_id=asset_id))
        except Exception as e:
            logger.warning("get_trades(asset=%s) for cost failed: %s", asset_id, e)
            self._cost_cache[asset_id] = (None, [])
            return None, []
        fills = extract_buy_fills(trades, funder, asset_id)
        result = cost_basis_with_lots(fills, size)
        self._cost_cache[asset_id] = result
        return result
```

- [ ] **Step 4: 运行测试确认变绿**

Run: `pytest tests/test_monitor.py -q`
Expected: PASS（`TestStopLoss` 全部 + `TestCheckTakeProfit` 不回归；`grep avgPrice兜底` 应在 monitor.py 中绝迹）。

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat: 止损成本只认 get_trades 逐笔;删除 avgPrice 兜底(_cost/_cost_with_source),无成交则跳过+告警"
```

---

### Task 6: 文档 + 记忆 + 全量回归

**Files:**
- Modify: `CLAUDE.md`（止盈/止损那段）
- Modify: `C:\Users\Hank\.claude\projects\C--Users-Hank-PycharmProjects-poly----\memory\take-profit-position-driven.md`
- Modify: `C:\Users\Hank\.claude\projects\C--Users-Hank-PycharmProjects-poly----\memory\MEMORY.md`（对应那行 hook）

- [ ] **Step 1: 全量测试**

Run: `pytest -q`
Expected: PASS（全绿）。若有遗漏的 avgPrice/兜底相关断言失败，按 spec 改为"无成交即跳过"口径修正后重跑。

- [ ] **Step 2: 更新 `CLAUDE.md`**

在 "Critical behaviors to preserve" 的 take-profit 段落里，确认并补充：成本基准是 get_trades 加权（`cost_basis_with_lots`），**任何情况都不再用 Data API avgPrice**；当 get_trades 取不到买入成交时，止盈/止损都跳过该持仓并在监控状态表里醒目告警（`⚠️跳过·裸奔` / `⚠️跳过·无成本`），不挂卖单也不止损；止盈/止损卖单理由（`actions.price_basis`）列出参与加权的逐笔买入成交（时间+价格×份额+trade_id，`describe_cost_basis`）。删去任何"avgPrice 兜底"措辞。

- [ ] **Step 3: 更新记忆**

`take-profit-position-driven.md` 正文把"2026-05-24 补强：补抓 taker 成交 + 受控 avgPrice 最后兜底（avgPrice 不再全禁）"更新为：

> 2026-05-26：avgPrice 全面禁用——成本只认 get_trades 逐笔加权（`cost_basis_with_lots`），取不到买入成交即跳过止盈/止损并醒目告警（裸奔），绝不回落 avgPrice；卖单理由含逐笔成本构成（时间+价格×份额+trade_id）。保留 2026-05-24 的 taker 成交补抓。

`MEMORY.md` 对应那一行的 hook 文案同步把"受控 avgPrice 最后兜底（avgPrice 不再全禁）"改为"avgPrice 全面禁用，成本只认 get_trades 逐笔，无成交则跳过+告警"。

- [ ] **Step 4: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md 标注成本只认 get_trades、无成交跳过+告警、理由含逐笔构成"
```

（记忆文件在仓库外，不进 git；直接写盘即可。）

---

## Self-Review

**Spec coverage：**
- 成本只来自 get_trades 加权（maker∪taker）→ Task 2 `cost_basis_with_lots` + Task 4/5 `_cost_lots`。✅
- 彻底移除 avgPrice 兜底（avgPrice 不参与成本）→ Task 5 删 `_cost_with_source`/`_cost`、止损改用 `_cost_lots`；Task 4 止盈已不读 avgPrice。✅
- 取不到成交 → 跳过 + 醒目告警（止盈 `⚠️跳过·裸奔`、止损 `⚠️跳过·无成本` + log.warning）→ Task 4/5。✅
- 卖单理由含逐笔构成（时间+价格×份额+trade_id）→ Task 1（trade_id）+ Task 3（`describe_cost_basis`）+ Task 4/5（接入 price_basis）。✅
- 不改卖价公式/穿价护栏 → 各任务均未触 `take_profit_price`/`plan_take_profit`。✅
- 保留 taker 成交补抓 → `extract_buy_fills` taker 分支不动、`_cost_lots` 仍不传 maker_address。✅
- 不往 actions 表每 tick 写裸奔记录 → 跳过路径只 `_status_add`+log，无 `_record_action`。✅
- 上限 6 笔 + 汇总 → Task 3 `max_lots=6`、`…等共N笔`。✅

**Placeholder scan：** 无 TBD/TODO；每个改码步骤均给出完整代码与精确文件位置。✅

**Type consistency：** `cost_basis_with_lots(buy_fills, size) -> (cost|None, lots)`、lot 字段 `{price, take, ts, trade_id}`、`describe_cost_basis(cost, lots, max_lots=6) -> str`、`_cost_lots(asset_id, size) -> (cost|None, lots)`——Task 2/3/4/5 引用一致。`extract_buy_fills` fill 字段全程 `{price, size, ts, trade_id}`。✅
