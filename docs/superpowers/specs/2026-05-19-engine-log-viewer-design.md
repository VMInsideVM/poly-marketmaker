# 运行日志（Engine Log Viewer）设计

日期：2026-05-19

## 背景与目标

终端用户为非技术人员（双击 exe 运行），无法 tail `market_maker.log`。需要一个前端"运行日志"页，实时看到引擎在做什么——尤其是 **Step 3 对每个在挂买单是怎么检查的、判定与处理结果**。

现状：日志用 Python `logging`，`app.py` 的 `logging.basicConfig` 配置 INFO 级 + `StreamHandler` + `FileHandler("market_maker.log", utf-8)`；`scanner`/`monitor`/`manager` 均用 `logging.getLogger(__name__)`。无任何前端日志入口。Step 3 仅在执行后记 `Replaced/Cancelled` 结果，未记每单检查输入与判定细节。

## 组件设计

### 1. 内存环形缓冲 Handler（新文件 `utils/log_buffer.py`）

- `config.py` 新增顶层常量 `LOG_BUFFER_SIZE = 1000`（与 `DB_PATH`/`HOST`/`PORT` 同级；改值改这一行，非设置页）。
- `utils/log_buffer.py`：
  - 模块级单例 `_BUFFER: collections.deque`，`maxlen=LOG_BUFFER_SIZE`（从 `config` 导入）。
  - `class BufferLogHandler(logging.Handler)`：`emit(record)` 中 `self.format(record)` 后，向 `_BUFFER` append 一条 dict `{"ts": record.created, "level": record.levelname, "logger": record.name, "message": record.getMessage()}`。`emit` 内整体 `try/except`（遵循 logging handler 不得抛出的约定，用 `self.handleError(record)`）。
  - `get_logs() -> list[dict]`：返回 `list(_BUFFER)`（时间升序，最旧在前）。
  - `clear_logs() -> None`：`_BUFFER.clear()`。
  - 线程安全：`logging.Handler.emit` 由 logging 框架加锁调用；`deque.append`/`clear`/`list()` 在 CPython 下原子，足够本单进程单用户场景。

### 2. 在 `app.py` 注册 Handler

`app.py` 现有：

```python
logging.basicConfig(
    level=logging.INFO,
    ...,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("market_maker.log", encoding="utf-8"),
    ],
)
```

在 `handlers=[...]` 列表追加一个 `BufferLogHandler()` 实例（与另两个并列，挂 root logger）。因引擎模块日志向上传播到 root，**所有引擎日志自动入缓冲**，无需逐处改调用。给该 handler 设与现有一致的 `Formatter`（沿用 `basicConfig` 的 format；`message` 字段单独取 `record.getMessage()`，`ts`/`level`/`logger` 取 record 属性，不依赖 format 串）。

### 3. 路由（`web/routes.py`）

- `GET /logs`（`@login_required`）：`render_template("logs.html")`。
- `GET /api/logs`（`@login_required`）：`return jsonify(get_logs())` —— 列表，每项 `{ts, level, logger, message}`，时间升序。
- `POST /api/logs/clear`（`@login_required`）：`clear_logs(); return jsonify({"ok": True})`。仅清内存缓冲，不动 `market_maker.log`。

### 4. 前端页面（`web/templates/logs.html` + `base.html` 导航）

- `base.html` 导航 `nav-links` 内，在"历史记录"后加：
  `<a href="{{ url_for('logs_page') }}" class="{% if request.endpoint == 'logs_page' %}active{% endif %}">运行日志</a>`
  （路由函数名 `logs_page`，`/logs`）。
- `logs.html` 继承 `base.html`，含：
  - 级别筛选下拉（全部 / INFO / WARNING / ERROR），客户端过滤。
  - "清空"按钮 → `POST /api/logs/clear` 后刷新。
  - 日志区：每条一行，显示本地时间（`ts*1000` 转 `toLocaleString('zh-CN')`）、级别（按 INFO/WARNING/ERROR 配色）、message。
  - 自动轮询 `/api/logs` 每 4 秒；渲染后自动滚到底部。
  - 文案全部 简体中文。

### 5. Step 3 逐单详细日志（`engine/monitor.py` `_check_compliance` / `check_sell_orders`）

每个被检查的在挂买单产出一条 INFO 日志，**在判定后、执行撤/挂前**组装并打印（含动作意图）；保留现有执行后 `Replaced/Cancelled` 日志，两者互补。格式（中文，单条）：

- 正常 replace：
  `[Step3] 单 {id} 市场 {cid} 现价 {price} | 盘口 bid {best_bid} ask {best_ask} mid {mid} tick {tick} | max_spread={ms} 区间[{rmin},{rmax}] | 应挂价 {want} | 判定 replace → 撤单并重挂 {want}`
- keep：同上但 `… | 判定 keep → 保持不动`
- cancel（want 为 None）：`… 应挂价 无 | 判定 cancel → 撤单不重挂`
- 取不到 max_spread（`_market_max_spread` 返回 None）：
  `[Step3] 单 {id} 市场 {cid} 现价 {price} | 取不到 rewards_max_spread，本轮跳过（不撤不重挂）`
- 空盘口（bids/asks 为空）：
  `[Step3] 单 {id} 市场 {cid} | 盘口为空，本轮跳过`

数值格式化：价格类 `.4f`，`ms` 整数。当前空盘口分支在取 max_spread 之前 `return`，需在该 `return` 前补这条 skip 日志；取不到 max_spread 的 `return` 前补对应 skip 日志。其余引擎活动（扫描、挂单、Step1 成交、Step2 止损）沿用现有 `logger.info/warning`（已被缓冲 handler 捕获），不大改；仅当某引擎动作确无任何 INFO 日志时补一条。

## 数据流

引擎模块 `logger.info(...)` → 传播到 root → 三 handler（控制台 / 文件 / `BufferLogHandler`）→ `_BUFFER` deque。前端 `/logs` 页轮询 `GET /api/logs` → `get_logs()` 返回 deque 快照 → 渲染。`POST /api/logs/clear` → `clear_logs()`。

## 范围 / 不改动项

- 新增：`utils/log_buffer.py`、`web/templates/logs.html`、`tests/test_log_buffer.py`；`config.py` 加常量 `LOG_BUFFER_SIZE`。
- 改动：`app.py`（注册 handler）、`web/routes.py`（3 个路由）、`web/templates/base.html`（导航项）、`engine/monitor.py`（Step 3 逐单日志 + 两个 skip 分支补日志）、`tests/test_monitor.py`（新增日志断言）。
- **不改** `determine_order_price` / `needs_replace` / `extract_max_spread` / 挂单逻辑 / Step 1 / Step 2 的行为——只加日志，不改任何决策或下单。

## 错误处理

- `BufferLogHandler.emit` 内 `try/except` + `handleError`，绝不向日志调用方抛异常。
- `/api/logs` 即使缓冲为空也返回 `[]`（200）。
- `clear` 幂等。

## 测试

- `tests/test_log_buffer.py`（纯逻辑，构造 `logging.LogRecord` 直接喂 handler）：
  - `emit` 后 `get_logs()` 含该条且字段（ts/level/logger/message）正确。
  - 追加超过 `maxlen` 时最旧被丢弃、长度不超过 maxlen。
  - `clear_logs()` 后 `get_logs()` 为空。
  - `emit` 遇异常记录（如 message 格式化问题）不抛出。
- 扩 `tests/test_monitor.py`：用 pytest `caplog` 断言 Step 3 五个分支（keep / replace / cancel / 取不到 max_spread / 空盘口）各产出含关键字段（单号、`[Step3]`、判定动作关键字）的日志行。
- 现有用例不回归（仅新增日志，不改既有断言对象 / 行为）。

## 不做（YAGNI）

- 日志不落新 DB 表（`market_maker.log` 已全量持久化历史）。
- 不做服务端分页 / 搜索（前端本地对 ≤1000 条筛选足够）。
- 不做运行时日志级别调节。
- `LOG_BUFFER_SIZE` 不做设置页可配（避免启动早于登录解密的时序耦合）。
