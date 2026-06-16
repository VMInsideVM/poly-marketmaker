# SP5b 成交后单侧暂停 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `place_orders` 的「有持仓 → 跳过整个市场」细化为「某侧有持仓 → 仅暂停那一侧的新买单并撤光该侧旧买单,另一侧用扣减后预算照常做市」(v4 §4.4/§7)。

**Architecture:** 新增纯函数 `held_side_info(positions)`(`engine/positions.py`)一次遍历持仓产出 `(held_assets, value_by_market, shares_by_market)`;`place_orders`(`engine/manager.py`)删市场级跳过,改按侧分流——暂停侧 `reconcile_buy_orders([], resting)` 全撤、活跃侧用 `min(余额,max_exposure_usd) − 本市场已持仓市值` 的预算正常多档收敛。信号是 Data API 持仓 `size>0`(自愈,`size→0` 自动恢复),不引入新模板参数。

**Tech Stack:** Python 3.12 / pytest(MagicMock 桩 API)。

**执行顺序:** T1 纯函数(附加,不碰调用方)→ T2 接线(用 T1 函数)。每提交都绿。基线:SP5c 合并后 `383 passed`。

---

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `engine/positions.py` | 加 `held_side_info` 纯函数(与 `held_condition_ids` 并列) | 修改 |
| `engine/manager.py` | `place_orders` 改用 `held_side_info` + 按侧暂停/撤单/预算扣减 | 修改 |
| `tests/test_positions.py` | `held_side_info` 单测 | 修改 |
| `tests/test_place_orders.py` | 单侧暂停 / 预算扣减 / 双侧持仓 集成测试 | 修改 |

`held_condition_ids` 保留不删(其自身单测仍在);T2 后 production 不再引用它,留作 SP6 死字段清理候选。

---

## Task 1: `held_side_info` 纯函数

**Files:**
- Modify: `engine/positions.py`(在 `held_condition_ids` 之后追加)
- Test: `tests/test_positions.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_positions.py` 顶部 import 行改为同时导入新函数:

```python
from engine.positions import held_condition_ids, held_side_info
```

在文件末尾追加四个测试:

```python
def test_held_side_info_held_assets_only_positive_size():
    pos = [
        {"conditionId": "c1", "asset": "yes", "size": 100.0, "curPrice": 0.4},
        {"conditionId": "c1", "asset": "no", "size": 0, "curPrice": 0.6},
    ]
    held_assets, value, shares = held_side_info(pos)
    assert held_assets == {"yes"}


def test_held_side_info_value_and_shares_aggregate_by_market():
    pos = [
        {"conditionId": "c1", "asset": "yes", "size": 100.0, "curPrice": 0.40},
        {"conditionId": "c1", "asset": "no", "size": 50.0, "curPrice": 0.60},
    ]
    held_assets, value, shares = held_side_info(pos)
    assert held_assets == {"yes", "no"}
    assert value["c1"] == 100.0 * 0.40 + 50.0 * 0.60  # 70.0
    assert shares["c1"] == 150.0


def test_held_side_info_skips_nonpositive_and_missing_fields():
    pos = [
        {"conditionId": "c1", "asset": "yes", "size": 0, "curPrice": 0.4},  # size<=0 skip
        {"asset": "x", "size": 100.0, "curPrice": 0.5},                     # 无 conditionId
        {"conditionId": "c2", "size": 100.0},                              # 无 asset/curPrice
        {"conditionId": "c3", "asset": "z", "size": None, "curPrice": 0.5},  # None size skip
    ]
    held_assets, value, shares = held_side_info(pos)
    assert "x" in held_assets        # size>0、有 asset -> 进集合(无 conditionId 不影响)
    assert "yes" not in held_assets  # size 0 -> 跳过
    assert "z" not in held_assets    # None size -> 跳过
    assert value.get("c2") == 0.0    # 无 curPrice -> 市值计 0
    assert shares.get("c2") == 100.0
    assert "c1" not in value         # size 0 整项跳过,无聚合


def test_held_side_info_empty():
    assert held_side_info([]) == (set(), {}, {})
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_positions.py -v`
Expected: FAIL（`ImportError: cannot import name 'held_side_info'`）

- [ ] **Step 3: 实现**

在 `engine/positions.py` 的 `held_condition_ids` 函数之后追加:

```python
def held_side_info(positions: list[dict]):
    """从 Data API /positions 提取按侧(asset)暂停信息 + 按市场已持仓敞口。

    返回 (held_assets, value_by_market, shares_by_market):
      held_assets:      {asset_id}  size>0 的 token(该侧暂停新买单)。
      value_by_market:  {conditionId: Σ size×curPrice}  已持仓市值(扣减另一侧预算)。
      shares_by_market: {conditionId: Σ size}            已持仓份数(扣减份数预算)。

    size<=0 的项整项忽略;缺 asset/conditionId 只跳过对应聚合。市值用 curPrice
    (持仓当前市值),绝不用 avgPrice。size/curPrice 做 None/字符串安全转换。
    """
    held_assets: set[str] = set()
    value_by_market: dict[str, float] = {}
    shares_by_market: dict[str, float] = {}
    for p in positions:
        size = float(p.get("size", 0) or 0)
        if size <= 0:
            continue
        asset = p.get("asset", "")
        if asset:
            held_assets.add(asset)
        cid = p.get("conditionId", "")
        if cid:
            cur = float(p.get("curPrice", 0) or 0)
            value_by_market[cid] = value_by_market.get(cid, 0.0) + size * cur
            shares_by_market[cid] = shares_by_market.get(cid, 0.0) + size
    return held_assets, value_by_market, shares_by_market
```

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_positions.py -v`
Expected: PASS（既有 6 + 新增 4 = 10 passed）

- [ ] **Step 5: Commit（不 stage `.claude/settings.local.json`）**

```bash
git add engine/positions.py tests/test_positions.py
git commit -m "feat(positions): held_side_info 按侧持仓信息(暂停侧/已持仓市值/份数)"
```

---

## Task 2: `place_orders` 按侧暂停接线

**Files:**
- Modify: `engine/manager.py`（import 行 15、`place_orders` 内 4 处）
- Test: `tests/test_place_orders.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_place_orders.py` 文件末尾追加三个测试（沿用文件已有的 `_make_worker` / `_ob` / `_elig` 辅助）:

```python
def test_paused_side_cancels_resting_and_other_side_runs():
    # 持有 YES(A-y) -> YES 侧暂停:撤光 YES 在挂买单、不挂 YES 新单;NO(A-n) 照常挂。
    worker, api, db = _make_worker()
    api.get_user_positions.return_value = [
        {"conditionId": "A", "asset": "A-y", "size": 100.0, "curPrice": 0.30}
    ]
    api.get_open_orders.return_value = [
        {"side": "BUY", "market": "A", "asset_id": "A-y", "price": "0.30",
         "original_size": "100", "id": "o-yes"}
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes"), _elig("A", "A-n", "No")])
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-yes" in cancelled                       # YES 旧单被撤
    placed_tokens = [c.args[0] for c in api.place_limit_buy.call_args_list]
    assert placed_tokens and all(t == "A-n" for t in placed_tokens)  # 只挂 NO


def test_held_value_deducts_other_side_budget():
    # 持有 YES 市值 50U(100×0.50);max_exposure_usd=100 -> NO 侧预算 50U,fixed 200 被封到 100。
    worker, api, db = _make_worker(template={
        "max_exposure_usd": 100,
        "tier_rules": [
            [{"upper": None, "action": {"type": "fixed_shares", "shares": 200}}]
            for _ in range(6)
        ],
    })
    api.get_user_positions.return_value = [
        {"conditionId": "A", "asset": "A-y", "size": 100.0, "curPrice": 0.50}
    ]
    api.get_orderbook.return_value = _ob([(0.50, 1000)], [(0.51, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes"), _elig("A", "A-n", "No")])
    placed = {round(c.args[1], 2): c.args[2]
              for c in api.place_limit_buy.call_args_list if c.args[0] == "A-n"}
    assert placed.get(0.50) == 100   # 预算 100-50=50U;50/0.50=100 封顶(未扣则 200)


def test_both_sides_held_cancels_both_and_places_nothing():
    worker, api, db = _make_worker()
    api.get_user_positions.return_value = [
        {"conditionId": "A", "asset": "A-y", "size": 100.0, "curPrice": 0.30},
        {"conditionId": "A", "asset": "A-n", "size": 100.0, "curPrice": 0.30},
    ]
    api.get_open_orders.return_value = [
        {"side": "BUY", "market": "A", "asset_id": "A-y", "price": "0.30",
         "original_size": "100", "id": "o-yes"},
        {"side": "BUY", "market": "A", "asset_id": "A-n", "price": "0.30",
         "original_size": "100", "id": "o-no"},
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes"), _elig("A", "A-n", "No")])
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-yes" in cancelled and "o-no" in cancelled
    api.place_limit_buy.assert_not_called()
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_place_orders.py::test_paused_side_cancels_resting_and_other_side_runs tests/test_place_orders.py::test_both_sides_held_cancels_both_and_places_nothing -v`
Expected: FAIL（当前是市场级跳过：持仓 → 整市场 skip，既不撤 YES 旧单、也不挂 NO；`o-yes` 不在 cancelled、且两侧都持仓时不撤旧单）

- [ ] **Step 3: 实现（`engine/manager.py` 共 4 处编辑）**

**(3a) import 行（第 15 行）** — 把
```python
from engine.positions import held_condition_ids
```
改为
```python
from engine.positions import held_side_info
```

**(3b) 读持仓（原 `held = held_condition_ids(positions)`）** — 改为
```python
        held_assets, held_value, held_shares = held_side_info(positions)
```

**(3c) 删市场级跳过** — 把
```python
            if mid in blacklist or mid in held:
                continue
```
改为
```python
            if mid in blacklist:
                continue
```

**(3d) 预算扣减 + 暂停侧排除出 ladder 计算** — 把
```python
            budget = min(balance, max_exposure_usd)
            shares_budget = max_exposure_shares
            if budget <= 0 or shares_budget <= 0:
                continue
            ladders = compute_market_ladders(
                side_a, side_b, tier_rules, budget, shares_budget
            )
            ladders = apply_double_sided_floor(ladders, min_price_double_cents)
```
改为
```python
            budget = max(0.0, min(balance, max_exposure_usd) - held_value.get(mid, 0.0))
            shares_budget = max(0, max_exposure_shares - int(held_shares.get(mid, 0.0)))
            budget_ok = budget > 0 and shares_budget > 0
            ladders = {"a": [], "b": []}
            if budget_ok:
                ca = None if side_a["token_id"] in held_assets else side_a
                cb = None if (side_b and side_b["token_id"] in held_assets) else side_b
                ladders = compute_market_ladders(
                    ca, cb, tier_rules, budget, shares_budget
                )
                ladders = apply_double_sided_floor(ladders, min_price_double_cents)
```

**(3e) 撤改收敛循环按侧分流** — 把
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
                            wallet=self.wallet_address,
                            market_id=mid,
                            action_type="buy_reconcile_cancel",
                            side="-",
                            price=-1,
                            size=0,
                            reason="撤改收敛:撤掉价漂移/量不符的旧买单(目标多档梯已变)",
                            price_basis=f"撤 {len(cancel_ids)} 笔 BUY；来源：CLOB get_open_orders",
                        )
                    except Exception as ex:
                        logger.warning("Reconcile cancel %s failed: %s", token_id, ex)
```
改为
```python
            for key, side in (("a", side_a), ("b", side_b)):
                if side is None:
                    continue
                token_id = side["token_id"]
                resting = buys_by_token.get(token_id, [])
                if token_id in held_assets:
                    # 成交后单侧暂停:撤光该侧全部在挂买单、不挂新单(SP5b Q1)
                    cancel_ids, to_place = reconcile_buy_orders([], resting)
                    cancel_reason = "成交后单侧暂停:撤掉该侧全部买单,直至该侧持仓平掉"
                    cancel_action = "side_pause_cancel"
                elif budget_ok:
                    cancel_ids, to_place = reconcile_buy_orders(
                        ladders.get(key, []), resting
                    )
                    cancel_reason = "撤改收敛:撤掉价漂移/量不符的旧买单(目标多档梯已变)"
                    cancel_action = "buy_reconcile_cancel"
                else:
                    # 预算不足(扣减后):活跃侧保持不动
                    continue
                if cancel_ids:
                    try:
                        self.api.cancel_orders(cancel_ids)
                        self.db.record_action(
                            wallet=self.wallet_address,
                            market_id=mid,
                            action_type=cancel_action,
                            side="-",
                            price=-1,
                            size=0,
                            reason=cancel_reason,
                            price_basis=f"撤 {len(cancel_ids)} 笔 BUY；来源：CLOB get_open_orders",
                        )
                    except Exception as ex:
                        logger.warning(
                            "Reconcile/pause cancel %s failed: %s", token_id, ex
                        )
```

> 下面原有的 `for price, shares in to_place:` 下单块**保持不动**——暂停侧 `to_place` 恒为空,自然不下单。

- [ ] **Step 4: 运行确认 PASS**

Run: `python -m pytest tests/test_place_orders.py -v`
Expected: PASS（新增 3 个 + 既有用例。既有 `test_skips_held_and_cooldown_and_blacklist` 靠冷却仍跳过、不受影响；其余 positions 默认 `[]` → `held_assets` 空、`held_value` 空 → 预算与改前一致。）

- [ ] **Step 5: 全套测试无回归**

Run: `python -m pytest -q`
Expected: ALL PASS（基线 383 + Task1 的 4 + Task2 的 3 = `390 passed`）

- [ ] **Step 6: Commit**

```bash
git add engine/manager.py tests/test_place_orders.py
git commit -m "feat(manager): place_orders 成交后单侧暂停(暂停侧全撤+另一侧扣已持仓预算)"
```

---

## 验收 checkpoint（对应 spec §七）

1. 某侧持仓 → 该侧旧买单撤光、不挂新单;另一侧照常多档挂单:`test_paused_side_cancels_resting_and_other_side_runs`。
2. 另一侧预算按 `min(余额,max_exposure_usd) − 本市场已持仓市值` 扣减:`test_held_value_deducts_other_side_budget`。
3. 持仓平掉(size→0)后该侧自动恢复:无需专门用例——`held_assets` 每轮实时重算,size→0 时该 token 不再在集合中,走 `elif budget_ok` 正常挂单分支(由信号语义保证)。
4. 两侧都持仓 → 两侧都撤光、都不挂:`test_both_sides_held_cancels_both_and_places_nothing`。
5. 离场/止损/Step3/Step1 不变:本计划只动 `place_orders` 与一个纯函数,未触 `monitor.py`/`take_profit.py`,`python -m pytest -q` 全绿即证。
6. `pytest` 全绿:Task2 Step5。

## 范围之外

SP5a 三档节奏 + 观察名单 + 跌出 eligible 整仓撤单 · SP6 模板 UI（含 `held_condition_ids` 死字段清理）。
