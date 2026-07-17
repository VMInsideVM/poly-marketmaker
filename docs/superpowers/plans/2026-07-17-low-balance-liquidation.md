# 低余额清仓 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development 或 executing-plans。Steps 用 `- [ ]`。

**Goal:** 余额 < 阈值(默认4u)时按优先级逐笔市价卖持仓腾现金:档1低奖励市场(<30u)、档2小仓(<20份)、档3按亏损从小到大;卖到「停手目标」停。

**Architecture:** 纯函数 `engine/liquidation.py plan_liquidation`(定卖出顺序)+ monitor `check_low_balance`(编排:查余额→组装候选→逐笔市价卖到目标停)+ 复用/抽出的 `_market_dump`(撤挂卖+市价卖+记账,resolution 与清仓共用)+ 模板级配置 + config 页表单。

**Tech Stack:** Python、pytest;前端 Jinja 内联 JS(主会话手改)。

## Global Constraints

- **覆盖「卖单永不低于成本」**(主动清仓腾现金,同结算清仓);市价卖 FAK;成交价用 `market_fill_price`(禁 Data API 现价);成本只走 `get_trades`(`_cost_lots`,禁 avgPrice/curPrice)。
- **估算余额防过卖**:初始 `get_balance()` 为基,每卖一笔加 `best_bid×份额`,`估算≥目标`即停(不逐笔重查真实余额)。
- 无买盘(best_bid 空/≤0)→ 跳过该笔;成本未知 → 仍照卖、不记 pnl、亏损排档末。
- 阈值/奖励线/份额/目标模式/目标U **模板级**(`TEMPLATE_DEFAULTS`),配置页可调,默认 4/30/20/"balance"/4;`low_balance_threshold_usd=0` = 关闭。
- 落点:monitor `check_low_balance` 插入 `_tick`,在 `check_exit` **之前**;`余额≥阈值`秒退;整体 try/except 不阻断其余步骤。
- `_market_dump` 抽取只重构 `_resolution_dump`(TestResolutionExit 兜底),**不动 B0 分支**。

---

## Task 1: 配置键 + 两个 DB 查询

**Files:** Modify `config.py`(TEMPLATE_DEFAULTS)、`models/database.py`;Test `tests/test_database.py`(追加)。

**Interfaces:** `db.get_market_daily_reward(condition_id) -> float|None`；`db.get_min_order_cost() -> float|None`。

- [ ] **Step 1: 失败测试**(`tests/test_database.py` 追加):

```python
class TestLiquidationQueries:
    def _seed_eligible(self, db, market_id, daily_reward, min_cost):
        c = db.conn.cursor()
        c.execute(
            "INSERT INTO eligible_markets (market_id, token_id, market_name, outcome,"
            " daily_reward, order_price, min_cost) VALUES (?,?,?,?,?,?,?)",
            (market_id, "tok", "n", "Yes", daily_reward, 0.1, min_cost),
        )
        db.conn.commit()

    def test_get_market_daily_reward(self, db):
        self._seed_eligible(db, "0xC1", 25.0, 2.0)
        assert db.get_market_daily_reward("0xC1") == 25.0
        assert db.get_market_daily_reward("0xNONE") is None

    def test_get_min_order_cost(self, db):
        assert db.get_min_order_cost() is None  # 空表
        self._seed_eligible(db, "0xC1", 25.0, 5.0)
        self._seed_eligible(db, "0xC2", 40.0, 2.0)
        assert db.get_min_order_cost() == 2.0
```

- [ ] **Step 2:** `pytest tests/test_database.py::TestLiquidationQueries -v` → FAIL。
- [ ] **Step 3: `config.py` TEMPLATE_DEFAULTS 加键**(`max_concurrent_markets` 之后):

```python
    # 低余额清仓:余额 < low_balance_threshold_usd(0=关)时按优先级逐笔市价卖持仓腾现金。
    "low_balance_threshold_usd": 4.0,
    "low_reward_threshold_usd": 30.0,
    "small_position_shares": 20.0,
    "liquidate_target_mode": "balance",  # "balance" | "next_order"
    "liquidate_target_usd": 4.0,
```

- [ ] **Step 4: `models/database.py` 加两方法**(`get_min_order_cost` 之类查询,放 eligible 相关方法附近或 record_net_worth 前):

```python
    def get_market_daily_reward(self, condition_id):
        """某市场在 eligible_markets 里的 daily_reward(市场奖励);不在表 -> None。"""
        c = self.conn.cursor()
        c.execute(
            "SELECT daily_reward FROM eligible_markets WHERE market_id = ? LIMIT 1",
            (condition_id,),
        )
        row = c.fetchone()
        return float(row["daily_reward"]) if row else None

    def get_min_order_cost(self):
        """当前 eligible_markets 里最便宜一单的 min_cost(能挂得起的最小本金);空表 -> None。"""
        c = self.conn.cursor()
        c.execute("SELECT MIN(min_cost) AS m FROM eligible_markets")
        row = c.fetchone()
        return float(row["m"]) if row and row["m"] is not None else None
```

- [ ] **Step 5-6:** 测试通过 + `pytest -q` 全绿;commit（`feat(db): 低余额清仓配置键 + daily_reward/min_order_cost 查询`）。

---

## Task 2: 纯函数 `engine/liquidation.py plan_liquidation`

**Files:** Create `engine/liquidation.py`;Test `tests/test_liquidation.py`(新)。

**Interfaces:** `plan_liquidation(candidates, low_reward_threshold, small_shares_threshold) -> list[str]`（返回按优先级排序的 asset_id 列表）。

- [ ] **Step 1: 失败测试** `tests/test_liquidation.py`：

```python
from engine.liquidation import plan_liquidation


def _c(a, size, reward, loss):
    return {"asset_id": a, "size": size, "daily_reward": reward, "loss": loss}


def test_tier1_low_reward_first_by_loss():
    # 档1=daily_reward<30;档内按 loss 升序
    cands = [
        _c("A", 100, 10, 5.0),   # 档1 loss5
        _c("B", 100, 10, 1.0),   # 档1 loss1
        _c("C", 100, 50, 2.0),   # 高奖励 -> 档3
    ]
    assert plan_liquidation(cands, 30, 20) == ["B", "A", "C"]


def test_tier2_small_position():
    cands = [
        _c("A", 5, 50, 3.0),     # 高奖励但份额<20 -> 档2
        _c("B", 100, 50, 1.0),   # 高奖励大仓 -> 档3
    ]
    assert plan_liquidation(cands, 30, 20) == ["A", "B"]


def test_tier1_beats_tier2():
    cands = [
        _c("A", 5, 50, 9.0),     # 高奖励小仓 -> 档2
        _c("B", 100, 10, 9.0),   # 低奖励大仓 -> 档1(优先)
    ]
    assert plan_liquidation(cands, 30, 20) == ["B", "A"]


def test_tier3_by_loss_profit_first():
    # 档3 按 loss 升序:盈利(负 loss)排最前
    cands = [
        _c("A", 100, 50, 3.0),
        _c("B", 100, 50, -2.0),  # 盈利
        _c("C", 100, 50, 0.0),
    ]
    assert plan_liquidation(cands, 30, 20) == ["B", "C", "A"]


def test_cost_unknown_sorts_to_tier_end():
    # loss=None(成本未知)排所在档末尾
    cands = [
        _c("A", 100, 10, None),  # 档1 但 loss 未知 -> 档1末
        _c("B", 100, 10, 5.0),   # 档1 loss5
    ]
    assert plan_liquidation(cands, 30, 20) == ["B", "A"]


def test_empty():
    assert plan_liquidation([], 30, 20) == []
```

- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 实现** `engine/liquidation.py`：

```python
"""engine/liquidation.py — 低余额清仓的卖出优先级(纯函数,无 IO)。"""

import math


def plan_liquidation(candidates, low_reward_threshold, small_shares_threshold):
    """返回按优先级排序的 asset_id 列表:档1(低奖励市场)→档2(小仓)→档3(其余),
    每档内按 loss 升序(loss=None 视为 +∞ 排档末)。

    candidates: [{asset_id, size, daily_reward(或 None), loss(或 None)}]。
    """
    tier1, tier2, tier3 = [], [], []
    for c in candidates:
        reward = c.get("daily_reward")
        size = float(c.get("size", 0) or 0)
        if reward is not None and reward < low_reward_threshold:
            tier1.append(c)
        elif size < small_shares_threshold:
            tier2.append(c)
        else:
            tier3.append(c)

    def _key(c):
        loss = c.get("loss")
        return math.inf if loss is None else loss

    ordered = []
    for tier in (tier1, tier2, tier3):
        ordered += [c["asset_id"] for c in sorted(tier, key=_key)]
    return ordered
```

- [ ] **Step 4-6:** 测试通过 + `pytest -q` + commit（`feat(liquidation): plan_liquidation 三档优先级纯函数`）。

---

## Task 3: `_market_dump` 抽取 + `check_low_balance` 编排 + 接入 `_tick`

**Files:** Modify `engine/monitor.py`、`engine/manager.py`(`_tick` 加步骤);Test `tests/test_monitor.py`(追加 `TestLowBalance`)。

**Interfaces:** `_market_dump(self, cid, asset_id, size, cur, best_bid, cost, lots, open_orders, tag, reason) -> float|None`(撤挂卖+市价卖+记账;成功返 fill、被拒返 None);`check_low_balance(self)`。

- [ ] **Step 1: 失败测试**（`tests/test_monitor.py` 追加）:

```python
class TestLowBalance:
    def _mon(self, balance, positions, daily_rewards, costs, bids, threshold=4, target_usd=4, mode="balance"):
        monitor, api, db = _make_monitor(settings={
            "low_balance_threshold_usd": threshold,
            "low_reward_threshold_usd": 30,
            "small_position_shares": 20,
            "liquidate_target_mode": mode,
            "liquidate_target_usd": target_usd,
        })
        api.get_balance.return_value = balance
        api.get_user_positions.return_value = positions
        api.get_open_orders.return_value = []
        db.get_market_daily_reward.side_effect = lambda cid: daily_rewards.get(cid)
        db.get_min_order_cost.return_value = 2.0
        monitor._cost_lots = lambda a, s, c: (costs.get(a), [{"price": costs.get(a) or 0, "take": s, "ts": 0, "trade_id": "t"}])
        monitor._sell_book = lambda a: (0.01, "0.01", bids.get(a), (bids.get(a) or 0) + 0.02)
        return monitor, api, db

    def _pos(self, asset, size, cid, cur=0.1):
        return {"asset": asset, "size": size, "conditionId": cid, "curPrice": cur}

    def test_no_action_when_balance_ok(self):
        m, api, db = self._mon(10, [self._pos("A", 100, "cA")], {"cA": 10}, {"A": 0.1}, {"A": 0.1})
        m.check_low_balance()
        api.place_market_sell.assert_not_called()

    def test_sells_low_reward_first_until_target(self):
        # 余额2<4;卖档1(低奖励 cA)到 est≥4 停。best_bid 0.05×100=5 到手 -> est 2+5=7≥4,只卖一笔。
        m, api, db = self._mon(
            2,
            [self._pos("A", 100, "cA"), self._pos("B", 100, "cB")],
            {"cA": 10, "cB": 50},  # A 低奖励(档1)、B 高奖励(档3)
            {"A": 0.1, "B": 0.1},
            {"A": 0.05, "B": 0.05},
        )
        m.check_low_balance()
        api.place_market_sell.assert_called_once_with("A", 100)

    def test_skips_no_bid(self):
        # 唯一候选无买盘 -> 跳过、不卖
        m, api, db = self._mon(2, [self._pos("A", 100, "cA")], {"cA": 10}, {"A": 0.1}, {"A": None})
        m.check_low_balance()
        api.place_market_sell.assert_not_called()

    def test_disabled_when_threshold_zero(self):
        m, api, db = self._mon(1, [self._pos("A", 100, "cA")], {"cA": 10}, {"A": 0.1}, {"A": 0.05}, threshold=0)
        m.check_low_balance()
        api.get_balance.assert_not_called()  # 阈值0秒退,连余额都不查
        api.place_market_sell.assert_not_called()
```

- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 抽出 `_market_dump`**（`engine/monitor.py`）:把 `_resolution_dump` 里「组装 sells → 撤挂卖 → place_market_sell → market_fill_price → record_trade/record_action/status」整段抽成新方法,`tag`/`reason` 参数化,成功返 `fill`、被拒返 `None`:

```python
    def _market_dump(self, cid, asset_id, size, cur, best_bid, cost, lots, open_orders, tag, reason):
        """市价清仓一个持仓(撤挂卖→FAK市价卖→记账),覆盖「永不低于成本」。tag=短标签
        (结算/低余额),reason=record_action 理由。成功返成交价 fill、被拒返 None。
        调用方须先确保 best_bid 有效(无买盘不进来)。"""
        sells = [
            o for o in open_orders
            if o.get("asset_id") == asset_id and o.get("side") == "SELL"
        ]
        sell_ids = [o["id"] for o in sells]
        if sell_ids:
            try:
                self.api.cancel_orders(sell_ids)
                self._record_action(
                    market_id=cid, action_type="exit_cancel_sell", side="-", price=-1,
                    size=size, reason=f"{tag}清仓:先撤全部挂卖单以便市价清仓",
                    price_basis=f"撤 {len(sell_ids)} 笔 SELL",
                )
            except Exception as e:
                logger.warning("Cancel sells %s failed (proceed to %s dump): %s", asset_id, tag, e)
        try:
            resp = self.api.place_market_sell(asset_id, size)
        except Exception as e:
            logger.error("%s dump failed asset=%s: %s", tag, asset_id, e)
            self._status_add(
                market=cid, side="卖出", price=f"{cur:.4f}", size=str(size), matched="-",
                stage="离场", action=f"⚠️{tag}清仓失败",
                detail=f"{tag}市价清仓被拒:{e}",
            )
            return None
        fill = market_fill_price(resp, best_bid, cur)
        if cost is not None and cost > 0:
            self.db.record_trade(
                wallet=self.wallet_address, market_id=cid, market_name="",
                side="stop_loss", price=fill, size=size, pnl=(fill - cost) * size,
            )
            basis = describe_cost_basis(cost, lots)
            price_basis = f"{basis}；{tag}市价清仓·成交≈买一{fill:.4f}（精确成交以链上为准）；来源：CLOB get_trades+get_orderbook"
            detail = f"成本{cost:.4f} 成交≈{fill:.4f}"
        else:
            price_basis = f"成本未知（get_trades 无买入成交）；{tag}市价清仓·成交≈买一{fill:.4f}（精确成交以链上为准）"
            detail = f"成本未知 成交≈{fill:.4f}"
        self._record_action(
            market_id=cid, action_type="exit_market", side="卖出", price=fill, size=size,
            reason=reason, price_basis=price_basis,
        )
        self._status_add(
            market=cid, side="卖出", price=f"{fill:.4f}", size=str(size), matched="-",
            stage="离场", action=f"⚠️{tag}·市价清仓", detail=detail,
        )
        return fill
```

- [ ] **Step 4: `_resolution_dump` 改为调用 `_market_dump`**（只保留 sell_book + 无买盘守卫,其余委托):

```python
    def _resolution_dump(self, cid, asset_id, size, cur, cost, lots, open_orders):
        """结算清仓:...(docstring 保留原意)"""
        _, _, best_bid, _ = self._sell_book(asset_id)
        if best_bid is None or best_bid <= 0:
            self._status_add(
                market=cid, side="卖出", price="-", size=str(size), matched="-",
                stage="离场", action="⚠️结算·无买盘暂未清仓",
                detail="市场结果已提交但盘口无买盘，市价卖不出，该持仓仍暴露，待有买盘再清",
            )
            return
        self._market_dump(
            cid, asset_id, size, cur, best_bid, cost, lots, open_orders,
            tag="结算", reason="市场结果已提交/进入 UMA 结算 → 市价清仓（无视盈亏）",
        )
```

（注:`TestResolutionExit` 须仍全绿——`test_sell_rejection_emits_naked_warning` 断言 action 含「裸奔」或「失败」,`⚠️结算清仓失败` 含「失败」✓;`test_resolving_no_bid` 断言含「无买盘」✓;其余断言 place_market_sell/record_trade 调用不变。)

- [ ] **Step 5: `check_low_balance`**（`engine/monitor.py`,`check_exit` 之前;顶部 import `from engine.liquidation import plan_liquidation`）:

```python
    def check_low_balance(self):
        """余额 < 阈值(0=关)时按优先级逐笔市价卖持仓腾现金,卖到「停手目标」停。
        覆盖「永不低于成本」(主动清仓)。用估算余额防过卖;无买盘跳过;失败不抛。"""
        tmpl = self.db.get_template_for(self.wallet_address)
        threshold = float(tmpl.get("low_balance_threshold_usd", 4) or 0)
        if threshold <= 0:
            return
        try:
            balance = float(self.api.get_balance() or 0)
        except Exception as e:
            logger.warning("get_balance failed (skip low-balance): %s", e)
            return
        if balance >= threshold:
            return
        low_reward = float(tmpl.get("low_reward_threshold_usd", 30))
        small_shares = float(tmpl.get("small_position_shares", 20))
        if tmpl.get("liquidate_target_mode", "balance") == "next_order":
            moc = self.db.get_min_order_cost()
            target = moc if moc else threshold
        else:
            target = float(tmpl.get("liquidate_target_usd", 4))
        try:
            positions = self.api.get_user_positions(self._funder())
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.warning("fetch failed (skip low-balance): %s", e)
            return
        meta, candidates = {}, []
        for pos in positions:
            asset_id = pos.get("asset", "")
            size = float(pos.get("size", 0) or 0)
            if size <= 0:
                continue
            cid = pos.get("conditionId", "")
            cur = float(pos.get("curPrice", 0) or 0)
            cost, lots = self._cost_lots(asset_id, size, cid)
            _, _, best_bid, _ = self._sell_book(asset_id)
            reward = self.db.get_market_daily_reward(cid)
            loss = None if (cost is None or best_bid is None) else (cost - best_bid) * size
            meta[asset_id] = {"cid": cid, "size": size, "cur": cur, "best_bid": best_bid, "cost": cost, "lots": lots}
            candidates.append({"asset_id": asset_id, "size": size, "daily_reward": reward, "loss": loss})
        est = balance
        for asset_id in plan_liquidation(candidates, low_reward, small_shares):
            if est >= target:
                break
            m = meta[asset_id]
            bb = m["best_bid"]
            if bb is None or bb <= 0:
                continue  # 无买盘,卖不出 -> 跳过(不计入腾现金)
            try:
                fill = self._market_dump(
                    m["cid"], asset_id, m["size"], m["cur"], bb, m["cost"], m["lots"],
                    open_orders, tag="低余额", reason="低余额清仓腾现金（无视盈亏）",
                )
            except Exception as e:
                logger.warning("low-balance dump %s failed: %s", asset_id, e)
                continue
            if fill is not None:
                est += bb * m["size"]  # 保守估算到手(best_bid×份额),防过卖
```

- [ ] **Step 6: 接入 `_tick`**（`engine/manager.py`,`check_resolution` 之后、`check_exit` 之前）:

```python
        self.monitor.check_buy_orders()
        self.monitor.check_resolution()
        self.monitor.check_low_balance()
        self.monitor.check_exit()
```

- [ ] **Step 7-9:** `pytest tests/test_monitor.py -v`(TestLowBalance + TestResolutionExit 全绿)+ `pytest -q` + commit（`feat(liquidation): 低余额清仓 check_low_balance + _market_dump 抽取复用`）。

---

## Task 4: config.html「低余额清仓」表单（主会话手改）

**Files:** Modify `web/templates/config.html`(strategy-form 内)。

- [ ] **Step 1:** strategy-form 内(止盈方式附近)加「低余额清仓」fieldset:number 输入 `name=low_balance_threshold_usd/low_reward_threshold_usd/small_position_shares/liquidate_target_usd` + select `name=liquidate_target_mode id=liquidate-target-mode`(选项 balance「卖到余额线」/ next_order「卖到够下一单」)。`loadStrategy` 已自动填(含 select 的 .value)。
- [ ] **Step 2:** strategy-form submit 收 select:在 `data.take_profit_mode = ...` 旁加 `data.liquidate_target_mode = (document.getElementById('liquidate-target-mode')||{}).value || 'balance';`(number 输入已被现有 `input[type=number]` 循环收)。
- [ ] **Step 3: 校验**:无 BOM、node 读模板、渲染 `/config` 200 + 关键标记(`low_balance_threshold_usd`/`liquidate-target-mode`)。
- [ ] **Step 4: 提交**（`feat(ui): 配置页低余额清仓表单`）。

---

## Self-Review

**Spec coverage**:三档优先级+亏损排序→T2 plan_liquidation;市价卖覆盖永不低于成本+复用→T3 _market_dump;估算防过卖+无买盘跳过+成本未知照卖→T3 check_low_balance;两种目标模式→T3;模板级配置→T1+T4;daily_reward/min_order_cost 来源→T1 db 查询;落点离场前→T3 Step6。✓
**Placeholder**:无 TBD;每步完整代码。
**Type consistency**:`plan_liquidation(candidates, low_reward, small_shares)` 定义(T2)与调用(T3)一致;`_market_dump(cid,asset_id,size,cur,best_bid,cost,lots,open_orders,tag,reason)` 定义(T3 Step3)与两调用点(resolution/低余额)一致;`get_market_daily_reward`/`get_min_order_cost`(T1)与 check_low_balance 调用一致;配置键名跨 config/monitor/前端一致。
