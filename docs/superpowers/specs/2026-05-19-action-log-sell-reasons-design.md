# 操作记录持久化 + 历史记录卖单理由 + 监控状态钱包筛选 设计

日期：2026-05-19

## 背景与问题

用户反馈："历史记录里凡是挂了卖单的逻辑都要列出来，说明为什么挂卖单、为什么挂这个价格、信息来源在哪里；监控状态要能像历史记录那样切换钱包；监控状态下要能看到每一个动作；撤单/调整订单等行为要被存储并支持查询；没有涉及具体调整/撤单的就按原样显示。"

### 已查清的根本原因（驱动本设计）

排查"一笔止损卖出在历史里显示成买入"时确认：

1. `trades` 表**不是** `get_trades`（GetTradeForUserOrMarket）的镜像。全仓只有两处 `record_trade`（`engine/monitor.py:105`、`:178`），没有任何地方读取 trade 的 `Side` 字段来决定方向。
2. `get_trades` 只用于**检测我们挂的买单是否成交**：`engine/fills.py:28` 的 `select_new_buy_fills` 显式只挑 `maker_orders` 中 `side == "BUY"` 的条目，其余 `continue`，SELL 信息在此被丢弃。
3. 检测到买单成交后，`monitor.py:105` **写死** `side="buy"`，从不读 Side。
4. 三种卖出归宿：止盈限价卖单**完全未写入** `trades`（缺口）；止损市价卖单以 `side="stop_loss"` 记录、前端显示"止损"；普通卖出无独立记录路径。

所以用户看到的"买入"行是**买单成交本身**的正确记录，并不代表那次卖出；卖出要么是另一行"止损"、要么（止盈卖单）根本缺失。

## 决策（已与用户确认）

- 数据模型：**新建 `actions` 表**，不动 `trades` 结构。
- 范围扩展：**主表也补记止盈卖单**——Step1 在挂出止盈限价卖单后，除现有 `record_trade(side="buy")` 外，新增 `record_trade(side="sell")`，使主历史表方向列完整（买入/卖出/止损齐全）。止损仍记 `side="stop_loss"` 显示"止损"（不在本次改动范围）。
- 历史记录页：保留现有交易历史表与筛选栏不动；其下新增"卖单理由"区。
- 监控状态页：上下两区——上区为现有实时快照 + 新增钱包下拉；下区为持久化"操作记录"，按钱包/日期可查。
- 未涉及调整/撤单的状态（keep / 盘口为空 / 取不到 max_spread / 部分成交 / 止盈卖单挂单中）**不持久化**，仍只走内存快照"按原样显示"。

## 组件设计

### 1. 数据模型 `models/database.py`

在 `_create_tables` 的 `executescript` 内追加（`CREATE TABLE IF NOT EXISTS` 对新库与旧库均自动建表，`_migrate` 无需改——该 ALTER 模式仅用于给已有表加列）：

```sql
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    market_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL DEFAULT -1,
    size REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    price_basis TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
```

`action_type` 取值：`take_profit_sell` / `cancel_remainder` / `stoploss_cancel_sell` / `stoploss_market_sell` / `step3_cancel_old` / `step3_replace_new` / `step3_cancel_nocompliant`。`side` ∈ {`买入`,`卖出`,`-`}。纯撤单 `price = -1`。

新增方法（紧随 `record_trade` / `get_trade_history` 之后，沿用其写法）：

```python
def record_action(self, wallet, market_id, action_type, side, price, size,
                   reason, price_basis):
    c = self.conn.cursor()
    c.execute(
        """INSERT INTO actions (wallet, market_id, action_type, side,
           price, size, reason, price_basis)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (wallet, market_id, action_type, side, price, size,
         reason, price_basis),
    )
    self.conn.commit()

def get_actions(self, wallet=None, start=None, end=None, action_types=None):
    c = self.conn.cursor()
    query = "SELECT * FROM actions WHERE 1=1"
    params = []
    if wallet:
        query += " AND wallet = ?"; params.append(wallet)
    if start:
        query += " AND created_at >= ?"; params.append(start)
    if end:
        query += " AND created_at <= ?"; params.append(end)
    if action_types:
        query += " AND action_type IN (%s)" % ",".join("?" * len(action_types))
        params.extend(action_types)
    query += " ORDER BY created_at DESC"
    c.execute(query, params)
    return [dict(row) for row in c.fetchall()]
```

### 2. 主表补记止盈卖单 + 操作埋点 `engine/monitor.py`

新增私有助手（try/except 包裹，绝不打断 Step1/2/3，与 `_status_add` 一致；只在对应 API 调用成功后调用）：

```python
def _record_action(self, market_id, action_type, side, price, size,
                    reason, price_basis):
    try:
        self.db.record_action(
            wallet=self.wallet_address, market_id=market_id,
            action_type=action_type, side=side, price=price, size=size,
            reason=reason, price_basis=price_basis,
        )
    except Exception as e:
        logger.warning("record_action failed: %s", e)
```

各埋点的 `reason` / `price_basis`（简体中文）：

**Step1 `_handle_fill`**（现有 `place_limit_sell` → `record_trade(buy)` → `set_cooldown` → 撤余）：
- 在 `record_trade(side="buy", ...)` 之后**新增主表补记**：
  `record_trade(wallet=..., market_id=market_id, market_name="", side="sell", price=price, size=size, pnl=0.0)`
  （止盈卖单按成交价等价挂出，placement 时 pnl 未知记 0.0，与现有"买单成交即记 buy/pnl=0"语义一致；`history.html` 的 `sideLabels` 已含 `sell:'卖出'`，前端无需改）
- `take_profit_sell`：side `卖出`，price=`price`，size=`size`
  reason：`买单成交，按成交价挂等价止盈卖单（赚流动性奖励，原价卖出不亏本金）`
  price_basis：`f"卖价=买入成交价 {price:.4f}；来源：CLOB get_trades 的 maker_orders 成交价"`
- `cancel_remainder`（仅当 `order_id and order_id not in cancelled_orders` 且 cancel 成功后）：side `-`，price=-1，size=`size`
  reason：`该买单已成交，撤销同一买单剩余未成交量，避免超买`
  price_basis：`f"撤 order_id={order_id}；撤单操作无价格"`

**Step2 `_check_pos_sl`**（现有触发判定 → 撤 SELL → `place_market_sell` → `record_trade(stop_loss)`）：
- `stoploss_cancel_sell`（仅当 `sell_ids` 非空且 `cancel_orders` 成功后）：side `-`，price=-1，size=`size`
  reason：`触发止损，先撤该持仓全部止盈卖单以便市价平仓`
  price_basis：`f"撤 {len(sell_ids)} 笔 SELL；来源：CLOB get_open_orders（asset={asset_id} 的 SELL）"`
- `stoploss_market_sell`（`place_market_sell` 之后）：side `卖出`，price=`cur`，size=`size`
  reason：`f"现价 {cur:.4f} 跌破成本价 {avg:.4f} 的止损阈值 avg×(1-止损比例{settings['stop_loss_pct']}%)，市价平仓止损"`
  price_basis：`f"成本价 avgPrice={avg:.4f}、现价 curPrice={cur:.4f}；来源：Polymarket Data API /positions"`

**Step3 `_check_compliance`**（现有 keep/replace/cancel）：
- 仅在 `action != "keep"` 且 `cancel_orders([o["id"]])` 成功后记 `step3_cancel_old`（replace 路径）或 `step3_cancel_nocompliant`（cancel 路径，want is None）：side `-`，price=-1，size=`int(float(o.get("original_size",0) or 0))`
  - `step3_cancel_old` reason：`f"挂单价 {float(o.get('price',0) or 0):.4f} 不在最新奖励区间内，撤旧买单准备重挂"`
  - `step3_cancel_nocompliant` reason：`奖励区间内无合规价，撤该买单（不重挂）`
  - 两者 price_basis：`f"旧价 {float(o.get('price',0) or 0):.4f}；区间[{rmin:.4f},{rmax:.4f}] mid{midpoint:.4f} ms{max_spread} tick{tick:.4f}；来源：CLOB get_orderbook + get_rewards_for_market"`
- replace 路径 `place_limit_buy(...)` 之后记 `step3_replace_new`：side `买入`，price=`want`，size=`size`
  reason：`按策略在奖励区间内重挂买单（贴最优买价深度，最大化奖励占比）`
  price_basis：`f"应挂价 {want:.4f}=determine_order_price(bids, ms{max_spread}, tick{tick:.4f}, 区间[{rmin:.4f},{rmax:.4f}])；来源：CLOB get_orderbook + get_rewards_for_market"`
- **不持久化**：keep、盘口为空、取不到 rewards_max_spread、部分成交、止盈卖单"挂单中"——仅保留现有 `_status_add` 内存快照行。

时序要点：每个 `_record_action` 必须放在对应 API 调用**成功之后**；若 Step3 cancel 成功但 `place_limit_buy` 抛错，仍记 `step3_cancel_old`（撤单确实发生），新挂失败按现状仅日志。

### 3. 路由 `web/routes.py`

新增（紧邻 `/api/history`，复用其参数解析）：

```python
@app.route("/api/actions", methods=["GET"])
@login_required
def api_get_actions():
    wallet = request.args.get("wallet")
    start = request.args.get("start", type=float)
    end = request.args.get("end", type=float)
    types = request.args.get("types")
    action_types = types.split(",") if types else None
    return jsonify(db.get_actions(wallet, start, end, action_types))
```

不动 `/api/history`、`/api/monitor-status`、`/api/wallets`。

### 4. 前端

**`web/templates/history.html`**：现有交易历史表与筛选栏完全不动。其下新增：

```html
<h2>卖单理由</h2>
<table class="data-table">
  <thead><tr>
    <th>时间</th><th>钱包</th><th>市场</th><th>动作</th>
    <th>价格</th><th>原因</th><th>价格依据/来源</th>
  </tr></thead>
  <tbody id="sell-reasons-body"></tbody>
</table>
```

`refreshHistory()` 内追加调用 `refreshSellReasons()`，复用同一钱包/日期筛选值，请求 `/api/actions?types=take_profit_sell,stoploss_market_sell`（只列"挂卖单"逻辑——Step3 重挂是买单，不入此区）。`action_type → 中文`映射：`take_profit_sell→止盈挂卖单`、`stoploss_market_sell→止损市价卖`。price 为 -1 时显示 `-`。

**`web/templates/logs.html`（监控状态）**：

- **上区**：现有实时快照表保留 + 顶部新增钱包下拉（结构与 history 一致，从 `/api/wallets` 填充）。`refreshStatus()` 渲染时按选中钱包对 `data.rows` 做客户端过滤（`r.wallet`）。保留 4s 轮询。
- **下区**：新增 `<h2>操作记录</h2>` + 开始/结束日期输入 + 表（列：时间·钱包·市场·动作·方向·价格·数量·原因·价格依据/来源），数据来自 `/api/actions`（**全部** action_type），受同一钱包下拉 + 日期筛选；筛选变化时刷新，并随 4s tick 一并刷新（接口轻量、本地单用户）。`action_type` 全量中文映射表见实现。

## 数据流

监控 tick：Step1 检测买单成交 → `place_limit_sell` → `record_trade(buy)` + **`record_trade(sell)`**（主表）+ `_record_action(take_profit_sell)`；撤余成功 → `_record_action(cancel_remainder)`。Step2 止损触发 → 撤 SELL 成功 `_record_action(stoploss_cancel_sell)` → `place_market_sell` → `record_trade(stop_loss)` + `_record_action(stoploss_market_sell)`。Step3 非 keep 且撤成功 → `_record_action(step3_cancel_old|step3_cancel_nocompliant)`；replace 重挂成功 → `_record_action(step3_replace_new)`。前端：历史页主表读 `/api/history`（现含 buy/sell/stop_loss），卖单理由区读 `/api/actions?types=...`；监控页上区读 `/api/monitor-status` 客户端按钱包过滤，下区读 `/api/actions` 按钱包/日期查。

## 错误处理

- `_record_action` / 主表补记 `record_trade(sell)` 失败仅 `logger.warning`，绝不打断交易决策（与 `_status_add` 同级）。
- `actions` 写入在 API 成功后，失败的下单/撤单不会产生误导记录。
- `get_actions` 无结果返回空列表，前端渲染空表。

## 测试

- `tests/test_database.py`：`record_action` 落库；`get_actions` 按 wallet / start / end / action_types 各维度筛选；默认按 created_at DESC。
- `tests/test_monitor.py`：
  - **既有用例回归修正**：原成交用例 `db.record_trade.assert_called_once()` 改为断言**两次**调用——一次 `side="buy"`、一次 `side="sell"`（price/size 同成交值，sell 的 pnl=0.0）。
  - 新增：成交 → `record_action` 含 `take_profit_sell`（+ `cancel_remainder`，当有 order_id 且撤成功）；止损 → `stoploss_cancel_sell`（有 SELL 时）+ `stoploss_market_sell`；Step3 replace → `step3_cancel_old` + `step3_replace_new`；Step3 want=None → `step3_cancel_nocompliant`；并断言 keep / 盘口为空 / 取不到 max_spread / 部分成交 **不**调用 `record_action`。
  - 断言关键 `reason` / `price_basis` 子串（如止损含 `成本价 avgPrice=`、来源含 `Data API`）。
- `web/routes`：`/api/actions` 参数透传（types 拆分、wallet/start/end）。
- 前端纯 HTML/JS，沿用本仓库无 UI 测试惯例：`python -c "import web.routes"` + `python -m pytest -q` 全绿 + 人工核对。

## 不做（YAGNI）

- 不改 `trades` 表结构、不改 `/api/history`、不改止损在主表的 `side="stop_loss"`/前端"止损"显示。
- 不持久化 no-op 快照行。
- 无分页、无操作记录的编辑/删除、无新增 settings。
- 不改 `engine/fills.py`、`engine/scanner.py`、`engine/strategy.py`、`monitor_status.py` 结构。
