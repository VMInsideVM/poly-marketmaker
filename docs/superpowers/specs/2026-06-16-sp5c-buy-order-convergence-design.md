# SP5c：买单撤改收敛（buy-order cancel/replace convergence）设计 / spec

> 日期：2026-06-16
> 状态：待用户评审
> SP5（节奏/观察名单/单侧暂停/撤改收敛）的第一个子块。父背景见记忆 [[v4-strategy-integration-roadmap]]。

## 零、背景与定位

v4 §6「撤改收敛原则」：每轮重判后，仅当挂单的目标价或目标份数变化时才撤旧挂新；目标没变就保持不动，避免无谓频繁撤挂；买单（§5 多档）与卖单（§7 离场）皆适用。SP2/SP3 都把它推迟到这里。

**现状**：
- **卖单已具备**：`check_exit` 的 rest 动作用 `plan_take_profit` 维护**恰好一笔**卖单，价/量变才撤改、没变保持。→ SP5c **不动卖单**。
- **买单缺**：`place_orders` 是「幂等跳过（同价已挂就不重挂）+ 只挂新」，从不撤掉「价已不在目标多档梯」的旧买单，也不管同价的量不符。书一移，旧档买单就滞留（仅 Step3 的价差/价格区间/黑名单会撤部分）。

**SP5c 范围（窄，已与用户确认）**：只收**还在做的市场**（`place_orders` 处理到的 eligible 市场）的**档位漂移**——把现挂买单收敛到当前目标多档梯。

**不在 SP5c**：跌出 eligible 的市场整仓买单（→ SP5a 实时看守）、成交后单侧暂停（→ SP5b）、三档节奏重构（→ SP5a）。

## 一、纯函数 `reconcile_buy_orders`

`engine/laddering.py` 新增（纯函数，全单测）：

```python
def reconcile_buy_orders(ladder, resting_buys):
    """撤改收敛(v4 §6):把某 token 的现挂买单收敛到目标 ladder。

    ladder: [(price, shares), ...] 该 token 的目标多档(compute_market_ladders 结果)。
    resting_buys: [{"id","price","original_size"/"size"}, ...] 该 token 当前在挂买单。
    返回 (cancel_ids, to_place):
      - 现挂买单价不在目标、或同价但量不符(容差) -> 撤(进 cancel_ids)。
      - 价量都符 -> 保持(不撤不挂)。
      - 目标档里没被任何现挂单保持到的 -> 挂(进 to_place,[(price,shares)])。
    """
```

要点：
- 价匹配按 `round(price, 4)`（与下单价同口径）。
- 量匹配带容差 `abs(a-b) <= max(1.0, 0.01*b)`（防 sub-share/部分成交导致无谓 churn；与 `take_profit.py._size_matches` 同口径，此处内联一个等价小函数，保持 laddering 模块自洽）。
- 空 `ladder` → 全部现挂单进 cancel_ids、`to_place` 为空（目标为空即全撤）。

## 二、`place_orders` 接线改动（`engine/manager.py`）

两处：

**(a) 下单循环改为撤改收敛**：现有按 side 遍历 `ladders[key]` 的「幂等跳过 + place_limit_buy」改为：每个 token 取其现挂买单（从 `open_orders` 按 `asset_id` 过滤），调 `reconcile_buy_orders(ladder_for_token, resting)` → 先 `cancel_orders(cancel_ids)`、再对 `to_place` 逐个 `place_limit_buy`。记 action（撤改原因）。

**(b) 预算口径回归全额**：现有
```python
budget = min(balance, max_exposure_usd) - exposure_usd.get(mid, 0.0)
shares_budget = max_exposure_shares - exposure_shares.get(mid, 0)
```
改为
```python
budget = min(balance, max_exposure_usd)
shares_budget = max_exposure_shares
```
并删除为此服务的 `exposure_usd`/`exposure_shares` 累计（`open_price_keys` 也随幂等逻辑退役；`markets_with_open`（并发市场上限用）保留）。

> 为什么改回全额：SP2 的「扣已挂敞口」是「只幂等不撤」下防跨轮超额的权宜。有了撤改收敛，每轮按全额预算重算目标梯、并把现挂收敛到它——resting 始终 = target ≤ 敞口，天然不超额。若仍扣已挂敞口，会把「本轮即将被撤的旧单」算进占用，导致少挂。

## 三、保留不变

- 卖单：`check_exit` rest 动作的 `plan_take_profit` 单笔卖单对账（已是撤改收敛）。
- `place_orders` 的：黑名单/持仓/冷却跳过、最大并发市场数闸（`markets_with_open`）、live 余额读取、§8 `apply_double_sided_floor`、记 action/状态。
- Step3 `_check_compliance` 的价差复查 / 价格区间 / 黑名单撤单（realtime 安全网，与撤改收敛互补）。

## 四、测试

- **`reconcile_buy_orders` 纯函数**（核心）：
  - 价漂移：现挂 0.30，目标只剩 0.29 → 撤 0.30、挂 0.29。
  - 量不符：现挂 0.30×100，目标 0.30×200 → 撤 0.30、挂 0.30×200。
  - 价量都符：现挂 0.30×100，目标含 0.30×100 → 保持（cancel_ids 空、to_place 不含 0.30）。
  - 容差：现挂 0.30×100，目标 0.30×100.5 → 保持（量容差内）。
  - 空目标：现挂若干、ladder=[] → 全撤、to_place 空。
- **`place_orders`（test_place_orders.py）**：书移后旧价档被撤、新价档挂上；稳定轮（书不变）不撤不挂（不 churn）；预算用全额（min(balance, 敞口)，不再减已挂）。
- 改写 SP2 的 `test_existing_exposure_on_market_reduces_budget`：语义已变（全额预算 + 撤改收敛），改为验证撤改收敛/全额预算行为或删除。

## 五、验收 checkpoint

1. 书移动致目标梯变化：旧价档买单被撤、新价档买单挂上（`reconcile_buy_orders` + place_orders 测试）。
2. 同价量不符 → 撤改；价量都符 → 保持不 churn。
3. 预算按全额（min(余额,敞口)），跨轮 resting 收敛到 target ≤ 敞口、不超额、不滞留旧档。
4. 卖单行为不变（仍由 plan_take_profit 收敛）。
5. `pytest` 全绿。

## 六、范围之外

SP5a 三档节奏 + 观察名单 + 跌出 eligible 市场的整仓撤单（实时看守）· SP5b 成交后单侧暂停 · SP6 模板 UI。
