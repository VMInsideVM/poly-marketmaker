# 设计：订单管理与 monitor 改为 API 实时状态驱动

日期：2026-05-18
状态：已与用户确认，待写实现计划

## 1. 背景与动机

当前订单管理页、持仓、历史的数据全部来自本地 SQLite（`orders`/`positions`/`trades` 表），是程序运行过程中下单成功 / monitor 检测到成交后自己写进去的，**没有任何一处实时查询交易所真实状态**。后果：

- 用测试脚本 `test_real_order.py` 挂的单在订单管理页看不到（脚本不写 DB，页面只读 DB）。
- DB 是「程序自己记的账」，与交易所真实挂单/持仓可能不一致（订单被吃/掉单而 monitor 未轮询到时，页面显示陈旧挂单）。
- 撤单走的是逐笔 `cancel_order` 循环，没用 SDK 的批量 `cancel_orders` / `cancel_all`。
- 分发挂单用扫描时缓存的 `order_price` 直接挂，不在下单前重算，价格可能已过时。

目标：订单管理页与 monitor 全部改为以 **Polymarket API 实时状态为唯一真相源**；DB 仅保留历史（`trades`）与冷却（`cooldown`）。

## 2. 关键决策（已与用户确认）

| 决策点 | 结论 |
|---|---|
| 总体架构 | API 为唯一真相源；DB 只留 `trades` + `cooldown` + 配置表。`orders`/`positions` 表不再驱动任何逻辑 |
| 策略合规判定口径 | 重跑 `determine_order_price` 比对当前应挂价 |
| 重挂触发条件 | 只要重算价的「档位（tick）」与当前挂单价不同就重挂；不设容差/重挂冷却，churn 由轮询间隔限频 |
| 无法挂出合规价 | `determine_order_price` 返回 None → 撤掉该市场买单，不重挂 |
| 改造范围 | A（成交检测/止盈/止损/持仓）与 B（策略合规检查）都改成基于 API |
| 止损成本价来源 | 挂卖单/止损前调 Polymarket Data API「Get current positions for a user」取 `avgPrice` |
| 一键撤单范围 | 只撤买单（用 `cancel_orders(buy_ids)`），保留止盈卖单；作用于页面当前钱包筛选范围（无筛选=全部启用钱包） |
| 多选撤单 | `cancel_orders(selected_ids)`，按钱包分组各一次请求 |
| 成交检测机制 | 用 CLOB `get_trades(TradeParams(maker_address=<funder>, after=<水位>))` 拉成交；**遍历每个 trade 的 `maker_orders[]`，只取 `maker_address==funder` 的条目**（顶层字段是 taker/整笔视角，非我们的单），按 **`(trade_id, order_id)`** 去重；不再用 `size_matched` 增量、不再用顶层 `side/size/price` |
| 成交后处理 | 每个属于我们、`side=="BUY"` 的 maker_order 条目：用其 `matched_amount`/`price`/`asset_id` 挂止盈卖单 + `cancel_orders([order_id])` 撤该买单未成交剩余 + 按 trade 顶层 `market`(condition_id) 设 cooldown |
| 分发挂单对齐 | 下单前重拉订单簿 + 重跑 `determine_order_price`，不再用扫描缓存价 |

## 3. SDK / API 能力确认（py_clob_client_v2 1.0.1）

- `get_open_orders(params=None)`：自动翻页，返回扁平订单对象列表，字段含 `id, status, side, market, asset_id, original_size, size_matched, price, outcome, created_at` 等（与用户提供的样例 JSON 一致）。
- `get_trades(TradeParams(maker_address=, market=, asset_id=, after=, before=, id=))`：自动翻页。**经 spike 实测确认的结构**：每条 trade 顶层 `id`(uuid)、`match_time`(epoch 秒字符串)、`market`(condition_id)、`trader_side`("MAKER")，**顶层 `side/size/price/asset_id` 是 taker/整笔视角，不代表我们的单**。我们的成交在 `maker_orders[]` 数组：每个条目有 `maker_address`、`order_id`、`side`(BUY/SELL)、`matched_amount`、`price`、`asset_id`、`outcome`。一个 trade 的 `maker_orders` 可能**混有他人订单**，也可能含**多个**我们的订单；须按 `maker_address==funder` 过滤。我们挂的限价**买单被成交时**，taker 是卖方 → 顶层 `side` 会是 `SELL`，但对应 `maker_orders[i].side=="BUY"`。`after` 为时间戳过滤（仅用于限制返回量，真正去重靠 id）。
- `are_orders_scoring(OrdersScoringParams(orderIds=[...]))`：批量查订单是否在奖励区间（scoring）。`is_order_scoring(OrderScoringParams(orderId))`：单查。
- `cancel_orders(order_hashes: list)`：一次请求批量撤多笔。`cancel_all()`：撤该钱包全部。`cancel_order(payload)`：单撤。
- Polymarket Data API（非 CLOB SDK，需独立 HTTP）：`GET https://data-api.polymarket.com/positions?user=<address>` 返回**持仓数组**。**已确认字段**（用户提供文档化样例）：`proxyWallet`(持有者)、`asset`(ERC1155 token id，即我们的 asset_id)、`conditionId`(market)、`size`、`avgPrice`、`curPrice`、`outcome`、`title`、`cashPnl`、`percentPnl` 等。`user` 参数用钱包的 proxy 地址 = 本应用的 `funder`（GNOSIS_SAFE 代理）。spike 实测该 endpoint 对 funder 与 wallet 地址均返回 200（当时无持仓为空），取 `funder`。
- `are_orders_scoring` 返回结构未现场采到（spike 时无挂单）；按 SDK 惯例为 `dict[order_id] -> bool`，实现时用防御性 `.get(order_id)`，是本设计唯一未现场验证项（低风险）。

## 4. 架构总览

### 4.1 数据源映射（改造后）

| 数据 | 来源 | 替代的旧逻辑 |
|---|---|---|
| 钱包当前挂单 | CLOB `get_open_orders()`（每钱包，自动翻页） | `db.get_open_orders()` 读 `orders` 表 |
| 买单是否在奖励区间 | CLOB `are_orders_scoring(buy_ids)` 批量 | 无（新增） |
| 持仓与成本价 | Data API `GET /positions?user=<proxy>`：`avgPrice`/`size`/`curPrice` | `positions` 表 |
| 成交检测 | CLOB `get_trades()` → 展平 `maker_orders`（按 funder 过滤）→ 按 `(trade_id, order_id)` 去重 | DB `orders` 表 + `_last_matched` 增量 |
| 成交/止盈/止损历史 | 仍写 DB `trades` 表 | 不变 |
| 冷却 | 仍用 DB `cooldown` | 不变 |

### 4.2 DB 角色收缩

`orders`、`positions` 表保留建表语句（便于回滚）但读写路径全部切走，不再驱动任何逻辑。DB 仅剩：`trades`（历史展示）、`cooldown`（防同市场重复挂单）、钱包/设置/密码等配置表。

### 4.3 重启自愈

`startup_recovery` 重写为「从 API + 历史水位对账」：每钱包的成交拉取下界 `after` 初始化为 DB `trades` 表中该钱包最新一条记录的 `created_at`（无则取 0）。注意 DB `created_at` 是本地记账时间、trade `match_time` 是交易所撮合时间，两者都是 ~unix 秒、量级可比，`after` 仅用于限制返回量、设一个保守下界即可；**真正的幂等保证是 `(trade_id, order_id)` 去重**。启动后 `get_trades(after=水位)` 捞出离线期间成交，展平 `maker_orders` 后按去重键补挂止盈，既不漏离线成交也不重复处理。`_seen_fill_keys` 内存集合（元素为 `(trade_id, order_id)`）在进程内防止跨轮重复处理。不再依赖 DB 里的陈旧挂单/持仓。

### 4.4 已知代价

monitor 每轮每钱包要调 `get_trades`（成交检测）+ `get_open_orders`（Step 2/3）+ Data positions（止损）+ 每个待校验买单的订单簿；订单页每次刷新每钱包 `get_open_orders` + `are_orders_scoring`。单用户本地、钱包数少，可接受。订单页轮询从 5s 放宽到 10s，并加手动刷新按钮。`get_trades` 用 `after=水位` 限制返回量。

## 5. 组件设计

### 5.1 订单管理页

**后端**

- `GET /api/orders`（改造）：遍历所有启用钱包 → 每钱包用其 `PolymarketAPI` 调 `get_open_orders()` → 合并。对所有 `side=BUY` 的订单按钱包收集 `id`，每钱包调一次 `are_orders_scoring(buy_ids)`，把 scoring 状态合并进结果。返回字段：`wallet, order_id, market(condition_id), asset_id, side, outcome, price, original_size, size_matched, created_at, scoring`（买单 true/false，卖单 null）。
  - 钱包 API 来源：优先复用 `manager.engines[addr].api`；引擎未运行时按需用解密私钥临时构造（与 scanner 现有做法一致）。
  - 单钱包失败：跳过该钱包，返回体带 `errors: [{wallet, msg}]`，不让一个钱包拖垮整页。
- `POST /api/orders/cancel-batch`（改造）：入参 `[{order_id, wallet}, ...]`。按 wallet 分组，每组一次 `cancel_orders(ids)`。**不再写 DB**。
- `POST /api/orders/cancel-all-buys`（新增）：入参可选 `wallet`。目标钱包集合（无则全部启用钱包）各调 `get_open_orders()` 取 `side=BUY` 的 id，按钱包 `cancel_orders(buy_ids)`。只撤买单，保留止盈卖单。
- 旧 `/api/engine/cancel-all`（`_cancel_buy_orders` 逐笔撤）与单笔 `/api/orders/<id>/cancel` 统一改走 `cancel_orders` 路径。
- `GET /api/positions`（改造）：改读 Data API positions（`asset_id/size/avgPrice/curPrice`），不再读 `positions` 表。

**前端 `orders.html`**

- 表格列：钱包、市场、方向、Outcome、价格、原始数量、已成交、是否在奖励区间（买单 ✓/✗，卖单 —）、操作。
- 筛选：钱包（已有）+ 方向（全部/买/卖）+ 奖励区间（全部/在区间/不在区间）。
- 排序：点击列头按 价格 / 原始数量 / 时间 / scoring 升降序，纯客户端排序（数据已全量拉回）。
- 按钮：「一键撤买单」→ `/api/orders/cancel-all-buys`（带当前钱包筛选值）；「撤选中」→ `/api/orders/cancel-batch`；行内「撤单」→ 单 id 走 cancel-batch。
- 刷新：轮询 5s→10s + 「立即刷新」按钮；`errors` 非空时页面顶部红字提示哪个钱包失败。

### 5.2 monitor 重构（`engine/monitor.py`）

每轮（`fill_check_interval_sec`）对每个钱包按序跑三步，全部基于 API。逻辑与 IO 分离：核心判定抽成纯函数（输入为 API 返回数据），薄 IO 层负责调用与副作用。

**Step 1 — 成交检测（基于 get_trades，展平 maker_orders）+ 挂止盈 + 撤剩余（A）**
1. `get_trades(TradeParams(maker_address=<funder>, after=<该钱包成交水位>))` 拉成交。
2. **展平为「买单成交事件」**（纯函数 `select_new_buy_fills`）：遍历每个 trade，对其 `maker_orders[]` 中 `maker_address == funder` **且** `side == "BUY"` 的条目，产出事件 `{trade_id, order_id, asset_id, price, size=matched_amount, market=trade.market, ts=trade.match_time}`；剔除 `(trade_id, order_id)` 已在 `_seen_fill_keys` 的；按 `ts` 升序。
3. 对每个新买单成交事件：
   - `place_limit_sell(asset_id, price, size)` 挂止盈（卖价=该笔成交价；maker 限价买成交价即原挂单价，与现状语义一致）。
   - 写 DB `trades`（side=buy，market_id=event.market）；按 `event.market` 设 `cooldown`。
   - `cancel_orders([order_id])` 撤掉该买单未成交剩余部分（已结束/已撤则幂等无害）；同一轮同一 `order_id` 只发一次撤单。
   - 把 `(trade_id, order_id)` 加入 `_seen_fill_keys`；用 `event.ts` 推进该钱包成交水位 `after`。
4. 一个 trade 内可能含**多个**我们的 maker 买单条目、同一 `order_id` 也可能跨多个 trade 部分成交：去重粒度是 `(trade_id, order_id)`，每个事件独立挂对应止盈。
5. 我们的 `side=="SELL"` maker_order 条目（止盈卖单成交）本步**不处理**（持仓由 Step 2 基于 Data API 管理）；顶层 `side/size/price/asset_id` 一律忽略。
6. 不再读写 `orders`/`positions` 表，不再维护 `_last_matched`。

**Step 2 — 止损（A）**
1. Data API `GET /positions?user=<proxy>` 取该钱包持仓（`asset_id/size/avgPrice/curPrice`）。
2. 每持仓：`curPrice <= avgPrice * (1 - stop_loss_pct)` → 触发：
   - 在 `get_open_orders()` 结果里按 `asset_id` 找该持仓对应 SELL 挂单，`cancel_orders` 撤掉。
   - `place_market_sell(asset_id, size)` 市价平仓。
   - 写 DB `trades`（side=stop_loss，pnl=(curPrice-avgPrice)*size）。
3. 持仓与卖单配对完全靠 `asset_id`，不再依赖 `positions` 表 / `sell_order_id`。
4. Data positions 拉取失败 → 本轮跳过该钱包止损（不臆测成本价、不误平仓），下轮重试，记 warning。

**Step 3 — 策略合规检查 + 重挂/撤单（B）**
1. 对每个 `side=BUY` 且 `size_matched == 0` 的挂单：
   - 拉该 token 最新订单簿，按 `determine_order_price` 重算 `want`。
   - `want is None` → `cancel_orders([order_id])`，不重挂，记日志。
   - `want` 的 tick ≠ 当前挂单价 tick → `cancel_orders([order_id])` 后以 `want` + 原 size `place_limit_buy`；`_last_matched` 移除旧 id。
   - tick 相同 → 不动。
2. 仅处理 `size_matched == 0` 的纯未成交买单（部分成交的已在 Step 1 被撤）。

**顺序与安全**：Step 1 先于 Step 3，避免「刚成交还没挂止盈就被当不合规撤掉」。

**关联改动**：`engine/manager.py` 的 `check_existing_orders`（按 orderbook 重算 reward_range 撤单）与 `_cancel_buy_orders`（逐笔撤）被 Step 3 / 新撤单接口取代，删除或改为转调。`worker.place_orders`（分发挂单）下单前重拉订单簿 + 重跑 `determine_order_price`，不再用扫描缓存价。

### 5.3 API 包装层（`api/polymarket_api.py`）

- 复用已有 `get_open_orders()`、`get_trades()`、`cancel_orders()`、`cancel_all()`、`place_limit_*`。
- `get_trades()` 包装改为接受 `TradeParams`（`maker_address`/`after` 等），透传给 `client.get_trades`。**trade→买单的展平不放在 API 层，而是纯函数 `engine/fills.select_new_buy_fills(trades, funder, seen_keys)`**（已实测结构，见 §3）。不再需要 `extract_maker_order_id` 单字段工具。
- 新增 `are_orders_scoring(order_ids: list) -> dict`：包 `client.are_orders_scoring(OrdersScoringParams(orderIds=order_ids))`；空输入返回 `{}`；调用方用防御性 `.get(order_id)`（返回结构按 SDK 惯例 dict[order_id]->bool，唯一未现场验证项）。
- 新增 `get_user_positions(user_address: str) -> list`：HTTP GET Data API `positions?user=<funder>`，`raise_for_status` 后返回 list。**确认字段**：`asset`(=asset_id)、`size`、`avgPrice`、`curPrice`、`conditionId`(=market)、`outcome`、`title`。Step 2/Task 8 据此取值，不再有占位符。

## 6. 错误处理

- 每钱包、每订单的 API 调用独立 try/except；单点失败只跳过该项并记日志，不中断整轮 monitor / 整页订单。
- Data API positions 失败 → 该钱包本轮跳过止损，下轮重试。
- `cancel_orders` / `place_limit_buy` 失败 → 记日志，`_last_matched` 不变，下轮幂等重试。
- `place_limit_sell` 挂止盈失败 → 记 error，但**仍把 `(trade_id, order_id)` 加入 `_seen_fill_keys`、推进水位**（避免同一成交被反复挂卖造成重复卖出），靠下轮 Step 2 止损兜底（容错哲学：宁可漏挂止盈让止损兜底，也不重复卖）。
- `get_trades` 拉取失败 → 本轮跳过该钱包成交检测，水位不前移，下轮重试。

## 7. 测试策略

- 纯逻辑单测进 `tests/`（无网络）：`determine_order_price` 已有覆盖不动；新增对「`select_new_buy_fills`：给定真实结构的 trades 列表 + funder + seen_keys，展平 maker_orders 按 funder/BUY 过滤、(trade_id,order_id) 去重、按 ts 升序」「档位是否相同的重挂判定」「止损触发条件」的纯函数单测（用 spike 采到的真实 trade JSON 作为测试夹具）。
- monitor 三步拆为可独立测试的纯函数 + 薄 IO 层。
- API 包装层（`get_open_orders`/`are_orders_scoring`/Data positions/`cancel_orders`）不进 pytest，沿用手动脚本与 `test_real_order.py` 真实环境验证。

## 8. 风险与影响面

1. **`orders`/`positions` 表停用**：依赖这两表的 `/api/positions`、dashboard 统计、`startup_recovery` 需一并改造，否则读空。`/api/positions` 改读 Data API。
2. **Data API 依赖**：新引入 `data-api.polymarket.com/positions`，需确认 `user` 参数地址与现有 `funder` 一致；不可用时止损降级（跳过本轮）。
3. **churn**：严格档位比对、无容差，行情剧烈时同一买单可能每轮撤重挂；由轮询间隔限频，接受。
4. **trade 结构（已 spike 实测，模型已修正）**：成交在 `maker_orders[]`，须按 `maker_address==funder` 过滤，去重键 `(trade_id, order_id)`，量/价/asset 取 `maker_orders[i]` 而非顶层。原始 plan 假设（看顶层 `side/size`）已证实错误并据此重写 Task 1/4/5。残留风险：`maker_address` 大小写/校验和形式与本地 funder 不一致——比对时两边统一小写。
5. **positions 字段已由用户文档化样例确认**（`asset/size/avgPrice/curPrice/conditionId/...`），无需二次 spike。**`are_orders_scoring` 返回结构是唯一未现场验证项**：按 SDK 惯例 `dict[order_id]->bool` 防御实现（`.get`），返回非预期时 scoring 列显示 `?`、不阻断订单页与撤单；首次真实联调时核对。
5. **CLAUDE.md 文档同步**：「Balance re-read before every order」「startup_recovery」「stop=stop monitor」「Pipeline scan→strategy→place→monitor」等描述需随实现改写；实现计划阶段列出要同步修改的具体段落。

## 9. 验收标准

- 订单管理页展示的挂单与交易所 `get_open_orders()` 一致（含测试脚本挂的单）；买单显示是否在奖励区间。
- 「一键撤买单」只撤买单、保留卖单；「撤选中」按钱包分组批量撤成功。
- monitor：`get_trades` 检测到买单新成交后自动挂止盈卖单并撤掉该单剩余（同一 trade 不重复处理，离线期间成交重启后能补处理）；持仓按 Data API `avgPrice` 触发止损；未成交买单档位偏离即撤了重挂，无法合规则撤单。
- DB 不再写 `orders`/`positions`；`trades` 历史与 `cooldown` 仍正常。
- 纯逻辑单测通过 `pytest`。
