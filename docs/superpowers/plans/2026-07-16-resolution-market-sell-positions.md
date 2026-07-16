# 结果提交即市价清仓 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** UMA 结算守卫加持仓侧——市场 `umaResolutionStatus` 变非空(结果已提交)时,把该市场全部持仓无视盈亏市价清仓。

**Architecture:** 折进 `engine/monitor.py` 的 `check_exit`:顶部对持仓所在市场批量取 `gamma_resolution_status`,算出 `resolving` 集合,逐持仓把「是否在结算市场」传入 `_exit_position`;为真时跳过 `plan_exit`,走新助手 `_resolution_dump`(撤该 asset 全部挂卖单 → `place_market_sell` → 记账),复用现有市价卖机器。`check_resolution`(买单侧)与 `plan_exit` 不动。

**Tech Stack:** Python 3、pytest、`unittest.mock`(纯逻辑单测,不触网)。

## Global Constraints

- **fail-open**:`gamma_resolution_status` 返回 `{}`(Gamma 抖动)时 `resolving` 为空,全部持仓走原离场,绝不因一次接口失败误清全仓。
- **成本未知也照卖**:正常离场在成本重建失败时「跳过 + ⚠️裸奔」;结算清仓**不 skip**——成本已知记 pnl,未知只记成交价/理由、不写 `record_trade`。
- **成交价绝不用 Data API 现价**:用 `market_fill_price(resp, best_bid, cur)`(现价常与盘口背离,会把亏损显示成盈利,2026-06-24 教训)。
- **trades 记账复用 `side="stop_loss"`**(成本已知时 `pnl=(fill-cost)*size`);本次不加独立「结算清仓」side 标签(YAGNI)。
- **UI/状态行/理由文案一律简体中文**。
- **常开、无 config key**(与现有 UMA 结算守卫一致)。
- **版本**:此改动在结算市场推翻「永不低于成本」,属行为改变 → 发版走 **MAJOR**(`v6.0.2` → `v7.0.0`)。版本 bump 与 `release.ps1` 由用户在决定发版时单独执行,**不属本计划的 subagent 执行范围**。

---

## File Structure

- `engine/monitor.py`(改):`check_exit` 顶部加 `resolving` 集合并逐持仓传布尔;`_exit_position` 加参数 `in_resolution_market` 并在成本计算后分叉;新增私有方法 `_resolution_dump`。触发判定复用已导入的 `engine.resolution.in_resolution`,不新增导入。
- `tests/test_monitor.py`(改):`_make_monitor` 补 `gamma_resolution_status` 默认 `{}`;新增测试类 `TestResolutionExit`。
- `CLAUDE.md`(改):「Critical behaviors to preserve」的 UMA 守卫段补「持仓侧结算清仓」。

`plan_exit` / `engine/take_profit.py` / `check_resolution` / `place_orders` **不动**。

---

## Task 1: 结算触发的持仓市价清仓(核心)

**Files:**
- Modify: `engine/monitor.py`(`check_exit` 约 330-365;`_exit_position` 约 367-618;新增 `_resolution_dump`)
- Test: `tests/test_monitor.py`(`_make_monitor` 约 10-29;新增类 `TestResolutionExit`)

**Interfaces:**
- Consumes:
  - `self.api.gamma_resolution_status(condition_ids: list) -> dict`(静态方法,`{cid: status 或 None}`,失败返回 `{}`)。
  - `engine.resolution.in_resolution(uma_status) -> bool`(已在 `monitor.py` 顶部导入)。
  - `self._sell_book(asset_id) -> (tick_float, tick_str, best_bid, best_ask)`。
  - `market_fill_price(resp, best_bid, cur)`、`describe_cost_basis(cost, lots)`(已导入)。
  - `self.api.place_market_sell(asset_id, size)`、`self.api.cancel_orders(ids)`、`self.db.record_trade(...)`。
- Produces:
  - `_exit_position(..., in_resolution_market=False)` 新增末位关键字参数。
  - `_resolution_dump(self, cid, asset_id, size, cur, cost, lots, open_orders) -> None`。

- [ ] **Step 1: 给测试夹具补 Gamma 默认值(否则裸 Mock 冲垮现有离场用例)**

`tests/test_monitor.py` 的 `_make_monitor`(约 25-27 行,`api.get_open_orders.return_value = []` 附近)加一行:

```python
    # sane default so methods that read get_trades don't iterate a MagicMock
    api.get_trades.return_value = []
    # 结算守卫默认:无市场在结算(否则裸 MagicMock 会被 in_resolution 判真,冲垮离场用例)
    api.gamma_resolution_status.return_value = {}
    # Step 1 只撤仍在挂的单;默认无在挂单(测试按需覆盖)
    api.get_open_orders.return_value = []
```

- [ ] **Step 2: 写失败测试(新类 `TestResolutionExit`,加在 `tests/test_monitor.py` 末尾)**

复用同文件已有的离场夹具 `_setup`(位于离场测试类内,构造 conditionId="A"、asset="A-y" 的持仓,并把 `monitor._cost_lots` 打桩为固定成本)。为独立起见,`TestResolutionExit` 自带一份等价的最小 `_setup`:

```python
class TestResolutionExit:
    """结算(umaResolutionStatus 非空)时,该市场持仓无视盈亏市价清仓。"""

    def _setup(self, cost, size, bids, asks, sells=None, cur_price=None, gamma=None):
        monitor, api, db = _make_monitor(
            settings={
                "theta_loss_cents": 2,
                "theta_stop_cents": 5,
                "stop_loss_mode": "fixed",
                "stop_loss_percent": 20,
                "case_a_mode": "ask",
            }
        )
        api.get_user_positions.return_value = [
            {
                "asset": "A-y",
                "size": size,
                "curPrice": cur_price if cur_price is not None else (bids[0][0] if bids else 0),
                "conditionId": "A",
            }
        ]
        api.get_open_orders.return_value = sells or []
        api.get_orderbook.return_value = {
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
            "tick_size": "0.01",
        }
        api.gamma_resolution_status.return_value = gamma if gamma is not None else {}
        monitor._cost_lots = lambda a, s, c: (
            cost,
            [{"price": cost, "take": s, "ts": 0, "trade_id": "t"}],
        )
        return monitor, api, db

    def test_resolving_market_market_sells_not_rests(self):
        # 结果已提交 + 盈利持仓:仍市价清仓(不再挂 maker 卖单等价差)。
        monitor, api, db = self._setup(
            0.30, 100, [(0.31, 500)], [(0.33, 500)], gamma={"A": "proposed"}
        )
        monitor.check_exit()
        api.place_market_sell.assert_called_once_with("A-y", 100)
        api.place_limit_sell.assert_not_called()
        ats = [c.kwargs["action_type"] for c in db.record_action.call_args_list]
        assert "exit_market" in ats
        db.record_trade.assert_called_once()  # 成本已知 -> 记 pnl

    def test_resolving_cancels_resting_sell_before_market(self):
        # 已有挂卖单 -> 必须先撤再市价卖(否则挂卖单占用份额,市价卖没份额可卖)。
        sells = [{
            "id": "s1", "asset_id": "A-y", "side": "SELL",
            "price": "0.42", "original_size": "100", "size_matched": "0",
        }]
        calls = []
        monitor, api, db = self._setup(
            0.30, 100, [(0.31, 500)], [(0.33, 500)], sells=sells, gamma={"A": "proposed"}
        )
        api.cancel_orders.side_effect = lambda ids: calls.append(("cancel", ids))
        api.place_market_sell.side_effect = lambda a, s: calls.append(("market", a, s))
        monitor.check_exit()
        assert calls[0][0] == "cancel" and "s1" in calls[0][1]
        assert calls[1][0] == "market"

    def test_resolving_cost_unknown_still_market_sells(self):
        # 成本重建失败(get_trades 无买入成交):正常离场会「跳过·裸奔」,但结算市场必须照卖。
        monitor, api, db = self._setup(
            0.30, 100, [(0.06, 500)], [(0.20, 500)], gamma={"A": "proposed"}
        )
        monitor._cost_lots = lambda a, s, c: (None, [])
        rows = []
        monitor._status_add = lambda **kw: rows.append(kw)
        monitor.check_exit()
        api.place_market_sell.assert_called_once_with("A-y", 100)
        db.record_trade.assert_not_called()  # 成本未知 -> 不记 pnl
        assert any("结算" in (r.get("action") or "") for r in rows), rows
        assert not any("跳过" in (r.get("action") or "") for r in rows), rows

    def test_resolving_records_real_fill_not_curprice(self):
        # 记录价必须是真实成交价≈买一 0.06,绝不用抽风现价 0.13。
        monitor, api, db = self._setup(
            0.1133, 60, [(0.06, 500)], [(0.20, 500)], cur_price=0.13, gamma={"A": "proposed"}
        )
        monitor.check_exit()
        api.place_market_sell.assert_called_once()
        _, k = db.record_trade.call_args
        assert abs(k["price"] - 0.06) < 1e-9

    def test_gamma_empty_falls_back_to_normal_exit(self):
        # fail-open:Gamma 返回 {} -> 走原离场(盈利挂 maker 卖一,不市价)。
        monitor, api, db = self._setup(
            0.30, 100, [(0.31, 500)], [(0.33, 500)], gamma={}
        )
        monitor.check_exit()
        api.place_limit_sell.assert_called_once()
        api.place_market_sell.assert_not_called()

    def test_non_resolving_status_none_normal_exit(self):
        # umaResolutionStatus 为 None(正常交易)-> 原离场,不市价。
        monitor, api, db = self._setup(
            0.30, 100, [(0.31, 500)], [(0.33, 500)], gamma={"A": None}
        )
        monitor.check_exit()
        api.place_limit_sell.assert_called_once()
        api.place_market_sell.assert_not_called()

    def test_market_sell_rejection_emits_naked_warning(self):
        # 市价清仓被 CLOB 拒 -> 不静默裸奔:不抛,且留含「裸奔」的 ⚠️ 状态行。
        from api.polymarket_api import OrderRejected

        monitor, api, db = self._setup(
            0.30, 100, [(0.31, 500)], [(0.33, 500)], gamma={"A": "proposed"}
        )
        api.place_market_sell.side_effect = OrderRejected("市价卖单 被拒")
        rows = []
        monitor._status_add = lambda **kw: rows.append(kw)
        monitor.check_exit()  # 不应抛
        assert any("裸奔" in (r.get("action") or "") for r in rows), rows
```

- [ ] **Step 3: 跑测试确认失败**

Run: `pytest tests/test_monitor.py::TestResolutionExit -v`
Expected: FAIL——`place_market_sell` 未被调用(当前结算持仓仍走 `plan_exit` 挂 maker 卖单),多条断言失败。

- [ ] **Step 4: 改 `check_exit` 顶部——算出 `resolving` 并逐持仓传布尔**

`engine/monitor.py` `check_exit`,在 `positions` 取到之后、`open_orders` 取到之后、`for pos in positions:` 循环之前,插入 `resolving` 计算;并把布尔传进 `_exit_position`。改动后该段(约 342-365 行)为:

```python
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
        # 结算守卫(持仓侧):对持仓所在市场批量取 UMA 结算状态,结果已提交(非空)的市场,
        # 该持仓无视盈亏市价清仓。fail-open:Gamma 返回 {} -> resolving 空 -> 全部走原离场。
        cids = [
            pos.get("conditionId", "")
            for pos in positions
            if float(pos.get("size", 0) or 0) > 0
        ]
        status_map = self.api.gamma_resolution_status(cids)
        resolving = {c for c in cids if in_resolution(status_map.get(c))}
        for pos in positions:
            try:
                self._exit_position(
                    pos,
                    open_orders,
                    theta_loss,
                    stop_mode,
                    stop_percent,
                    stop_cents,
                    case_a_mode,
                    take_profit_mode,
                    in_resolution_market=pos.get("conditionId", "") in resolving,
                )
            except Exception as e:
                logger.error("Exit error on %s: %s", pos.get("asset"), e)
```

- [ ] **Step 5: 给 `_exit_position` 加参数 + 成本计算后分叉**

`engine/monitor.py` `_exit_position` 签名(约 367-377)末位加 `in_resolution_market=False`:

```python
    def _exit_position(
        self,
        pos,
        open_orders,
        theta_loss,
        stop_mode,
        stop_percent,
        stop_cents,
        case_a_mode,
        take_profit_mode="maker",
        in_resolution_market=False,
    ):
```

在 `cost, lots = self._cost_lots(asset_id, size, cid)`(约 384)之后、`if cost is None or cost <= 0:`(约 385,裸奔跳过)**之前**插入分叉。改动后该段为:

```python
        cost, lots = self._cost_lots(asset_id, size, cid)
        if in_resolution_market:
            # 结算清仓:结果已提交,无视盈亏、无视成本是否算得出,市价清掉该持仓。
            self._resolution_dump(cid, asset_id, size, cur, cost, lots, open_orders)
            return
        if cost is None or cost <= 0:
```

- [ ] **Step 6: 新增 `_resolution_dump` 助手(放在 `_exit_position` 之后、`check_sell_orders` 之前)**

```python
    def _resolution_dump(self, cid, asset_id, size, cur, cost, lots, open_orders):
        """结算清仓:市场结果已提交(umaResolutionStatus 非空)时,无视盈亏市价把该持仓清掉。

        与正常离场的关键差异:成本取不到也照卖——结算在即,裸奔不卖=等着被结算成 0,是最坏
        情况。先撤该 asset 全部挂卖单(否则挂卖单占用份额,市价卖没份额可卖),再市价卖。
        成交价用 market_fill_price(≈买一),绝不用 Data API 现价。
        """
        tick, tick_str, best_bid, best_ask = self._sell_book(asset_id)
        sells = [
            o
            for o in open_orders
            if o.get("asset_id") == asset_id and o.get("side") == "SELL"
        ]
        sell_ids = [o["id"] for o in sells]
        if sell_ids:
            try:
                self.api.cancel_orders(sell_ids)
                self._record_action(
                    market_id=cid,
                    action_type="exit_cancel_sell",
                    side="-",
                    price=-1,
                    size=size,
                    reason="结算清仓:先撤全部挂卖单以便市价清仓",
                    price_basis=f"撤 {len(sell_ids)} 笔 SELL",
                )
            except Exception as e:
                # 撤单失败仍继续清仓:结算离场优先保证出手(超卖不会发生,没有的份额卖不出)。
                logger.warning(
                    "Cancel sells %s failed (proceed to resolution dump): %s",
                    asset_id,
                    e,
                )
        try:
            resp = self.api.place_market_sell(asset_id, size)
        except Exception as e:
            logger.error(
                "Resolution dump failed asset=%s: %s — UNPROTECTED", asset_id, e
            )
            self._status_add(
                market=cid,
                side="卖出",
                price=f"{cur:.4f}",
                size=str(size),
                matched="-",
                stage="离场",
                action="⚠️结算清仓失败·裸奔",
                detail=f"市场结果已提交，市价清仓被拒，该持仓未受保护：{e}",
            )
            return
        fill = market_fill_price(resp, best_bid, cur)
        if cost is not None and cost > 0:
            self.db.record_trade(
                wallet=self.wallet_address,
                market_id=cid,
                market_name="",
                side="stop_loss",
                price=fill,
                size=size,
                pnl=(fill - cost) * size,
            )
            basis = describe_cost_basis(cost, lots)
            price_basis = (
                f"{basis}；结算市价清仓·成交≈买一{fill:.4f}（精确成交以链上为准）；"
                f"来源：CLOB get_trades+get_orderbook"
            )
            detail = f"成本{cost:.4f} 成交≈{fill:.4f}"
        else:
            price_basis = (
                f"成本未知（get_trades 无买入成交）；结算市价清仓·"
                f"成交≈买一{fill:.4f}（精确成交以链上为准）"
            )
            detail = f"成本未知 成交≈{fill:.4f}"
        self._record_action(
            market_id=cid,
            action_type="exit_market",
            side="卖出",
            price=fill,
            size=size,
            reason="市场结果已提交/进入 UMA 结算 → 市价清仓（无视盈亏）",
            price_basis=price_basis,
        )
        self._status_add(
            market=cid,
            side="卖出",
            price=f"{fill:.4f}",
            size=str(size),
            matched="-",
            stage="离场",
            action="⚠️结算·市价清仓",
            detail=detail,
        )
```

- [ ] **Step 7: 跑新测试确认通过**

Run: `pytest tests/test_monitor.py::TestResolutionExit -v`
Expected: PASS(7 项全绿)。

- [ ] **Step 8: 跑全量确认零回归(尤其现有离场/`check_resolution` 用例)**

Run: `pytest -q`
Expected: PASS,全绿(现有 `check_exit` 用例因夹具默认 `gamma_resolution_status={}` 仍走原离场)。

- [ ] **Step 9: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "$(cat <<'EOF'
feat(exit): 结果提交即市价清仓——UMA 结算守卫加持仓侧

check_exit 顶部批量取持仓所在市场的 umaResolutionStatus,结果已提交(非空)的
市场,该持仓无视盈亏走新助手 _resolution_dump 市价清仓(撤挂卖单→place_market_sell
→记账);复用现有市价卖机器。成本未知也照卖(结算在即,裸奔=被结算成 0);成交价用
market_fill_price 绝不用 Data API 现价。fail-open:Gamma {} 时全部走原离场。
_make_monitor 夹具补 gamma 默认 {} 防裸 Mock 冲垮离场用例。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 文档——CLAUDE.md 记入持仓侧结算清仓

**Files:**
- Modify: `CLAUDE.md`(「Critical behaviors to preserve」的 UMA 结算守卫段)

**Interfaces:**
- Consumes: 无(纯文档)。
- Produces: 无(纯文档)。

- [ ] **Step 1: 在 CLAUDE.md 的「UMA resolution guard cancels buys in resolving markets.」段末尾补一句**

定位该段结尾原句「Positions and resting SELLs are untouched (`check_exit` still exits before settlement). Always-on, no config key.」,改为:

```
Positions are now **market-sold on resolution**: `check_exit` batch-fetches the
`umaResolutionStatus` of the condition_ids it holds positions in and, for any
market in resolution, routes that position through `_resolution_dump` — cancel
the resting SELL, then `place_market_sell` regardless of P/L, **overriding the
"never sell below cost" invariant in resolving markets** (a maker sell parked at
cost may never fill before settlement, leaving a losing side to settle to 0).
The dump sells **even when cost can't be reconstructed** (normal exit would skip
with ⚠️裸奔; here skipping means settling to 0), records the fill via
`market_fill_price` (never Data API curPrice), and reuses the `stop_loss` trade
row (pnl only when cost known). Fail-open: Gamma `{}` → nothing is in resolution
→ all positions take the normal exit. Always-on, no config key.
```

- [ ] **Step 2: 提交**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs(claude): 记入 UMA 结算守卫的持仓侧市价清仓

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## 发版(用户自行执行,非 subagent 范围)

代码合并后,决定发版时:改 `version.py` 为 `7.0.0`(MAJOR,结算市场推翻「永不低于成本」属行为改变),从 **Bash** 跑 `powershell -File release.ps1`(build+tag+publish);公告须写明新行为:「市场结果一提交(UMA 有人提交裁决)即把该市场全部持仓市价清仓,无视盈亏」。参见 `docs/版本号规范.md` 与自动更新流程的既有约定。

---

## Self-Review

**1. Spec coverage**（逐节对照 spec）:
- 触发点复用 `in_resolution` → Task 1 Step 4。✓
- 落点方案 B(折进 `check_exit`,`check_resolution` 不动)→ Task 1 Step 4/5/6。✓
- `resolving` 集合 + fail-open → Task 1 Step 4 + `test_gamma_empty_falls_back_to_normal_exit`。✓
- `_exit_position` 新增 `in_resolution_market` + 成本后分叉 → Step 5。✓
- 无视盈亏市价卖 + 先撤挂卖单 → Step 6 + `test_resolving_*` / `test_resolving_cancels_resting_sell_before_market`。✓
- 成本未知也照卖(不裸奔跳过)→ Step 6 else 分支 + `test_resolving_cost_unknown_still_market_sells`。✓
- 成交价 `market_fill_price` 不用现价 → Step 6 + `test_resolving_records_real_fill_not_curprice`。✓
- 记账复用 `stop_loss` + 已知副作用 → Step 6(成本已知记 `record_trade`)。✓
- 被拒不静默裸奔 → Step 6 except + `test_market_sell_rejection_emits_naked_warning`。✓
- 夹具默认 `{}` → Step 1。✓
- 常开无 config → 无新增配置键。✓
- 版本 MAJOR → 发版段(用户执行)。✓
- CLAUDE.md 文档 → Task 2。✓

**2. Placeholder scan**:无 TBD/TODO/「同上」;每个代码步骤给出完整可照抄代码。✓

**3. Type consistency**:`_resolution_dump(self, cid, asset_id, size, cur, cost, lots, open_orders)` 的调用点(Step 5)与定义(Step 6)参数顺序/个数一致;`_exit_position` 新参数 `in_resolution_market` 在签名(Step 5)与调用(Step 4)一致;`gamma_resolution_status`/`market_fill_price`/`describe_cost_basis`/`_sell_book` 签名与既有代码一致。✓
