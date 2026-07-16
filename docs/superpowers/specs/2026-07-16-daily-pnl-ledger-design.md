# 每日做市盈亏台账（daily P&L ledger）设计 / spec

> 日期：2026-07-16　　状态：已批准，待写实现计划
> 这是「资产曲线扩成每日盈亏台账」大功能的**子项目 1（本地台账）**。子项目 2（远程推送汇总）随后单独 spec。

## 一、背景与目标

现有净值曲线（`net_worth_history`）只记「现金 + 持仓市值 = 总资产」，且只在引擎运行时逐日快照、无补漏、不区分充提。
用户要把它扩成**每日做市盈亏台账**：每天记录做市奖励、卖出盈利、各类亏损、taker 手续费，算出每日净利润，
与净值并列展示；能补漏（漏运行的日子自动补上）、过滤手动充提、给出全钱包汇总。

**净值 vs 台账（互补，不是替代）**：净值 = 总资产（含充提，会因转入转出跳变）；台账 = 纯利润（由奖励+成交+结算算出，
充提永不进利润）。两者并列，正好满足「过滤转入转出、利润不被充提污染」。

## 二、口径与时区（已与用户敲定）

- **每日净利润 = 做市奖励 + 卖出/赎回盈利 − 卖出/赎回亏损 − taker 手续费**。
  - **盈利桶** `sell_profit_usd`：任一退出（卖出成交 或 REDEEM 赎回）中「退出金额 > 成本」的正部分。
  - **亏损桶** `loss_usd`：任一退出中「退出金额 < 成本」的负部分绝对值（止损、市价清仓、结算归零都落这里）。
  - **奖励** `reward_usd`：做市奖励（单列，正）。
  - **手续费** `fee_usd`：taker 手续费（maker 恒 0）。
  - `net_usd = reward_usd + sell_profit_usd − loss_usd − fee_usd`。
- **时区**：成交/亏损/手续费/净值按**北京 0 点**分天（固定 `+8 hours` 偏移，不靠机器时区）。
  奖励按 Polymarket 的 **UTC 日期标签**记到台账同名日（UTC 日 D 的奖励 = 台账第 D 天；今早北京 8 点到账的奖励
  正好记到「前一天」——与用户在 Polymarket 官网看到的按日期奖励对齐）。UTC 日与北京日 8 小时错位不可消除
  （奖励是每 UTC 日一个整数，无法再拆），对每日台账无实质影响。
- **补漏起点**：**2026-06-01**（含）到「昨天」。

## 三、数据源（研究已文档确证，详见调研结论）

| 台账项 | 数据源 | 鉴权 | 关键点 |
|---|---|---|---|
| 做市奖励 | CLOB `GET /rewards/user/total?date=D&maker_address=<funder>&signature_type=<sig>` | L2（每钱包已有） | 权威口径；按天查、逐日循环；返回 `TotalUserEarning[]`（`date`/`asset_address`/`earnings`/`asset_rate`）；`reward_usd = Σ earnings×asset_rate`（**asset_rate 换算语义需 Phase 0 探针确认**） |
| 卖出盈亏 + 手续费 | CLOB `get_trades(TradeParams(market=condition_id))` | L2 | 每笔带 `fee_rate_bps`；**绝不用 `asset_id=` 过滤**（已知服务端 bug，CLAUDE.md 有记）；`match_time`（秒，UTC）分天 |
| 结算（赎回） | Data API `GET /activity?user=<funder>&type=REDEEM`（可加 `start/end`） | 免鉴权 | `usdcSize`=赎回到账；`timestamp`（秒，UTC） |
| 转入转出（过滤） | `/activity` 的 `DEPOSIT/WITHDRAWAL/SPLIT/MERGE/CONVERSION` | 免鉴权 | **本就不计入利润**——台账只加「奖励+已实现交易盈亏+结算盈亏−手续费」，转账天然不进利润，无需显式拉取过滤 |

- Data API base：`https://data-api.polymarket.com`；CLOB base：`https://clob.polymarket.com`。
- 所有时间戳均 epoch 秒（UTC）。分页：`/activity` 用 `limit`/`offset`（offset 上限 10000，超长历史用 `start/end` 分段）；
  `/rewards/user/total` 单日查（无区间）；`get_trades` 项目已有自动翻页封装。

## 四、数据模型（新表 `daily_pnl`）

```sql
CREATE TABLE IF NOT EXISTS daily_pnl (
    wallet TEXT NOT NULL,
    date TEXT NOT NULL,            -- YYYY-MM-DD（北京日；奖励用同名 UTC 日标签）
    reward_usd REAL NOT NULL DEFAULT 0,
    sell_profit_usd REAL NOT NULL DEFAULT 0,
    loss_usd REAL NOT NULL DEFAULT 0,     -- 正值 = 亏损额
    fee_usd REAL NOT NULL DEFAULT 0,
    net_usd REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (wallet, date)
);
```

- **幂等 upsert**：`INSERT ... ON CONFLICT(wallet, date) DO UPDATE`——补漏/重算安全覆盖。
- 建表 + 幂等迁移（`CREATE TABLE IF NOT EXISTS`，无需 ALTER，新表）。
- 存 `%LOCALAPPDATA%` 的同一 sqlite，升级不丢（现有存储已满足）。
- `net_worth_history` 不动（净值曲线照旧）。

## 五、纯计算 `engine/pnl.py`（无 IO，全单测）

- `beijing_day(ts_utc_sec) -> "YYYY-MM-DD"`：`+8h` 偏移取北京日（不靠机器时区）。
- `realized_pnl_by_day(fills, redeems, ...) -> {date: {"sell_profit":x, "loss":y, "fee":z}}`：
  - 输入：某钱包某 asset（或全量）的逐笔成交（买/卖，含 `price/size/side/match_time/fee_rate_bps`）+ REDEEM 事件（含 `usdcSize/size/timestamp/asset`）。
  - **FIFO 逐笔重建**（复用 `position_cost_with_lots` 方法论）：按时间正序回放，买入入队；每笔卖出/赎回从最早买入 lots 对冲，
    对冲部分的已实现盈亏 = (退出单价 − 该 lot 买入成本) × 对冲量；退出单价：卖出=成交价，赎回=`usdcSize/size`。
  - 每笔退出的已实现盈亏按「退出事件的北京日」归集：正入 `sell_profit`、负入 `loss`（取绝对值）。
  - 手续费：每笔卖出 `fee = fee_from_fill(fill)`，按该卖出的北京日累加进 `fee`（买入 maker 费恒 0）。
- `fee_from_fill(fill) -> usd`：由 `fee_rate_bps` + `price` + `size` 现算（**确切公式 Phase 0 探针核实**：文档给 taker
  `fee = C×feeRate×p×(1−p)`，但逐笔 `fee_rate_bps` 是实际计费率，优先按响应字段算实际扣费，不自己按品类查表）。
- `reward_usd_from_total(total_earnings) -> usd`：`Σ earnings×asset_rate`（**asset_rate 语义 Phase 0 核实**）。
- `net_of(row) -> net_usd`：`reward + sell_profit − loss − fee`。

**成本口径铁律沿用**：成本只来自 CLOB `get_trades` 逐笔重建，**绝不用 Data API `avgPrice`/`curPrice`**（项目两次翻车已明令禁止，见 [[take-profit-position-driven]]）。

## 六、API 原语（`api/polymarket_api.py`）

- `get_user_rewards_total(self, date: str) -> list[dict]`：CLOB `GET /rewards/user/total?date=&maker_address=self.funder&signature_type=`；L2 鉴权走本钱包；失败抛（调用方按天跳过/重试，不静默填 0）。
- `get_activity(self, types: list[str] = None, start: int = None, end: int = None) -> list[dict]`（静态或实例均可，免鉴权）：Data API `/activity?user=self.funder&type=&start=&end=`，自动翻页（offset 步进，遇满页继续，尊重 10000 上限；超限用 start/end 分段——v1 因 6/1 起窗口有限，offset 足够）。
- 复用现有 `get_trades`（成交 + fee_rate_bps）、`get_user_positions`（`redeemable` 辅助判断）。
- 代理：这些调用要走该钱包代理（`get_user_rewards_total`/`get_activity` 是实例方法，自 route via `self.proxy_url`，与现有 `_PROXIED_METHODS` 一致）。

## 七、结算归零（v1 尽力而为）

- **主路径已覆盖**：新上线的「结果提交即市价清仓」（[[take-profit-position-driven]] 2026-07-16）在结算前把持仓市价卖掉，
  记为**卖出亏损**（走 §五 的 FIFO），已进台账。真正走到「链上结算归零」的只剩极少数（市价卖失败/无买盘）。
- **v1 尽力而为**：对「某 condition_id 已结算（`gamma_resolution_status` 非空/`/positions` 显示已 resolve）、我方还剩未退出买入 lots、且无对应 REDEEM」的情形，把这些 lots 的成本按**结算日**记入 `loss`。需市场结算日期（Gamma）。
- **若 Phase 0 探针发现归零仓另有可靠信号**（如 REDEEM usdcSize=0、或 `/positions` 带 resolve 时间），据此实现更准；否则 v1 先只保证「市价卖亏损」这条主路径准确，结算归零列为**已知近似**，在 spec/公告注明。

## 八、补漏与更新（wiring）

- **`engine/pnl_ledger.py`**（有 IO，编排）：`rebuild_wallet_pnl(api, db, wallet, from_date, to_date)`：
  逐日 `date` 从 `from_date` 到 `to_date`：查奖励、按天归集成交/赎回/手续费 → 组装该钱包该日 `daily_pnl` 行 → upsert。
  近 N 天（默认 3，未结算完/奖励延迟发放）每次都重算覆盖。
- **启动补漏**：仿 `startup_recovery`——引擎启动后，对每个启用钱包 `rebuild_wallet_pnl(from=max(2026-06-01, 已有最早缺口), to=昨天)`。
  首次全量（6/1 起）；之后增量（只补缺失 + 重算近 N 天）。放后台线程/首个 tick,失败只 WARNING、下轮重试，不阻断交易。
- **每日更新**:仿 `_maybe_snapshot_networth`——跨北京日时对每钱包重算「昨天+今天」并 upsert。奖励当天查不到(次日 8 点才发)属正常,次日重算补上。
- 净值快照 `_maybe_snapshot_networth` 保留不动。

## 九、可视化与 API

- **路由**（`web/routes.py`,`@login_required`）：
  - `GET /api/pnl?wallet=<addr|all>&from=&to=`：返回该钱包(或全钱包汇总)按日 `daily_pnl` 序列 + 汇总(区间合计 reward/sell_profit/loss/fee/net + 累计净利润)。`all` = 各日跨钱包求和。
- **前端**：扩净值页(`networth.html`)或新增「盈亏」页：钱包下拉(含「全部」)、每日盈亏拆解(奖励/盈利/亏损/手续费/净利润的柱状或表格)、累计净利润曲线,与现有净值曲线并列。展示复用现有 SVG 折线手法(见 [[frontend-v4-redesign]]/净值页);含中文模板由主会话直接改。

## 十、分阶段（plan 按此拆 task）

- **Phase 0：实盘探针**（手动脚本 `probe_pnl.py`,仿 `dump_trades.py`,非 pytest）：只读真钱包,打印
  `get_user_rewards_total` 某日、`/activity`(REWARD/REDEEM/DEPOSIT/WITHDRAWAL 各取样)、`get_trades` 逐笔 fee 字段的**真实 JSON**。
  **用户跑一次**(`! python probe_pnl.py`),据输出**锁定**:reward 换算(asset_rate)、fee 公式、REDEEM/归零形态、各字段名。后续 Phase 按锁定结果写。
- **Phase 1：API 原语**（`polymarket_api.py` + 单测,mock 按探针形态）。
- **Phase 2：纯计算 `engine/pnl.py`**（FIFO 按日归集 + reward/fee 换算,全单测,核心 IP）。
- **Phase 3：DB `daily_pnl`**（建表 + upsert + 查询 + 全钱包汇总,单测）。
- **Phase 4：补漏/更新 wiring**（`pnl_ledger.py` + 启动补漏 + 每日更新,单测编排逻辑,mock API）。
- **Phase 5：路由 + 可视化**（`/api/pnl` + 前端页,路由单测 + 主会话手改前端 + 渲染走查）。

## 十一、需实盘验证项（Phase 0 锁定,不确定就不猜）

- `/rewards/user/total` 的 `asset_rate` 换算美元语义、`earnings` 单位。
- `get_trades` 逐笔手续费:是 `fee_rate_bps`(率,需现算金额)还是直接金额;maker 是否恒 0。
- `/activity` REDEEM 记录 `usdcSize`/`size` 语义;**结算归零仓是否产 REDEEM 记录**。
- `/activity` REWARD 记录是否与 `/rewards/user/total` 一致(交叉校验)。
- DEPOSIT/WITHDRAWAL 记录形态(确认可识别以便审计,虽然默认不进利润)。

## 十二、测试策略

- 纯函数(`engine/pnl.py`)全单测:FIFO 盈亏按日归集(买多笔、部分平、跨日退出)、reward/fee 换算、北京日边界(UTC 15:59:59 vs 16:00:00 分属两日)、盈利/亏损桶正确分流。
- DB `daily_pnl` upsert/查询/全钱包汇总单测。
- `pnl_ledger` 编排单测(mock API:补漏区间、近 N 天重算、幂等)。
- 路由 `/api/pnl` 单测(单钱包 + all 汇总)。
- API 原语单测(mock HTTP)。前端无单测,主会话渲染走查。
- 探针非 pytest(真 API,手动)。

## 十三、不做（YAGNI）/ 子项目 2 边界

- **不做远程推送**(子项目 2:每日汇总推送到云端/消息,单独 spec)。台账只需把数据算准存好、本地可视化。
- 不改净值曲线既有逻辑(并列新增,不替代)。
- 结算归零 v1 尽力而为(主路径市价卖亏损已覆盖),不为极少数归零仓做重型链上追溯。
- 不自己维护品类费率表(用逐笔实际费率)。
- 不引入新数据源做「本地成交账本」(成本仍只认 get_trades,禁 avgPrice/curPrice)。
