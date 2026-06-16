# SP5b：成交后单侧暂停（pause-the-filled-side after a fill）设计 / spec

> 日期：2026-06-16
> 状态：待用户评审
> SP5 的第二个子块（SP5c 已先行合并）。父背景见记忆 [[v4-strategy-integration-roadmap]]。

## 零、背景与定位

v4 §4.4 / §7：**某侧成交后仅暂停成交那一侧（YES 或 NO）的新买单，直至该侧持仓平掉；另一侧照常运行。** 目标是尽快把持仓卖出、不让它占用资金、影响后续挂单——出货期间不再往该侧加仓。

**现状**：`place_orders`（`engine/manager.py`）用 `held_condition_ids(positions)` 得到「有持仓的市场(condition_id)集合」，循环里 `if mid in blacklist or mid in held: continue` **跳过整个市场**（YES、NO 两侧都不挂）。粒度是市场级。

**SP5b 范围**：把这个跳过从**市场级（condition_id）细化到侧级（asset_id / token）**——某侧有持仓就暂停该侧，另一侧继续。**不引入新的模板参数**，复用 Data API 持仓信号。

**不在 SP5b**：三档节奏 / 观察名单 / 跌出 eligible 整仓撤单（→ SP5a）；模板 UI（→ SP6）。离场（`check_exit`）、止损、Step3 安全网、Step1 成交检测全部不动。

## 一、暂停信号（基线，非新参数）

某侧"暂停"判定 = 该侧 token（`asset` = asset_id）的 Data API 持仓 `size > 0`。`size → 0` 时自动恢复。

- 与现有 `held` 机制同源、**自愈**、无需持久化「成交标记」状态——和现在 `held` 每轮重读持仓一致，只是粒度更细。
- 覆盖「启动前 / 转入的持仓」也合理：有仓即进入该侧出货模式，不再加仓。
- 「直至该侧持仓平掉」= `size → 0`：用每轮实时持仓自然实现，无需额外状态机。

## 二、纯函数 `held_side_info`（`engine/positions.py`）

与既有 `held_condition_ids` 并列新增（纯函数、全单测）。一次遍历 `positions`（Data API `/positions` 返回，每项含 `asset` / `size` / `conditionId` / `curPrice`）产出三样：

```python
def held_side_info(positions):
    """从 Data API /positions 提取按侧(asset)暂停信息 + 按市场已持仓敞口。

    返回 (held_assets, value_by_market, shares_by_market):
      held_assets:      {asset_id}  size>0 的 token(该侧暂停新买单)。
      value_by_market:  {conditionId: Σ size×curPrice}  已持仓市值(扣减另一侧预算)。
      shares_by_market: {conditionId: Σ size}            已持仓份数(扣减份数预算)。
    size<=0 的项忽略;缺 asset/conditionId 的项只跳过对应聚合。size/curPrice 安全转换。
    """
```

要点：
- 市值用 **`curPrice`**（持仓当前市值，Data API 直接给）——**绝不用 avgPrice**（项目已彻底禁止 avgPrice 作为成本/价值来源，见 [[take-profit-position-driven]]）。`curPrice` 仅作预算扣减的护栏估计，与离场卖价无关。
- `curPrice` 缺失/为 0 → 该仓位市值计 0（少扣，较不保守，罕见，可接受）。

保留旧 `held_condition_ids` 不删（其自身单测仍在，且改动最小）。SP5b 后它仅剩测试引用、production 不再用，标记为 **SP6 死字段清理候选**。

## 三、`place_orders` 接线（`engine/manager.py`）

**(a) 读持仓改用 `held_side_info`**：把现有
```python
held = held_condition_ids(positions)
```
改为
```python
held_assets, held_value, held_shares = held_side_info(positions)
```

**(b) 删市场级跳过**：把
```python
if mid in blacklist or mid in held:
    continue
```
改为
```python
if mid in blacklist:
    continue
```
> **必须删**：否则有持仓的市场被整市场跳过，暂停侧的旧买单（§四 Q1 全撤）就永远撤不掉。冷却闸、并发闸（`markets_with_open`）保留不变。

**(c) 预算扣减（Q2：扣掉已持仓价值）**：把
```python
budget = min(balance, max_exposure_usd)
shares_budget = max_exposure_shares
if budget <= 0 or shares_budget <= 0:
    continue
```
改为
```python
budget = max(0.0, min(balance, max_exposure_usd) - held_value.get(mid, 0.0))
shares_budget = max(0, max_exposure_shares - held_shares.get(mid, 0))
budget_ok = budget > 0 and shares_budget > 0
```
> 不再 `continue`：预算不足时仍要让暂停侧进入循环撤单。`budget_ok` 改为在循环内对**活跃侧**生效（预算不足→活跃侧保持不动，沿用旧语义、不误撤活跃侧旧单）。

**(d) 算 ladder 时暂停侧传 None**（活跃侧拿全额扣减后预算）：原
```python
ladders = compute_market_ladders(side_a, side_b, tier_rules, budget, shares_budget)
ladders = apply_double_sided_floor(ladders, min_price_double_cents)
```
改为
```python
ladders = {"a": [], "b": []}
if budget_ok:
    ca = None if side_a["token_id"] in held_assets else side_a
    cb = None if (side_b and side_b["token_id"] in held_assets) else side_b
    ladders = compute_market_ladders(ca, cb, tier_rules, budget, shares_budget)
    ladders = apply_double_sided_floor(ladders, min_price_double_cents)
```
（`compute_market_ladders` 已支持任一侧为 None：side=None → 该侧空 ladder，另一侧独享预算。）

**(e) 撤改收敛循环按侧分流（Q1：暂停侧全撤）**：原
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
            self.db.record_action(... action_type="buy_reconcile_cancel" ...)
        except Exception as ex:
            logger.warning(...)
    for price, shares in to_place:
        ... place_limit_buy ...
```
改为：
```python
for key, side in (("a", side_a), ("b", side_b)):
    if side is None:
        continue
    token_id = side["token_id"]
    resting = buys_by_token.get(token_id, [])
    paused = token_id in held_assets
    if paused:
        # 成交后单侧暂停:撤光该侧全部在挂买单、不挂新单(Q1)
        cancel_ids, to_place = reconcile_buy_orders([], resting)
        cancel_reason = "成交后单侧暂停:撤掉该侧全部买单,直至该侧持仓平掉"
        cancel_action = "side_pause_cancel"
    elif budget_ok:
        cancel_ids, to_place = reconcile_buy_orders(ladders.get(key, []), resting)
        cancel_reason = "撤改收敛:撤掉价漂移/量不符的旧买单(目标多档梯已变)"
        cancel_action = "buy_reconcile_cancel"
    else:
        # 预算不足(扣减后):活跃侧保持不动
        continue
    if cancel_ids:
        try:
            self.api.cancel_orders(cancel_ids)
            self.db.record_action(
                wallet=self.wallet_address, market_id=mid,
                action_type=cancel_action, side="-", price=-1, size=0,
                reason=cancel_reason,
                price_basis=f"撤 {len(cancel_ids)} 笔 BUY；来源：CLOB get_open_orders",
            )
        except Exception as ex:
            logger.warning("Reconcile/pause cancel %s failed: %s", token_id, ex)
    for price, shares in to_place:   # 暂停侧 to_place 恒为空
        try:
            self.api.place_limit_buy(token_id, price, shares,
                tick_size=side["tick_size_str"], neg_risk=side["neg_risk"])
            placed += 1
            markets_with_open.add(mid)
            self._record_place_buy_tier(mid, side, price, shares)
            if limit is not None and placed >= limit:
                return
        except Exception as ex:
            logger.error("place_limit_buy failed %s: %s", token_id, ex)
```

> `reconcile_buy_orders([], resting)` 对空目标返回 `(全部 resting 的 id, [])`——撤光、不挂，正是 Q1。暂停侧每轮都会尝试撤；resting 为空（上一轮已撤完）时 `cancel_ids=[]`，不记 action、不噪声。

## 四、已确认决策

| | 决策 | 实现 |
| --- | --- | --- |
| **Q1 暂停侧旧买单** | **全撤该侧** | 暂停侧 `reconcile_buy_orders([], resting)` → 撤光、不挂新单 |
| **Q2 敞口口径** | **扣掉已持仓价值** | 另一侧预算 `= max(0, min(余额,max_exposure_usd) − Σ size×curPrice)`；份数同理 |

## 五、边界交互（不需特殊处理，记录备查）

1. **§8 <10¢ 双边**：一侧持仓暂停（传 None → ladder 空）、另一侧 <10¢ 时，`apply_double_sided_floor` 因「凑不齐双边」把活跃侧也清零（不单边挂 <10¢）→ 活跃侧 `reconcile([], resting)` 把其旧买单也撤掉。安全、符合 §8。
2. **暂停侧已成交买单残量**：仍由 Step1 `check_buy_orders` 的 `cancel_remainder` 撤（原有逻辑）；SP5b 额外撤的是该侧**其它档没成交的**买单。两者互补不冲突。
3. **预算被持仓吃满**（`held_value ≥ cap` → `budget_ok=False`）：活跃侧保持不动（不挂、不误撤其旧单），暂停侧照常撤光。
4. **两侧都持仓**：两侧都按暂停处理（各自撤光、不挂），等价于旧的整市场跳过，但额外把两侧旧买单撤掉（符合 Q1）。

## 六、测试

- **`held_side_info` 纯函数**（`tests/test_positions.py`）：
  - YES 有仓（size>0）、NO 无仓 → `held_assets` 只含 YES asset。
  - `value_by_market` = Σ size×curPrice；`shares_by_market` = Σ size（同市场两仓累加）。
  - `size<=0` 忽略；缺 `asset` / `conditionId` / `curPrice` 安全（不抛、对应聚合跳过或计 0）。
- **`place_orders`（`tests/test_place_orders.py`）**：
  - **暂停侧撤光 + 另一侧照常**：持有 YES（持仓 asset `A-y` size>0），市场 A 有 YES(`A-y`)+NO(`A-n`) 两侧 eligible，YES 有在挂买单 → YES 旧买单被撤、YES 不挂新单；NO 侧正常多档挂上。
  - **已持仓市值扣减另一侧预算**：持有 YES 市值 ≈50U（size×curPrice），`max_exposure_usd=100` → NO 侧预算 50U，挂单份数按 50U 封顶。
  - **两侧都持仓 → 两侧都撤光、不挂**：YES、NO 都有仓且都有在挂买单 → 两侧买单都被撤、`place_limit_buy` 不被调用。

## 七、验收 checkpoint

1. 某侧持仓 → 该侧旧买单撤光、不挂新单；另一侧照常多档挂单（侧级而非市场级暂停）。
2. 另一侧预算按 `min(余额,max_exposure_usd) − 本市场已持仓市值` 扣减（份数同理）。
3. 持仓平掉（size→0）后该侧自动恢复挂单（下一轮）。
4. 两侧都持仓时两侧都撤光、都不挂。
5. 离场 / 止损 / Step3 / Step1 行为不变。
6. `pytest` 全绿。

## 八、范围之外

SP5a 三档节奏 + 观察名单 + 跌出 eligible 整仓撤单 · SP6 模板 UI（含 `held_condition_ids` 死字段清理）。
