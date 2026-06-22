# UMA 结算提交即撤买单（resolution guard）设计 / spec

> 日期：2026-06-22
> 状态：待用户评审
> 背景：做市买单不应挂在一个即将结算的市场上——一旦有人在 UMA 上提交了 resolution，该市场随时会结算，此时被成交买进等于接盘将定价为 0/1 的份额。

## 零、背景与可行性

Polymarket 经 UMA 乐观预言机结算。实测 Gamma API（`GET /markets?condition_ids=...`，与现有 `gamma_markets_by_condition` 同端点）按市场暴露：

| 市场状态 | `umaResolutionStatus` | `acceptingOrders` | `closed` |
| --- | --- | --- | --- |
| 正常交易 | `None` | `True` | `False` |
| **UMA 已提交（proposal）** | `'proposed'` | `True` | `False` |
| 被争议 | `'disputed'` | `True` | `False` |
| 已结算 | `'resolved'` | `False` | `True` |

关键：proposal 提交后 `acceptingOrders` 仍 `True`、`closed` 仍 `False`，所以现有信号抓不到这个窗口——我们的挂买单仍活着、可能被吃进一个马上结算的市场。`umaResolutionStatus` 字段正是「有人提交了」的实时信号。订单字典带 `market`(=condition_id)，可批量映射。

**已确认决策**：① 触发 = `umaResolutionStatus` 非空（proposed/disputed/resolved 任一）；② 既撤现有买单、也阻止下一轮重挂；③ 默认开启、无配置键；④ 只撤 BUY，持仓 + 卖单不动（卖单仍由 `check_exit` 在结算前正常离场）。

## 一、架构

撤单（监控线程 `_tick`，每 `fill_check_interval_sec`）与阻止重挂（采集线程 `_scanner_loop` → `place_orders`，每 `scan_interval_sec`）在**不同线程**。两个执行点各自独立查一次 Gamma、各管各的动作，**无跨线程共享状态**，无竞态（与现有黑名单「多点独立拦截」一致）。

- **新纯函数模块 `engine/resolution.py`**（无 IO，全单测）：把「是否进入结算」的判定独立出来，单一职责，便于将来扩展触发条件。
- **新 API 原语 `gamma_resolution_status`**（`api/polymarket_api.py`）：批量取 condition_id → umaResolutionStatus，**失败 fail-open 返回 `{}`**。
- **监控新增 `check_resolution()`**（`engine/monitor.py`），插入 `_tick`：撤掉处于结算的市场的全部 BUY。
- **`place_orders` 加 resolution 跳过**（`engine/manager.py`）：下单循环跳过处于结算的市场。

## 二、纯函数 `engine/resolution.py`

```python
def in_resolution(uma_status) -> bool:
    """umaResolutionStatus 非空(proposed/disputed/resolved)即视为已进入 UMA 结算。

    None / "" / 纯空白 -> False(正常交易,可挂买单)。任何非空字符串 -> True。
    """
    return bool(uma_status and str(uma_status).strip())
```

## 三、API 原语 `gamma_resolution_status(condition_ids) -> dict`

```python
@staticmethod
def gamma_resolution_status(condition_ids: list) -> dict:
    """批量取每个 condition_id 的 umaResolutionStatus。

    GET /markets?condition_ids=a&condition_ids=b...(公共接口,免 auth),返回
    {condition_id: umaResolutionStatus 或 None}。网络/HTTP 失败 -> 返回 {}
    (fail-open:Gamma 抖动时不动任何买单,绝不因一次接口失败误撤全仓买单)。
    """
```

去重输入；空输入回 `{}`；只回 Gamma 实际返回的市场（缺失的视为无数据 → 不动）。

## 四、监控撤单 `check_resolution()`（`engine/monitor.py`）

插入 `_tick`：`check_buy_orders → check_resolution → check_exit → check_sell_orders`（撤单是买单侧，放在 fill 检测之后、离场之前）。

逻辑：
1. `open_orders = self.api.get_open_orders()`；取 `side=="BUY"`，按 `market`(=condition_id) 分组。无买单 → return。
2. `status_map = self.api.gamma_resolution_status(distinct_cids)`。
3. 对每个 `in_resolution(status_map.get(cid))` 为真的市场：`self.api.cancel_orders(该 cid 的全部 BUY id)`；发状态行 `action="⚠️UMA已提交·撤买单"`（detail 含 status 值与撤单数）；`record_action(action_type="uma_resolution_cancel", reason=..., price_basis="status={status}；来源：Gamma /markets")`。撤单失败 → log warning（不致命，下个 tick 重试）。
4. 持仓、卖单不触碰。

## 五、阻止重挂（`engine/manager.py` `place_orders`）

构建待下单 `market_id` 列表（现有 `order`）后，批量查一次：
```python
status_map = self.api.gamma_resolution_status(order)
resolving = {c for c in order if in_resolution(status_map.get(c))}
```
下单循环对 `mid in resolving` 的市场 `continue` 跳过；`log.info` 记录跳过的市场数。Gamma 失败 → `resolving` 空 → 不跳过（fail-open，与撤单侧一致）。

## 六、测试

- **`tests/test_resolution.py`**：`in_resolution` 边界——`None`/`""`/`"   "` → False；`"proposed"`/`"disputed"`/`"resolved"` → True。
- **`tests/test_monitor.py`**：`check_resolution`——两市场各有 BUY，Gamma 标其一为 `'proposed'`：只撤该市场 BUY、另一市场不动、发状态行；Gamma 失败（`gamma_resolution_status` 返回 `{}`）→ 一律不撤；无买单 → 不调用 Gamma。
- **`place_orders`**：eligible 含一个 `'proposed'` 市场 → 该市场被跳过、不下买单；其余正常。

## 七、范围之外

- 不掺 `closed`/`acceptingOrders` 进触发（只看 `umaResolutionStatus`，决策集中在 `in_resolution` 一处，将来要扩再改）。
- 不动持仓与卖单离场逻辑（`check_exit` 不变）。
- 不加配置开关（默认开启）。
