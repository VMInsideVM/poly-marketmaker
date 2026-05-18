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
| 成交后处理 | `new_fill > 0` 时除挂止盈卖单外，撤掉**该笔买单本身**的未成交剩余部分 |
| 分发挂单对齐 | 下单前重拉订单簿 + 重跑 `determine_order_price`，不再用扫描缓存价 |

## 3. SDK / API 能力确认（py_clob_client_v2 1.0.1）

- `get_open_orders(params=None)`：自动翻页，返回扁平订单对象列表，字段含 `id, status, side, market, asset_id, original_size, size_matched, price, outcome, created_at` 等（与用户提供的样例 JSON 一致）。
- `are_orders_scoring(OrdersScoringParams(orderIds=[...]))`：批量查订单是否在奖励区间（scoring）。`is_order_scoring(OrderScoringParams(orderId))`：单查。
- `cancel_orders(order_hashes: list)`：一次请求批量撤多笔。`cancel_all()`：撤该钱包全部。`cancel_order(payload)`：单撤。
- Polymarket Data API（非 CLOB SDK，需独立 HTTP）：`GET https://data-api.polymarket.com/positions?user=<address>` 返回持仓，含 `asset_id, size, avgPrice, curPrice` 等。`user` 参数为钱包的 proxy / funder 地址（与现有 `funder` 一致，实现时需验证）。

## 4. 架构总览

### 4.1 数据源映射（改造后）

| 数据 | 来源 | 替代的旧逻辑 |
|---|---|---|
| 钱包当前挂单 | CLOB `get_open_orders()`（每钱包，自动翻页） | `db.get_open_orders()` 读 `orders` 表 |
| 买单是否在奖励区间 | CLOB `are_orders_scoring(buy_ids)` 批量 | 无（新增） |
| 持仓与成本价 | Data API `GET /positions?user=<proxy>`：`avgPrice`/`size`/`curPrice` | `positions` 表 |
| 成交检测 | `get_open_orders()` 中买单 `size_matched` 增量（内存快照对比） | DB `orders` 表 + `_last_matched` |
| 成交/止盈/止损历史 | 仍写 DB `trades` 表 | 不变 |
| 冷却 | 仍用 DB `cooldown` | 不变 |

### 4.2 DB 角色收缩

`orders`、`positions` 表保留建表语句（便于回滚）但读写路径全部切走，不再驱动任何逻辑。DB 仅剩：`trades`（历史展示）、`cooldown`（防同市场重复挂单）、钱包/设置/密码等配置表。

### 4.3 重启自愈

`startup_recovery` 重写为「从 API 对账」：拉每钱包 `get_open_orders()` + Data API positions，把内存 `_last_matched[order_id]` 用当前 `size_matched` 初始化（避免把重启前已成交量误判为新成交）。不再依赖 DB 里的陈旧挂单/持仓。

### 4.4 已知代价

monitor 每轮、订单页每次刷新都要多次调 API（每钱包：`get_open_orders` + `are_orders_scoring` + Data positions）。单用户本地、钱包数少，可接受。订单页轮询从 5s 放宽到 10s，并加手动刷新按钮。

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

**Step 1 — 成交检测 + 挂止盈 + 撤剩余（A）**
1. `get_open_orders()` 取 `side=BUY` 的单。
2. `new_fill = 当前 size_matched - _last_matched.get(order_id, 0)`。
3. `new_fill > 0`：
   - `place_limit_sell(asset_id, 该买单 price, new_fill)` 挂止盈（卖价=买单挂单价，与现状一致）。
   - 写 DB `trades`（side=buy）；设该市场 `cooldown`。
   - `cancel_orders([order_id])` 撤掉该买单未成交剩余部分。
   - 从 `_last_matched` 移除该 order_id（该单已结束）。
4. 不再读写 `orders`/`positions` 表。

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

- 复用已有 `get_open_orders()`、`cancel_orders()`、`cancel_all()`、`place_limit_*`。
- 新增 `are_orders_scoring(order_ids: list) -> dict`：包 `client.are_orders_scoring(OrdersScoringParams(orderIds=order_ids))`。
- 新增 `get_user_positions() -> list`：HTTP GET Data API `positions?user=<funder/proxy>`，返回 `[{asset_id, size, avgPrice, curPrice, ...}]`。需确认 `user` 用钱包的哪个地址（与 `funder` 一致性校验）。

## 6. 错误处理

- 每钱包、每订单的 API 调用独立 try/except；单点失败只跳过该项并记日志，不中断整轮 monitor / 整页订单。
- Data API positions 失败 → 该钱包本轮跳过止损，下轮重试。
- `cancel_orders` / `place_limit_buy` 失败 → 记日志，`_last_matched` 不变，下轮幂等重试。
- `place_limit_sell` 挂止盈失败 → 记 error 但仍更新 `_last_matched`（避免同一成交被重复计为 new_fill 反复挂卖），靠下轮 Step 2 止损兜底。

## 7. 测试策略

- 纯逻辑单测进 `tests/`（无网络）：`determine_order_price` 已有覆盖不动；新增对「成交增量计算」「档位是否相同的重挂判定」「止损触发条件」的纯函数单测（API 返回作为入参传入）。
- monitor 三步拆为可独立测试的纯函数 + 薄 IO 层。
- API 包装层（`get_open_orders`/`are_orders_scoring`/Data positions/`cancel_orders`）不进 pytest，沿用手动脚本与 `test_real_order.py` 真实环境验证。

## 8. 风险与影响面

1. **`orders`/`positions` 表停用**：依赖这两表的 `/api/positions`、dashboard 统计、`startup_recovery` 需一并改造，否则读空。`/api/positions` 改读 Data API。
2. **Data API 依赖**：新引入 `data-api.polymarket.com/positions`，需确认 `user` 参数地址与现有 `funder` 一致；不可用时止损降级（跳过本轮）。
3. **churn**：严格档位比对、无容差，行情剧烈时同一买单可能每轮撤重挂；由轮询间隔限频，接受。
4. **CLAUDE.md 文档同步**：「Balance re-read before every order」「startup_recovery」「stop=stop monitor」「Pipeline scan→strategy→place→monitor」等描述需随实现改写；实现计划阶段列出要同步修改的具体段落。

## 9. 验收标准

- 订单管理页展示的挂单与交易所 `get_open_orders()` 一致（含测试脚本挂的单）；买单显示是否在奖励区间。
- 「一键撤买单」只撤买单、保留卖单；「撤选中」按钱包分组批量撤成功。
- monitor：买单成交后自动挂止盈卖单并撤掉该单剩余；持仓按 Data API `avgPrice` 触发止损；未成交买单档位偏离即撤了重挂，无法合规则撤单。
- DB 不再写 `orders`/`positions`；`trades` 历史与 `cooldown` 仍正常。
- 纯逻辑单测通过 `pytest`。
