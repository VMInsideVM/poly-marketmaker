# 结果提交即市价清仓（resolution market-sell positions）设计 / spec

> 日期：2026-07-16　　状态：已批准，待写实现计划

## 一、背景与目标

Polymarket 经 UMA 乐观预言机结算。一旦有人在 UMA 上提交裁决，Gamma 市场对象的
`umaResolutionStatus` 由 `None`/空 变为非空（`proposed` → `disputed` → `resolved`），
而 `acceptingOrders` 仍为 `True`、`closed` 仍为 `False`——市场随时会结算成 0/1。

现有的 UMA 结算守卫（`engine/resolution.py` `in_resolution()` + `monitor.check_resolution()`
+ `place_orders` 跳过）只做**买单侧**：撤掉该市场全部买单、阻止重挂。**持仓不动**，仍交给
`check_exit` 的两段式离场，而离场铁律是**永不低于成本**（只有亏损 ≥ 强平阈值才 B0 市价）。

问题：结算在即时，成本价挂着的 maker 卖单很可能撑到结算都不成交；若持有的是将被判为 0 的那一边，
整仓归零。故本次给守卫加**持仓侧**动作：**同一「结果已提交」信号下，把该市场全部持仓直接市价清仓**，
不论盈亏。这在结算市场里**主动推翻「永不低于成本」**——合理：与其挂个永不成交的成本价卖单等着被结算成 0，
不如立刻按市价把盘口流动性吃到手。用户已确认范围为「该市场全部持仓都市价卖」。

## 二、触发点（复用，不新造）

「结果被提交」= `in_resolution(umaResolutionStatus)` 为真，与现有买单侧守卫用的是同一判定。
本次是在这个信号上加持仓侧动作，形成 CLAUDE.md 已描述的「多点守卫」：买单侧（`check_resolution`）+
持仓侧（`check_exit`，本次新增）。判定仍集中在 `in_resolution()` 一处，不掺 `closed`/`acceptingOrders`。

## 三、落点：折进 `check_exit`（方案 B）

对比过两个落点：

- **方案 A（扩 `check_resolution` 里卖持仓）**：`check_resolution` 现只取在挂买单，要在此卖持仓得再拉
  positions、重建成本、复制整套市价卖 + 记账逻辑，与 `_exit_position` 重复；且 `check_resolution` 先跑、
  `check_exit` 后跑，两步会就同一持仓打架（前者刚市价卖、后者又按成本挂 rest）。**否决。**
- **方案 B（折进 `check_exit`，采用）**：`check_resolution` 保持不动（继续管买单侧，且覆盖「只有买单、
  无持仓」的市场）；`check_exit` 对持仓的 condition_id 批量取一次结算状态，结算市场的持仓**跳过 `plan_exit`、
  直接走已有市价卖分支**。一个持仓一处决策，不打架，完全复用离场机器（成本重建、`market_fill_price`、记账、状态行）。

## 四、详细设计

### 4.1 `check_exit` 顶部：算出 `resolving` 集合

取完 `positions` 后，对 `size > 0` 的 distinct `conditionId` 调
`self.api.gamma_resolution_status(cids)` → `status_map`，
`resolving = {cid for cid in cids if in_resolution(status_map.get(cid))}`。

- **fail-open**：Gamma 抖动返回 `{}` → `resolving` 空 → 全部持仓走原离场，绝不因一次接口抖动误清全仓。
- 把「该持仓是否在结算市场」作为布尔传入 `_exit_position`（`in_resolution_market = pos.conditionId in resolving`）。

### 4.2 `_exit_position` 新增参数 `in_resolution_market: bool`

为真时，在算出 `cost, lots` 之后、调用 `plan_exit` 之前**分叉**，直接市价清仓并 return：

1. **无视成本、无视盈亏**一律市价卖（用户已确认：该市场全部持仓都卖）。
2. 复用现有 market 分支：**先撤该 asset 全部挂卖单**（否则挂卖单占用份额，市价卖没份额可卖），
   再 `place_market_sell(asset_id, size)`（FAK）。撤单失败仍继续清仓（沿用现 market 分支的取舍：离场优先出手）。
3. **成本取不到也照卖**（与正常离场的关键差异）：正常离场在成本重建失败时是「跳过 + ⚠️裸奔」；
   但结算市场里「裸奔不卖」= 等着被结算成 0，是最坏情况。故结算清仓**不 skip**——成本已知则记 pnl，
   未知则只记成交价/理由、pnl 留空（不写 `record_trade`）。
4. 成交价用 `market_fill_price(resp, best_bid, cur)`（**绝不用 Data API 现价**，沿用 2026-06-24 教训：
   现价常与盘口背离，会把止损显示成盈利）。`best_bid`/`cur` 由 `_sell_book(asset_id)` 与 `pos.curPrice` 取。

### 4.3 记账

- **总是** `record_action(action_type="exit_market", reason="市场结果已提交/进入 UMA 结算 → 市价清仓（无视盈亏）")`
  + 状态行 `action="⚠️结算·市价清仓"`（detail 含 `umaResolutionStatus` 值、成本/成交价）。
- **成本已知时**沿用现有 B0 写法 `record_trade(side="stop_loss", price=fill, pnl=(fill - cost) * size)`
  （复用最省改动；trades 表语义仍是「强制市价离场 + pnl」）。
  - **已知副作用**：盈利的结算清仓在原始 trades 历史里也显示为「止损」，但 action 理由写清是结算清仓。
    若要独立标签「结算清仓」，仅需新增一个 `side` 值 + `history.html` 一处显示映射——本次按 YAGNI 不做，
    留作后续。
- 记账/状态行失败仅 `logger.warning`，绝不打断离场（与现有埋点同级）。

### 4.4 tick 顺序与交互（不变）

`check_buy_orders → check_resolution（撤买单）→ check_exit（现在含结算清仓）→ check_sell_orders`。
FAK 部分成交时，下一 tick `check_exit` 对残余持仓再次市价清仓（Gamma 仍标结算），直至清空，自愈。

因结算清仓是 `check_exit` 内**唯一**触碰该持仓的分支（撤挂卖单 → 市价卖），与 `check_resolution`
的买单侧互不干涉，不产生「一步挂 rest、另一步又市价卖」的churn。

### 4.5 常开、无配置开关

与现有 UMA 结算守卫一致——这是安全离场，不是策略选项，故常开、无 config key。

## 五、测试

- **`tests/test_monitor.py` `_make_monitor`**：补默认 `api.gamma_resolution_status.return_value = {}`
  （无市场结算），否则裸 `MagicMock` 会被 `in_resolution` 判真，冲垮现有 `check_exit` 用例。
  （与 `tests/test_place_orders.py:27` 同款处理。）
- **新增用例**（`check_exit` / `_exit_position`）：
  1. 结算市场持仓 → 走 `place_market_sell`，且**先撤该 asset 挂卖单**、**不**走 `place_limit_sell`；记
     `exit_market` action + 状态行。
  2. 成本未知的结算持仓 → **仍市价卖**（不「裸奔跳过」），不写 `record_trade`。
  3. Gamma 返回 `{}` → `resolving` 空 → 持仓走原离场（回归，行为不变）。
  4. 非结算持仓 → 行为完全不变（原 `plan_exit` 路径）。
  5. 成本已知的结算清仓 → `record_trade(side="stop_loss", pnl=(fill-cost)*size)`，`fill` 来自
     `market_fill_price`。
- `plan_exit` / `take_profit.py` 纯函数**不动**（结算判定在其之外）。

## 六、版本与文档

- **版本**：此改动在结算市场里推翻「永不低于成本」，属行为改变 → 按 `docs/版本号规范.md` 走 **MAJOR**：
  `v6.0.2` → **`v7.0.0`**。发版公告须写明这条新行为（结果一提交即整仓市价清仓、无视盈亏）。
- **文档**：CLAUDE.md「Critical behaviors to preserve」里 UMA 守卫那段补上「持仓侧结算清仓」；
  `history.html` 无需改（复用 `exit_market` action 显示）。

## 七、不做（YAGNI）

- 不加配置开关（安全离场，常开）。
- 不加独立「结算清仓」trades `side` 标签（复用 `stop_loss`，成本记 pnl；副作用见 4.3）。
- 不改 `check_resolution` 买单侧逻辑、不改 `place_orders` 跳过逻辑、不改 `plan_exit`。
- 不掺 `closed`/`acceptingOrders` 进触发（仍只看 `umaResolutionStatus`）。
