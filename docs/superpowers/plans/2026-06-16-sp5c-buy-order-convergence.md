# SP5c 买单撤改收敛 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 v4 §6 让多档买单撤改收敛:每轮把某 token 的现挂买单收敛到当前目标多档梯——价漂移/量不符则撤旧挂新,价量都符则保持;预算口径回归全额。

**Architecture:** 纯函数 `reconcile_buy_orders(ladder, resting_buys)`(engine/laddering.py,全单测)算 (撤哪些, 挂哪些);`place_orders` 用它替换原「幂等跳过+只挂新」的下单循环,并把预算从「min(余额,敞口)−已挂敞口」改回「min(余额,敞口)」全额。卖单已由 `plan_take_profit` 收敛,不动。

**Tech Stack:** Python 3.12 / pytest(临时库、MagicMock 桩 API)。

**执行顺序:** T1 纯函数(附加,绿)→ T2 place_orders 接线+预算全额(自带测试更新)。

---

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `engine/laddering.py` | 加 `reconcile_buy_orders` 纯函数 | 修改 |
| `engine/manager.py` | `place_orders` 下单循环改撤改收敛 + 预算全额 | 修改 |
| `tests/test_laddering.py` | `reconcile_buy_orders` 单测 | 修改 |
| `tests/test_place_orders.py` | 撤改收敛/全额预算用例(改写 SP2 敞口扣减用例) | 修改 |

---

## Task 1: reconcile_buy_orders 纯函数

**Files:** Modify `engine/laddering.py`(末尾追加)。Test: `tests/test_laddering.py`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_laddering.py`(文件已 `from engine.laddering import ...`;另起 import 或在用到处 import):

```python
from engine.laddering import reconcile_buy_orders


def _ob_order(oid, price, size):
    return {"id": oid, "price": str(price), "original_size": str(size)}


def test_reconcile_empty_resting_places_all():
    cancel_ids, to_place = reconcile_buy_orders([(0.30, 100), (0.29, 100)], [])
    assert cancel_ids == []
    assert to_place == [(0.30, 100), (0.29, 100)]


def test_reconcile_keeps_matching_price_and_size():
    resting = [_ob_order("o1", 0.30, 100)]
    cancel_ids, to_place = reconcile_buy_orders([(0.30, 100), (0.29, 100)], resting)
    assert cancel_ids == []            # 0.30×100 保持
    assert to_place == [(0.29, 100)]   # 只挂缺的 0.29


def test_reconcile_cancels_price_drift():
    # 现挂 0.30,目标只剩 0.29 -> 撤 0.30、挂 0.29
    resting = [_ob_order("o1", 0.30, 100)]
    cancel_ids, to_place = reconcile_buy_orders([(0.29, 100)], resting)
    assert cancel_ids == ["o1"]
    assert to_place == [(0.29, 100)]


def test_reconcile_cancels_size_mismatch():
    # 同价但量不符 -> 撤改
    resting = [_ob_order("o1", 0.30, 100)]
    cancel_ids, to_place = reconcile_buy_orders([(0.30, 200)], resting)
    assert cancel_ids == ["o1"]
    assert to_place == [(0.30, 200)]


def test_reconcile_size_tolerance_keeps():
    # 量在容差内(max(1,1%))-> 保持
    resting = [_ob_order("o1", 0.30, 100)]
    cancel_ids, to_place = reconcile_buy_orders([(0.30, 100.5)], resting)
    assert cancel_ids == []
    assert to_place == []


def test_reconcile_empty_target_cancels_all():
    resting = [_ob_order("o1", 0.30, 100), _ob_order("o2", 0.29, 100)]
    cancel_ids, to_place = reconcile_buy_orders([], resting)
    assert set(cancel_ids) == {"o1", "o2"}
    assert to_place == []
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_laddering.py -k reconcile -v`
Expected: FAIL（`cannot import name 'reconcile_buy_orders'`）

- [ ] **Step 3: 实现**

在 `engine/laddering.py` 末尾追加:

```python
def reconcile_buy_orders(ladder, resting_buys):
    """撤改收敛(v4 §6):把某 token 的现挂买单收敛到目标 ladder。

    ladder: [(price, shares), ...] 该 token 的目标多档。
    resting_buys: [{"id","price","original_size"/"size"}, ...] 该 token 当前在挂买单。
    返回 (cancel_ids, to_place):
      - 现挂单价不在目标、或同价但量不符(容差 max(1,1%)) -> 撤(进 cancel_ids)。
      - 价量都符 -> 保持(不撤不挂)。
      - 目标档里没被任何现挂单保持到的价 -> 挂(进 to_place)。
    """
    target = {round(float(p), 4): s for (p, s) in ladder}
    keep = set()
    cancel_ids = []
    for o in resting_buys:
        op = round(float(o.get("price", 0) or 0), 4)
        osize = float(o.get("original_size", o.get("size", 0)) or 0)
        tgt = target.get(op)
        if tgt is not None and abs(osize - tgt) <= max(1.0, 0.01 * tgt):
            keep.add(op)
        else:
            oid = o.get("id")
            if oid is not None:
                cancel_ids.append(oid)
    to_place = [(p, s) for (p, s) in ladder if round(float(p), 4) not in keep]
    return cancel_ids, to_place
```

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_laddering.py -k reconcile -v`
Expected: PASS（6 个）

- [ ] **Step 5: Commit（不 stage .claude/settings.local.json）**

```bash
git add engine/laddering.py tests/test_laddering.py
git commit -m "feat(laddering): reconcile_buy_orders 撤改收敛纯函数(撤/保持/挂)"
```

---

## Task 2: place_orders 撤改收敛接线 + 预算全额

**Files:** Modify `engine/manager.py`(`place_orders`)。Modify `tests/test_place_orders.py`。

- [ ] **Step 1: 改/加测试**

在 `tests/test_place_orders.py`:

(a) 把 SP2 的 `test_existing_exposure_on_market_reduces_budget` 整个函数替换为（语义已变:全额预算 + 撤改收敛撤掉漂移的旧单）:

```python
def test_full_budget_and_reconcile_cancels_stale_buy():
    # 全额预算(不再扣已挂敞口)+ 撤改收敛:旧价档(0.20,不在新目标)被撤,目标 0.30 挂上。
    worker, api, db = _make_worker(
        template={
            "max_exposure_usd": 50,
            "tier_rules": [
                [{"upper": None, "action": {"type": "fixed_shares", "shares": 200}}]
                for _ in range(6)
            ],
        }
    )
    api.get_open_orders.return_value = [
        {"side": "BUY", "market": "A", "asset_id": "A-y", "price": "0.20",
         "original_size": "100", "id": "o-old"}
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    # 全额预算 50:fixed_shares 200,cap 50/0.30=166 -> 挂 0.30×166(非旧的 100)
    placed = {round(c.args[1], 2): c.args[2] for c in api.place_limit_buy.call_args_list}
    assert placed.get(0.30) == 166
    # 旧的 0.20 档(不在目标)被撤
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-old" in cancelled
```

(b) 追加一条「书移后撤旧档挂新档」用例:

```python
def test_book_shift_cancels_old_tier_and_places_new():
    worker, api, db = _make_worker()
    # 现挂 A-y @ 0.31(旧档);书移后目标只在 0.30 -> 撤 0.31、挂 0.30
    api.get_open_orders.return_value = [
        {"side": "BUY", "market": "A", "asset_id": "A-y", "price": "0.31",
         "original_size": "100", "id": "o-stale"}
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.32, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-stale" in cancelled
    placed = {round(c.args[1], 2) for c in api.place_limit_buy.call_args_list}
    assert 0.30 in placed and 0.31 not in placed
```

(c) `test_idempotent_skips_existing_price` 保留(现由 reconcile 实现「保持匹配价、挂缺失价」,断言不变:0.30 保持不重挂、0.29 挂上)。其余 place_orders 用例不变。

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_place_orders.py::test_full_budget_and_reconcile_cancels_stale_buy tests/test_place_orders.py::test_book_shift_cancels_old_tier_and_places_new -v`
Expected: FAIL（当前是幂等+扣敞口,不撤漂移旧单、预算被扣）

- [ ] **Step 3: 改 place_orders**

在 `engine/manager.py`:

(i) 顶部该方法内的 import 行(`from engine.laddering import compute_market_ladders, apply_double_sided_floor`)加上 `reconcile_buy_orders`:
```python
        from engine.laddering import (
            compute_market_ladders,
            apply_double_sided_floor,
            reconcile_buy_orders,
        )
```

(ii) 把现挂买单的预处理块(从 `buy_orders = [o for o in open_orders ...]` 到那段 `for o in buy_orders:` 累计 `exposure_usd/exposure_shares/open_price_keys/markets_with_open` 结束)替换为按 token 分组 + 并发集:
```python
        buy_orders = [o for o in open_orders if o.get("side") == "BUY"]
        buys_by_token, markets_with_open = {}, set()
        for o in buy_orders:
            buys_by_token.setdefault(o.get("asset_id", ""), []).append(o)
            mkt = o.get("market", "")
            if mkt:
                markets_with_open.add(mkt)
```

(iii) 预算两行改全额:
```python
            budget = min(balance, max_exposure_usd)
            shares_budget = max_exposure_shares
```
（删掉 `- exposure_usd.get(mid, 0.0)` 与 `- exposure_shares.get(mid, 0)`。）

(iv) 把下单循环(`for key, side in (("a", side_a), ("b", side_b)):` 那整段「幂等跳过 + place_limit_buy」)替换为撤改收敛:
```python
            for key, side in (("a", side_a), ("b", side_b)):
                if side is None:
                    continue
                token_id = side["token_id"]
                ladder = ladders.get(key, [])
                resting = buys_by_token.get(token_id, [])
                cancel_ids, to_place = reconcile_buy_orders(ladder, resting)
                if cancel_ids:
                    try:
                        self.api.cancel_orders(cancel_ids)
                        self.db.record_action(
                            wallet=self.wallet_address, market_id=mid,
                            action_type="buy_reconcile_cancel", side="-",
                            price=-1, size=0,
                            reason="撤改收敛:撤掉价漂移/量不符的旧买单(目标多档梯已变)",
                            price_basis=f"撤 {len(cancel_ids)} 笔 BUY；来源：CLOB get_open_orders",
                        )
                    except Exception as ex:
                        logger.warning("Reconcile cancel %s failed: %s", token_id, ex)
                for price, shares in to_place:
                    try:
                        self.api.place_limit_buy(
                            token_id, price, shares,
                            tick_size=side["tick_size_str"], neg_risk=side["neg_risk"],
                        )
                        placed += 1
                        markets_with_open.add(mid)
                        self._record_place_buy_tier(mid, side, price, shares)
                        if limit is not None and placed >= limit:
                            return
                    except Exception as ex:
                        logger.error("place_limit_buy failed %s: %s", token_id, ex)
```

> `record_action` 的参数名以 `engine/manager.py` 内既有调用为准（`_record_place_buy_tier` 用的是 `wallet=/market_id=/action_type=/side=/price=/size=/reason=/price_basis=`）。若 db 是 MagicMock(测试),`record_action` 调用不影响断言。

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_place_orders.py -v`
Expected: PASS（新增 2 + 既有用例;既有 `test_idempotent_skips_existing_price` 现由 reconcile 满足:同价同量保持、缺失价挂上）

- [ ] **Step 5: 全套测试**

Run: `python -m pytest -q`
Expected: ALL PASS。若 `test_manager.py` 有依赖旧 `place_orders` 幂等/扣敞口细节的用例,按新行为最小调整。

- [ ] **Step 6: Commit**

```bash
git add engine/manager.py tests/test_place_orders.py
git commit -m "feat(manager): place_orders 撤改收敛(reconcile_buy_orders)+ 预算回归全额"
```

---

## 验收 checkpoint（对应 spec §五）

1. 书移致目标梯变化:旧价档撤、新价档挂（`test_book_shift_cancels_old_tier_and_places_new` + `reconcile_buy_orders` 单测）。
2. 同价量不符→撤改;价量都符→保持不 churn（`reconcile_buy_orders` 单测 + `test_idempotent_skips_existing_price`）。
3. 预算全额(min(余额,敞口),不再减已挂)（`test_full_budget_and_reconcile_cancels_stale_buy`）。
4. 卖单行为不变（未触 check_exit / plan_take_profit）。
5. `pytest -q` 全绿。

## 范围之外

SP5a 三档节奏 + 观察名单 + 跌出 eligible 市场整仓撤单(实时看守) · SP5b 成交后单侧暂停 · SP6 模板 UI。
