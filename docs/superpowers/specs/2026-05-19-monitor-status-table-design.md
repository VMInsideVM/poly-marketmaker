# 结构化"监控状态"页设计（替换原运行日志）

日期：2026-05-19

## 背景与目标

刚上线的"运行日志"页是原始文本流，混入 HTTP/库噪声，可读性差。用户要的是**按订单**的友好表格：每个当前在挂单，本轮被监控 Step 1/2/3 怎么检查、判定与结果如何。决策：用结构化表格页**替换**原运行日志页，并**移除**原始日志缓冲那套基础设施。

现状：`WalletWorker._run`（`engine/manager.py`）每个 tick 依次调 `monitor.check_buy_orders()`（Step 1 成交检测→`_handle_fill`）、`check_stop_loss()`（Step 2→`_check_pos_sl`）、`check_sell_orders()`（Step 3 逐买单→`_check_compliance`），再 `wait(fill_check_interval_sec)`。`utils/log_buffer.py` 的 `BufferLogHandler` 注册在 `app.py` root logger，`/api/logs`、`/api/logs/clear` + `web/templates/logs.html`（路由 `logs_page` → `/logs`）。

## 行为决策（已与用户确认）

- 用结构化表格页替换原运行日志页（同路由 `/logs`、同函数名 `logs_page`，导航标签改为"监控状态"）。
- 组织方式：**当前快照**，每个当前在挂订单一行，每个 tick **覆盖**刷新（无历史/事件流）。
- 列：`时间 · 钱包 · 市场 · 方向 · 价格 · 数量 · 已成交 · 阶段 · 判定/动作 · 详情/原因`。
- 一并**移除**原始日志缓冲设施（`utils/log_buffer.py`、`BufferLogHandler` 注册、`/api/logs`、`/api/logs/clear`、`tests/test_log_buffer.py`）。`market_maker.log` 文件与各模块现有 `logger.*` 调用（含 Step 3 详细日志）全部保留。

## 组件设计

### 1. 共享快照存储 `engine/monitor_status.py`（新文件）

线程安全（模块级 dict + `threading.Lock`），纯逻辑、可单测：

- `set_snapshot(wallet: str, rows: list[dict], ts: float) -> None`：以 `wallet` 为键**覆盖**写入 `{"ts": ts, "rows": rows}`。
- `get_snapshot() -> dict`：返回 `{"updated": <所有钱包 ts 的最大值，无则 0>, "rows": [...]}`，`rows` 为所有钱包行合并（按 `ts` 再按 `wallet` 稳定排序，新→旧或保持插入顺序，前端可再排）。
- `clear_snapshot() -> None`：清空（供测试隔离用）。

行 dict 字段约定（所有值均为前端可直接渲染的字符串/数字，组装时格式化好）：

```
{
  "ts": float,            # 本轮 tick 时间（unix 秒）
  "wallet": str,          # 钱包地址（全址；前端缩写显示）
  "market": str,          # 市场显示名（title 优先，无则 condition_id）
  "side": str,            # "买入" / "卖出"
  "price": str,           # 例 "0.4000"
  "size": str,            # original_size
  "matched": str,         # size_matched
  "stage": str,           # "Step1" / "Step2" / "Step3" / "止盈卖单" / "—"
  "action": str,          # 判定/动作，例 "replace→撤并重挂" / "keep" /
                          #   "cancel→撤单不重挂" / "成交→挂止盈+撤余" /
                          #   "止损→市价平仓" / "跳过(取不到max_spread)" /
                          #   "跳过(盘口为空)" / "挂单中"
  "detail": str,          # 详情/原因，例 "mid0.50 ms3 区间[.47,.53] 应挂0.48"
}
```

### 2. `OrderMonitor` 旁路记录（`engine/monitor.py`）

新增**只记录、不改决策**的旁路逻辑：

- `__init__`：`self._status_rows: list = []`、`self._tick_ts: float = 0.0`。
- `begin_status_tick()`：`self._status_rows = []`；`self._tick_ts = time.time()`。
- `_status_add(**fields)`：把一行（补全 `ts=self._tick_ts`、`wallet=self.wallet_address`）追加进 `self._status_rows`。
- `publish_status()`：`monitor_status.set_snapshot(self.wallet_address, self._status_rows, self._tick_ts)`。
- 在各决策点调用 `_status_add(...)`，**紧邻现有 `logger.*`，不改变控制流**：
  - Step 1 `_handle_fill`：成交并挂止盈/撤余后 → `stage="Step1", action="成交→挂止盈+撤余", detail=f"成交{size} 止盈卖{price}"`。
  - Step 2 `_check_pos_sl`：触发止损执行平仓后 → `stage="Step2", action="止损→市价平仓", detail=f"cur{cur}<avg{avg} 触发"`（side 记"卖出"）。
  - Step 3 `_check_compliance` 各分支：空盘口跳过 / 取不到 max_spread 跳过 / keep / replace / cancel —— 每分支一行，`detail` 含已算出的 mid·max_spread·区间·应挂价（取不到/空盘口时填对应原因）。
  - Step 3 `check_sell_orders` 遍历时，对**非买单或部分成交**等不进入 `_check_compliance` 的在挂单也补一行（如 `side="卖出", stage="止盈卖单", action="挂单中"`），保证"每个当前在挂单一行"。
- 字段格式化（价格 `.4f` 等）在 `_status_add` 调用处完成；`market` 取订单 `title` 否则 `market`(condition_id) 缩写交由前端。

### 3. `WalletWorker._run` tick 边界（`engine/manager.py`）

`_run` 循环体当前为：
```
self.monitor.check_buy_orders()
self.monitor.check_stop_loss()
self.monitor.check_sell_orders()
self._stop_event.wait(timeout=check_interval)
```
改为（仅加首尾两行，不改三步调用顺序与行为）：
```
self.monitor.begin_status_tick()
self.monitor.check_buy_orders()
self.monitor.check_stop_loss()
self.monitor.check_sell_orders()
self.monitor.publish_status()
self._stop_event.wait(timeout=check_interval)
```

### 4. 路由（`web/routes.py`）

- 新增 `GET /api/monitor-status`（`@login_required`）→ `jsonify(get_snapshot())`。
- **删除** `GET /api/logs`、`POST /api/logs/clear` 及其 `from utils.log_buffer import get_logs, clear_logs`，改为 `from engine.monitor_status import get_snapshot`。
- `/logs` 路由与 `logs_page` 函数保留（仅模板内容变）。

### 5. 页面（`web/templates/logs.html` 重写）

- 轮询 `GET /api/monitor-status` 每 4 秒，渲染一张表。
- 表头：`时间 · 钱包 · 市场 · 方向 · 价格 · 数量 · 已成交 · 阶段 · 判定/动作 · 详情/原因`。
- 钱包缩写 `0x前6..后4`（`title` 全址）；时间 `ts*1000` → `toLocaleString('zh-CN')`。
- "判定/动作"按关键字配色：含 `replace`/`cancel`/`止损`/`跳过` → 警示色；`keep`/`挂单中` → 常态；`成交` → 正向。
- 顶部："最后更新：<updated 时间>" + 手动"刷新"按钮；`rows` 为空显示"暂无在挂订单或监控未运行"。
- 全中文；`escapeHtml` 转义所有动态文本。

### 6. `app.py` / 旧设施移除

- 删 `from utils.log_buffer import BufferLogHandler` 与 `handlers` 列表里的 `BufferLogHandler()`（保留 `StreamHandler`、`FileHandler`）。
- 删文件 `utils/log_buffer.py`、`tests/test_log_buffer.py`。
- `base.html` 导航：`运行日志` 文案改 `监控状态`（`url_for('logs_page')` 不变）。

## 数据流

`WalletWorker._run` tick → `begin_status_tick()` → Step1/2/3 执行（决策不变）同时 `_status_add` 旁路记录 → `publish_status()` → `monitor_status.set_snapshot(wallet,...)`（覆盖该钱包）→ 前端轮询 `/api/monitor-status` → `get_snapshot()` 合并所有钱包 → 表格渲染。订单成交/撤销后下个 tick 自然从快照消失（当前快照语义，用户已接受）。

## 错误处理

- `monitor_status` set/get 在锁内，`get_snapshot` 永远返回 `{"updated":0,"rows":[]}` 之类的合法结构（无数据时）。
- `_status_add`/`publish_status` 自身 `try/except` 包裹，**绝不**影响 Step1/2/3 主流程（记录失败只记 `logger.warning`，不抛）。
- `/api/monitor-status` 即使监控未运行也返回空结构（200）。

## 测试

- `tests/test_monitor_status.py`（纯逻辑）：单钱包 set→get 往返；同钱包二次 set **覆盖**非追加；多钱包合并 + `updated` 取最大 ts；空时返回 `{"updated":0,"rows":[]}`；`clear_snapshot` 生效。
- 扩 `tests/test_monitor.py`：沿用现有 mock，一轮 `check_sell_orders`（配合 `begin_status_tick`/`publish_status`）后断言该钱包快照含每个在挂单的行且 `stage`/`action` 正确（Step3 keep/replace/cancel/空盘口跳过/取不到max_spread跳过）；`_handle_fill` 产出 Step1 行；`_check_pos_sl` 触发止损产出 Step2 行。
- 删除 `tests/test_log_buffer.py`；全套测试仍全绿。
- 现有 Step1/2/3 行为用例不回归（旁路记录不改既有断言对象/行为）。

## 不做（YAGNI）

- 不做历史/事件流（已选当前快照覆盖）。
- 不落 DB；不做服务端排序/分页/导出。
- 不保留原始日志缓冲页/接口。
- 不改任何下单/撤单/止盈/止损决策；`market_maker.log` 与现有 `logger.*` 全保留。
