# 账户净值历史 设计

日期：2026-07-13。状态：已与用户对齐口径，待实施。

## 背景与目标

用户想看每个账户的资产变化：引擎启动时记录每个启用账户的净值，之后能看每日变化曲线、查某一天的净值。目前系统只在下单前临时读余额，任何历史都没有留。

## 已确认的口径

- **记录值是净值三元组**：现金（`get_balance()` 的 pUSD）、持仓市值（`get_user_positions()` 的 Data API 估值）、合计净值。三个数都存。
- 持仓估值用 Data API 的 `currentValue`（缺失时 `size × curPrice`），**仅用于展示**，不进任何交易决策。这与「curPrice 禁用作成本」的铁律不冲突——那条铁律限定的是离场成本口径。
- **记录时机**：worker 启动后的首个 tick 记一次（覆盖引擎启动、单钱包启动、手动模式启动监控三条路径），之后运行中每天补记一次。
- 一天允许多条（重启会补记），查询与曲线口径都取**当天最后一条**。

## 数据

`models/database.py` 新表：

```sql
CREATE TABLE IF NOT EXISTS net_worth_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    total REAL NOT NULL,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_networth_wallet_time
    ON net_worth_history(wallet, created_at);
```

DB 方法：

- `record_net_worth(wallet, cash, positions_value)`——`total` 由方法内相加写入。
- `get_net_worth_daily(wallet, days)`——按**本地日期**聚合，每天取最后一条，返回 `[{date, cash, positions_value, total}]`，升序。

## 采集

`WalletWorker`（`engine/manager.py`）加一个 `_maybe_snapshot_networth()`，在 `_tick` 里调：

- 首个 tick 必记一条（即使当天已有——重启补记，多条无害）。
- 之后每 tick 只比对内存里的上次快照日期（本地日期字符串），跨天才再记，不用每 5 秒查一次库。
- 快照单独 try/except：失败打 WARNING，不阻断监控步骤，下个 tick 自然重试。
- `get_balance()` / `get_user_positions()` 都是实例方法，自带钱包代理路由，无需额外处理。

跨天判断和持仓市值求和抽成纯函数，放 `engine/networth.py`：

- `should_snapshot(last_date, today) -> bool`
- `positions_value(positions) -> float`（`currentValue` 优先，缺失回退 `size × curPrice`，两者都缺按 0 计）

## API

`GET /api/networth?wallet=<addr>&days=<n>`（`days` 默认 90，登录态保护与其他 `/api/*` 一致）：

```json
{"wallet": "0x…", "series": [{"date": "2026-07-13", "cash": 120.5, "positions_value": 80.2, "total": 200.7}]}
```

## 前端

侧边栏加第 8 屏「资产曲线」（`web/templates/networth.html`）：

- 钱包下拉（复用现有钱包列表接口）。
- 手写 SVG 折线图（零依赖，约百行 JS）：净值主线 + 现金辅线；悬停数据点显示当天日期、现金、持仓市值、净值。不引入 Chart.js——PyInstaller 包多带 200KB 资产不值。
- 日期查询框：选日期显示当天（最后一条）数值；当天无记录显示「无记录」。
- 深浅主题走现有 CSS 变量。
- 含中文的前端文件由主会话直接 Write，写后 `node --check` + 查别字/BOM（惯例）。

## 测试

- DB：`record_net_worth` + `get_net_worth_daily` 聚合（同日多条取最后一条、按日期升序）。
- 纯函数：`should_snapshot` / `positions_value`（含 `currentValue` 缺失回退）。
- `/api/networth` 契约测试（照 `/api/settings` 契约测试的先例）。

## 非目标

多账户合计曲线、收益率/盈亏归因、导出，这次都不做。

## 版本

向后兼容的新功能 → 次版本号；若与「档位模块化挂单」同版发布，则并入那次主版本。
