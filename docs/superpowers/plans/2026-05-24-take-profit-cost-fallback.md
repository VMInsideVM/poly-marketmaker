# 止盈/止损成本价兜底 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让任何 `size>0` 的持仓都能拿到止盈卖单与止损保护——补抓我们当 taker 的买入成交（覆盖 app 自己的仓位），并在 `get_trades` 真取不到成交时回落到 Data API `avgPrice`（覆盖外部仓位）。

**Architecture:** 两层。第一层在纯函数 `extract_buy_fills` 增加 taker 分支，并让 `_cost` 取数去掉 `maker_address` 过滤，使我们当 taker 的成交也进入加权成本。第二层在止盈/止损调用点引入 `_cost_with_source` 帮助函数：`get_trades` 加权成本取不到时（仅此时）回落 `pos["avgPrice"]`，止盈靠既有穿价护栏兜底、止损为已接受风险。无状态、不改 DB。

**Tech Stack:** Python 3.12、pytest、py-clob-client-v2（`TradeParams`/`get_trades`）、Polymarket Data API `/positions`。

参考设计：`docs/superpowers/specs/2026-05-24-take-profit-cost-fallback-design.md`

---

## File Structure

- `dump_trades.py`（新增，顶层手动脚本，与 `test_live.py`/`test_real_order.py` 并列）：Task 1 验证 `get_trades` 真实结构用，**不进 pytest 套件**。
- `engine/fills.py`（修改 `extract_buy_fills`）：增加 taker 买入分支。纯函数。
- `engine/monitor.py`（修改 `_cost` 取数、新增 `_cost_with_source`、改 `_reconcile_take_profit` 与 `_check_pos_sl`）。
- `tests/test_fills.py`（新增 taker 用例）。
- `tests/test_monitor.py`（新增兜底/ taker 用例，更新两个"取不到成本→跳过"用例）。

---

### Task 1: 验证 get_trades 真实结构（手动脚本，需用户配合运行）

这是第一层的地基。`extract_buy_fills` 的 taker 分支与 `_cost` 去掉 `maker_address` 都依赖两个事实：(a) 去掉 `maker_address` 后 `/data/trades` 只返回**本钱包**两种角色的成交，而非全市场；(b) 我们当 taker 的买入成交带 `trader_side == "TAKER"`、顶层 `side/size/price/asset_id/match_time` 即我们的成交。本任务用真实数据确认这两点。

**Files:**
- Create: `dump_trades.py`

- [ ] **Step 1: 写验证脚本**

```python
# dump_trades.py — 手动验证 get_trades 真实结构（非 pytest）。
# 用法：python dump_trades.py   （会要求输入访问密码）
import json
import hashlib
from models.database import Database
from utils.crypto import decrypt, derive_key
from config import DB_PATH
from py_clob_client_v2.clob_types import TradeParams


def main():
    db = Database(DB_PATH)
    db.init()
    pw_hash, salt = db.get_password()
    if pw_hash is None:
        print("未设置密码")
        return
    password = input("请输入访问密码: ")
    key = derive_key(password, salt)
    if hashlib.sha256(key).hexdigest() != pw_hash:
        print("密码错误")
        return
    w = db.list_wallets()[0]
    private_key = decrypt(w["encrypted_key"], key)

    from api.polymarket_api import PolymarketAPI

    api = PolymarketAPI(private_key, funder=w.get("funder") or None)
    funder = api.get_funder()
    print(f"funder = {funder}")

    positions = api.get_user_positions(funder)
    positions = [p for p in positions if float(p.get("size", 0) or 0) > 0]
    if not positions:
        print("当前无持仓，无法验证；请在有持仓时再跑")
        return
    asset_id = positions[0]["asset"]
    print(f"用持仓 asset_id = {asset_id} 验证\n")

    with_maker = api.get_trades(TradeParams(maker_address=funder, asset_id=asset_id))
    no_maker = api.get_trades(TradeParams(asset_id=asset_id))
    print(f"带 maker_address: {len(with_maker)} 笔")
    print(f"不带 maker_address: {len(no_maker)} 笔\n")

    f = funder.lower()
    for tr in no_maker:
        maker_addrs = [
            str(mo.get("maker_address", "")).lower()
            for mo in tr.get("maker_orders", []) or []
        ]
        ours_in_makers = f in maker_addrs
        print(
            f"id={tr.get('id')} trader_side={tr.get('trader_side')} "
            f"top.side={tr.get('side')} top.asset={tr.get('asset_id')} "
            f"top.size={tr.get('size')} top.price={tr.get('price')} "
            f"我们在maker_orders={ours_in_makers}"
        )

    with open("trades_dump.json", "w", encoding="utf-8") as fh:
        json.dump(no_maker, fh, ensure_ascii=False, indent=2)
    print("\n完整返回已写入 trades_dump.json")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 用户运行并核对（人工 gate）**

Run: `python dump_trades.py`（需用户提供能登录的钱包/密码环境）
人工确认：
1. **不带 `maker_address` 的返回只包含本钱包的成交**——每笔要么 `我们在maker_orders=True`，要么 `trader_side=TAKER`。若出现既不在 maker_orders、`trader_side` 又非 TAKER 的"无关成交" → **停止，按设计文档 §第一层§0 的应急方案改为"保留 maker 查询 + 额外按 taker 维度单独查一次"**，并回到本计划修订 Task 2/Task 3。
2. 若历史里存在我们当 taker 的买入：确认它 `trader_side=TAKER`、`top.side=BUY`、`top.asset` 等于 `asset_id`。若历史里没有 taker 买入，则依据现有 `tests/test_fills.py` fixture 已佐证的 `trader_side` 字段继续（taker 分支用 `trader_side=="TAKER"` 判定）。

- [ ] **Step 3: 提交脚本**

```bash
git add dump_trades.py
git commit -m "chore: 新增 dump_trades.py 验证 get_trades 真实结构(手动脚本)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `extract_buy_fills` 增加 taker 买入分支

**Files:**
- Modify: `engine/fills.py:48-74`（`extract_buy_fills`）
- Test: `tests/test_fills.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_fills.py` 末尾追加：

```python
# 我们当 taker 的买入:成交在 trade 顶层,maker_orders 里是对手方(不是我们)
TRADE_TAKER_BUY = {
    "id": "trade-taker-1",
    "side": "BUY",
    "size": "100",
    "price": "0.33",
    "asset_id": "ASSET_T",
    "market": "COND_T",
    "match_time": "1779040000",
    "trader_side": "TAKER",
    "maker_orders": [
        {
            "order_id": "ord-counterparty",
            "maker_address": "0x49c40bD313D8599F54B62fff13324a790c4fBf77",
            "side": "SELL",
            "matched_amount": "100",
            "price": "0.33",
            "asset_id": "ASSET_T",
        }
    ],
}


def test_extract_buy_fills_includes_taker_buy():
    fills = extract_buy_fills([TRADE_TAKER_BUY], FUNDER, "ASSET_T")
    assert fills == [{"price": 0.33, "size": 100.0, "ts": 1779040000.0}]


def test_extract_buy_fills_taker_sell_ignored():
    t = {**TRADE_TAKER_BUY, "side": "SELL"}
    assert extract_buy_fills([t], FUNDER, "ASSET_T") == []


def test_extract_buy_fills_taker_wrong_asset_ignored():
    assert extract_buy_fills([TRADE_TAKER_BUY], FUNDER, "OTHER_ASSET") == []


def test_extract_buy_fills_maker_and_taker_no_double_count():
    # 一个 maker 买入(ASSET_B1@0.4x20) + 一个 taker 买入(ASSET_T@0.33x100)
    maker_fills = extract_buy_fills([TRADE_TWO_OURS_BUY], FUNDER, "ASSET_B1")
    taker_fills = extract_buy_fills([TRADE_TAKER_BUY], FUNDER, "ASSET_T")
    assert maker_fills == [{"price": 0.4, "size": 20.0, "ts": 1779030000.0}]
    assert taker_fills == [{"price": 0.33, "size": 100.0, "ts": 1779040000.0}]
    # 混在一起按各自 asset 提取,互不串扰
    assert extract_buy_fills(
        [TRADE_TWO_OURS_BUY, TRADE_TAKER_BUY], FUNDER, "ASSET_T"
    ) == [{"price": 0.33, "size": 100.0, "ts": 1779040000.0}]
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_fills.py::test_extract_buy_fills_includes_taker_buy -v`
Expected: FAIL（当前 `extract_buy_fills` 无 taker 分支，返回 `[]`）

- [ ] **Step 3: 实现 taker 分支**

把 `engine/fills.py` 的 `extract_buy_fills` 改为（在 maker 遍历之后增加 taker 分支）：

```python
def extract_buy_fills(trades: list[dict], funder: str, asset_id: str) -> list[dict]:
    """挑出我们在某 token 上的全部 BUY 成交,用于算加权成本。

    返回 [{price, size, ts}, ...](不去重、不依赖 seen_keys)。两种来源:
    - maker:我们挂的买单被吃,成交在 trade.maker_orders 里(按 funder 过滤,
      side==BUY,asset 匹配);price=mo.price,size=mo.matched_amount。
    - taker:我们的 GTC 买单下单瞬间可成交而以 taker 立即成交,成交在 trade 顶层
      (maker_orders 里是对手方,不是我们)。当 trade.trader_side==TAKER 且顶层
      side==BUY 且 asset 匹配:price=trade.price,size=trade.size。
    一笔 trade 对我们而言要么是 maker 要么是 taker,两分支天然互斥,不会重复计入。
    ts 取 trade 顶层 match_time。
    """
    f = (funder or "").lower()
    a = str(asset_id)
    out = []
    for tr in trades:
        ts = float(tr.get("match_time", 0) or 0)
        for mo in tr.get("maker_orders", []) or []:
            if str(mo.get("maker_address", "")).lower() != f:
                continue
            if str(mo.get("side", "")).upper() != "BUY":
                continue
            if str(mo.get("asset_id", "")) != a:
                continue
            out.append(
                {
                    "price": float(mo.get("price", 0) or 0),
                    "size": float(mo.get("matched_amount", 0) or 0),
                    "ts": ts,
                }
            )
        if (
            str(tr.get("trader_side", "")).upper() == "TAKER"
            and str(tr.get("side", "")).upper() == "BUY"
            and str(tr.get("asset_id", "")) == a
        ):
            out.append(
                {
                    "price": float(tr.get("price", 0) or 0),
                    "size": float(tr.get("size", 0) or 0),
                    "ts": ts,
                }
            )
    return out
```

- [ ] **Step 4: 运行全部 fills 测试，确认通过**

Run: `pytest tests/test_fills.py -v`
Expected: 全部 PASS（含原有 maker 用例——它们的 fixture 都是 `trader_side="MAKER"`，taker 分支不触发）

- [ ] **Step 5: 提交**

```bash
git add engine/fills.py tests/test_fills.py
git commit -m "feat: extract_buy_fills 补抓 taker 买入成交(GTC 买单以 taker 成交时)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `_cost` 取数去掉 maker_address 过滤

依赖 Task 1 Step 2 结论 1（不带 `maker_address` 只返回本钱包成交）。去掉服务端 maker 过滤后，我们当 taker 的成交也会进入 `extract_buy_fills`。

**Files:**
- Modify: `engine/monitor.py:104-106`（`_cost` 里的 `get_trades` 调用）
- Test: `tests/test_monitor.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_monitor.py` 的 `TestCheckTakeProfit` 类内追加（`_taker_buy` 辅助 + 两个测试）：

```python
    def _taker_buy(self, price=0.33, size=100.0, asset="tok1", ts="200"):
        # 我们当 taker 的买入:成交在顶层,maker_orders 是对手方
        return {
            "id": f"tt-{ts}",
            "market": "mkt1",
            "match_time": ts,
            "trader_side": "TAKER",
            "side": "BUY",
            "asset_id": asset,
            "size": str(size),
            "price": str(price),
            "maker_orders": [
                {
                    "order_id": f"cp-{ts}",
                    "maker_address": "0xCOUNTERPARTY",
                    "side": "SELL",
                    "matched_amount": str(size),
                    "price": str(price),
                    "asset_id": asset,
                }
            ],
        }

    def test_cost_query_omits_maker_address(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos()]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = [self._buy()]
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        params = api.get_trades.call_args.args[0]
        assert params.maker_address is None
        assert params.asset_id == "tok1"

    def test_places_sell_for_taker_acquired_position(self):
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(size=100.0, avg=0.33)]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = [self._taker_buy(price=0.33, size=100.0)]
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        api.place_limit_sell.assert_called_once_with(
            "tok1", 0.33, 100.0, tick_size="0.01"
        )
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_monitor.py::TestCheckTakeProfit::test_cost_query_omits_maker_address -v`
Expected: FAIL（当前 `_cost` 传了 `maker_address=funder`，断言 `is None` 不成立）

- [ ] **Step 3: 去掉 maker_address**

把 `engine/monitor.py` `_cost` 里的：

```python
            trades = self.api.get_trades(
                TradeParams(maker_address=funder, asset_id=asset_id)
            )
```

改为：

```python
            # 不传 maker_address:服务端返回本钱包两种角色的成交,使我们当 taker 的
            # 买入也进入加权成本(extract_buy_fills 内部仍按 funder 过滤 maker_orders)。
            trades = self.api.get_trades(TradeParams(asset_id=asset_id))
```

（`funder` 变量在 `_cost` 内仍用于传给 `extract_buy_fills`，保留不动。）

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/test_monitor.py::TestCheckTakeProfit -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat: _cost 取数去掉 maker_address,纳入 taker 买入成交

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `_cost_with_source` 帮助函数 + 止盈接入 avgPrice 兜底

**Files:**
- Modify: `engine/monitor.py`（新增 `_cost_with_source`；改 `_reconcile_take_profit:233-251` 与 `:295-306` price_basis）
- Test: `tests/test_monitor.py`

- [ ] **Step 1: 写失败测试 + 改既有"跳过"用例**

在 `tests/test_monitor.py` 把既有 `test_skips_when_cost_unavailable`（约 line 367）**整个替换**为下面的版本（删除旧函数、改名为 `test_skips_when_both_sources_unavailable`、用 `avg=0.0`）。**务必删除旧用例**——否则它默认 `avg=0.30` + get_trades 空，改完会被兜底挂单而失败：

```python
    def test_skips_when_both_sources_unavailable(self):
        # get_trades 无成交 且 avgPrice<=0 -> 两源皆空 -> 不动卖单
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(avg=0.0)]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = []
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        api.place_limit_sell.assert_not_called()
        api.cancel_orders.assert_not_called()
```

并在 `TestCheckTakeProfit` 内新增兜底用例：

```python
    def test_falls_back_to_avgprice_when_no_trades(self):
        # get_trades 0 笔 + avgPrice=0.30 -> 用 avgPrice 经穿价护栏挂卖
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(size=222.08, avg=0.30)]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = []  # get_trades 取不到
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        api.place_limit_sell.assert_called_once_with(
            "tok1", 0.30, 222.08, tick_size="0.01"
        )
        tp = next(
            c
            for c in db.record_action.call_args_list
            if c.kwargs["action_type"] == "take_profit_sell"
        )
        assert "avgPrice兜底" in tp.kwargs["price_basis"]

    def test_no_fallback_when_get_trades_has_data(self):
        # get_trades 有成交 -> 用加权成本,不碰 avgPrice(门控验证)
        monitor, api, db = _make_monitor()
        api.get_user_positions.return_value = [self._pos(size=222.08, avg=0.99)]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = [self._buy(price=0.30, size=222.08)]
        api.get_orderbook.return_value = {"tick_size": "0.01"}

        monitor.check_take_profit()

        api.place_limit_sell.assert_called_once_with(
            "tok1", 0.30, 222.08, tick_size="0.01"  # 0.30 来自 get_trades,非 avgPrice 0.99
        )
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_monitor.py::TestCheckTakeProfit::test_falls_back_to_avgprice_when_no_trades -v`
Expected: FAIL（当前 `cost is None` 直接跳过，不挂卖单）

- [ ] **Step 3: 新增 `_cost_with_source` 并接入止盈**

在 `engine/monitor.py` `_cost` 方法之后新增：

```python
    def _cost_with_source(self, asset_id: str, size: float, avg_fallback: float):
        """成本 + 来源。优先 get_trades 加权成本;取不到(None/<=0)且 avg_fallback>0
        时回落 Data API avgPrice。返回 (cost_or_None, source_str)。
        门控:get_trades 有成本时永远不碰 avgPrice。"""
        cost = self._cost(asset_id, size)  # get_trades 加权(本 tick 缓存)或 None
        if cost is not None and cost > 0:
            return cost, "get_trades加权"
        if avg_fallback and avg_fallback > 0:
            return float(avg_fallback), "avgPrice兜底"
        return None, ""
```

把 `_reconcile_take_profit` 里的：

```python
        cost = self._cost(asset_id, size)
        if cost is None or cost <= 0:
            self._status_add(
                market=cid,
                side="卖出",
                price="-",
                size=str(size),
                matched="-",
                stage="止盈卖单",
                action="跳过(无成交数据)",
                detail="get_trades 无买入成交，保持现有卖单不动",
            )
            return
```

改为：

```python
        avg_fallback = float(pos.get("avgPrice", 0) or 0)
        cost, source = self._cost_with_source(asset_id, size, avg_fallback)
        if cost is None or cost <= 0:
            self._status_add(
                market=cid,
                side="卖出",
                price="-",
                size=str(size),
                matched="-",
                stage="止盈卖单",
                action="跳过(无成交数据)",
                detail="get_trades 无买入成交且无 avgPrice，保持现有卖单不动",
            )
            return
```

并把同方法内 `record_action` 的 `price_basis`（约 `:302-305`）由：

```python
            price_basis=(
                f"成本=get_trades加权 {cost:.4f}；卖价=max(成本,买一+1tick)={want:.4f}；"
                f"来源：CLOB get_trades + get_orderbook"
            ),
```

改为：

```python
            price_basis=(
                f"成本={source} {cost:.4f}；卖价=max(成本,买一+1tick)={want:.4f}；"
                f"来源：CLOB get_trades + get_orderbook"
            ),
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/test_monitor.py::TestCheckTakeProfit -v`
Expected: 全部 PASS（含改名后的 `test_skips_when_both_sources_unavailable` 与原 `test_places_one_sell_at_cost_when_none_exist`——后者 price_basis 仍含 "get_trades"）

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat: 止盈 get_trades 取不到时回落 avgPrice(穿价护栏兜底)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 止损接入 avgPrice 兜底

**Files:**
- Modify: `engine/monitor.py:341-351`（`_check_pos_sl`）
- Test: `tests/test_monitor.py`

- [ ] **Step 1: 写失败测试 + 改既有"跳过"用例**

在 `tests/test_monitor.py` 把既有 `test_skips_stop_loss_when_cost_unavailable`（约 line 487）**整个替换**为下面的版本（删除旧函数、改名为 `test_skips_stop_loss_when_both_sources_unavailable`、`avgPrice` 改为 `0.0`）。**务必删除旧用例**——否则它 `avgPrice=0.30` + get_trades 空，改完会被兜底市价平仓而失败：

```python
    def test_skips_stop_loss_when_both_sources_unavailable(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        api.get_user_positions.return_value = [
            {
                "asset": "tok1",
                "size": 1000.0,
                "avgPrice": 0.0,  # 无 avgPrice
                "curPrice": 0.10,
                "conditionId": "mkt1",
            }
        ]
        api.get_open_orders.return_value = []
        api.get_trades.return_value = []  # get_trades 也空 -> 两源皆空

        monitor.check_stop_loss()

        api.place_market_sell.assert_not_called()
```

新增止损兜底用例：

```python
    def test_stop_loss_falls_back_to_avgprice(self):
        # get_trades 0 笔 + avgPrice=0.30,现价 0.24 -> 用 avgPrice 判定并市价平仓
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
        api.get_trades.return_value = []  # get_trades 取不到 -> 回落 avgPrice

        monitor.check_stop_loss()

        api.cancel_orders.assert_called_with(["sell1"])
        api.place_market_sell.assert_called_with("tok1", 1000.0)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_monitor.py::TestStopLoss::test_stop_loss_falls_back_to_avgprice -v`
Expected: FAIL（当前 `_cost` 为 None 直接 return，不平仓）

- [ ] **Step 3: 止损用 `_cost_with_source`**

把 `engine/monitor.py` `_check_pos_sl` 里的：

```python
        avg = self._cost(asset_id, size)  # 真实成交加权成本,替代 Data API avgPrice
        if avg is None or avg <= 0:
            return
```

改为：

```python
        avg_fallback = float(pos.get("avgPrice", 0) or 0)
        avg, _source = self._cost_with_source(asset_id, size, avg_fallback)
        # get_trades 加权成本优先;取不到时回落 Data API avgPrice。
        # 已知风险(已接受):市价平仓无穿价护栏,avgPrice 读高可能误触发。
        if avg is None or avg <= 0:
            return
```

（`avg` 之后的 `stop_loss_triggered(cur, avg, ...)`、pnl、记录全部沿用 `avg`，无需再改。）

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/test_monitor.py::TestStopLoss -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat: 止损 get_trades 取不到时回落 avgPrice(已知风险已接受)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 全量测试 + 收尾

**Files:** 无新增

- [ ] **Step 1: 跑完整测试套件**

Run: `pytest`
Expected: 全部 PASS（重点确认 `tests/test_fills.py` 与 `tests/test_monitor.py` 全绿；其余无回归）

- [ ] **Step 2: 若有失败，修到全绿**

逐个排查失败用例。常见点：改名后的两个"跳过"用例是否漏改、price_basis 断言子串是否匹配。

- [ ] **Step 3: 完成**

无需额外提交（各任务已分别提交）。向用户汇报：两层已落地，并提示 Task 1 的 live 验证结论（尤其"不带 maker_address 是否只返回本钱包成交")。

---

## 守住的既有关键行为（不改）

- 每个持仓**恰好一笔**止盈卖单；穿价护栏 `max(ceil_to_tick(cost), best_bid+tick)`。
- 主路径永远优先 get_trades 加权成本，avgPrice 仅最后兜底（门控：get_trades 0 笔且 avgPrice>0）。
- 止盈跑在 Step1 之后；停引擎仍只撤买单；水位线/黑名单等不变。
- 不改 `select_new_buy_fills`（Step1 成交检测）。
