# 调查发现 — 下单→监控管线 (2026-05-23)

范围:整个代码库,聚焦 scan→strategy→place→monitor。只报告,未改动代码。
测试基线:`pytest` 215 passed(纯逻辑单测,不覆盖下单/监控的 API 副作用路径)。

---

## [HIGH] Bug 1:全额下单 + 跨市场不递减余额 → 资金集中单一市场

- **位置:** `engine/manager.py:197-200`(配合 `:100-139` 的上限逻辑)
- **假设:** 注释声称"Polymarket maker 买单不锁仓,同一笔余额垫付所有挂着的买单",故循环里刻意不递减余额。
- **证据:**
  - 代码:`order_size = int(balance / order_price)` 每个市场都用全额余额算量,且循环中不扣减。
  - Polymarket 官方 create-order 文档:`maxOrderSize = balance − ∑(openOrderSize − filledAmount)`,即**在挂买单会预占抵押金**。注释前提为假。
- **复现:** 一个钱包符合 ≥2 个市场时,按 `market_competitiveness` 升序,第 1 个市场用全额余额挂单→占满抵押金;第 2、3… 个市场再读全额(或净额)→要么被 CLOB 拒单("not enough balance / allowance"),要么 `order_size<min_size` 跳过(`:205`)。
- **影响:** 设计本意是把余额分散到多个奖励市场吃奖励,实际**每个扫描周期只有第一个市场拿到买单**,资金高度集中、奖励来源单一、单点行情风险放大。
- **根因:** 对交易所抵押金占用模型的误解。
- **建议修复:** 按可用槽位分摊余额(如 `order_size = int((balance/slots)/order_price)`),或在循环里按已下单金额递减一个本地余额账本;并在下单前用 `update_balance_allowance` 校准。

---

## [HIGH] Bug 2:max_buy_orders_per_wallet 实际不可达(Bug 1 的直接后果)

- **位置:** `engine/manager.py:100-139`(上限/槽位)+ `:197-200`(全额量)
- **假设:** 上限默认 5,意图让每钱包最多挂 5 笔买单分散到 5 个市场。
- **证据:** 由 Bug 1,首单吃光抵押金后,后续下单失败/跳过。`slots = max_buys - len(buy_orders)` 永远停在"已挂 1 笔"附近,5 个槽位用不满。
- **影响:** 一个看似生效的风控/分散参数其实是死配置;用户调它没有任何效果。
- **建议修复:** 与 Bug 1 一并修(分摊余额后,5 笔才可能共存)。

---

## [HIGH] Bug 3:监控管理整个钱包的所有持仓(无"机器人自己开的仓"过滤)

- **位置:** `engine/monitor.py:169`(check_take_profit)、`:278`(check_stop_loss),遍历 `get_user_positions(funder)` 的全部返回。
- **假设:** 监控只应管理本程序开出的持仓。
- **证据:** 全代码库 grep 无 bot-owned/created_by/owned_assets 之类的归属过滤;`_reconcile_take_profit`/`_check_pos_sl` 对每个 position 无条件执行。funder 就是用户在 Polymarket 的主充值钱包(资金/持仓都在这)。
- **复现:** 用户在同一钱包持有任何**与本程序无关**的 Polymarket 持仓 → 下一个 tick:
  1. take-profit 会对它在成本价 `avgPrice` 挂一笔卖单(封死上行收益);
  2. 一旦现价跌破 `avgPrice×(1−15%)`,stop-loss 直接**市价强平**该持仓。
- **影响:** 对非技术用户极危险——程序会动用户手动建的仓位,可能在用户不知情下平掉其它下注。
- **建议修复:** 给监控维护一份"本程序持仓的 asset_id 白名单"(下单成交时登记),take-profit/stop-loss 只处理白名单内的 asset;或至少在 UI 显著警告"本钱包内所有持仓都会被托管"。

---

## [MEDIUM] Bug 4:止损先记账后确认成交,且 P&L 用 curPrice 而非真实成交价

- **位置:** `engine/monitor.py:328-337`
- **证据:** `self.api.place_market_sell(asset_id, size)`(FOK)后,无视返回值直接 `record_trade(..., price=cur, pnl=(cur-avg)*size)`。
- **复现/影响:**
  - 薄盘下 FOK 可能未成交;若 SDK 以返回失败状态而非抛异常表达,会记下一笔实际没发生的 `stop_loss` 交易;
  - 即使成交,市价单滑点使真实成交价低于 `curPrice`,但 pnl 仍按 `curPrice` 计 → 止损盈亏被高估。
- **缓解:** 下个 tick 会重读持仓重试,持仓保护是自愈的;主要损害是交易/盈亏记录失真 + 未校验返回。
- **建议修复:** 校验 `place_market_sell` 返回的成交状态/成交价,成交后再按真实成交价记账;未成交则记一条"止损下单失败,待重试"。

---

## [MEDIUM] Bug 5:所有线程共用单个 sqlite3 连接,无应用层锁

- **位置:** `models/database.py:15`(`sqlite3.connect(..., check_same_thread=False)`,单连接全局共享)
- **证据:** 每个 WalletWorker 监控线程(每 5s 一 tick)+ scanner 线程 + Flask 请求线程都用同一个 `self.conn`,各方法 `execute`+`commit`,无 mutex。
- **影响:** 单连接=单事务上下文;多线程交叉的 execute/commit 在写竞争下可能 `database is locked`、提交彼此未完成的写、或 `recursive use of cursors`。当前多为即时 commit、窗口小,属潜在偶发性问题。
- **建议修复:** 给 DB 操作加一把进程内 `threading.Lock`,或每线程独立连接 + WAL 模式。

---

## [LOW] 其它

- **L1 `get_balance` 不刷新** `api/polymarket_api.py:129-137`:只 `get_balance_allowance` 不 `update_balance_allowance`,若 CLOB 缓存口径为陈旧/净额会与 Bug 1 叠加(行为更不可预期)。
- **L2 `_seen_fill_keys` 无界增长** `engine/monitor.py:24`:长时运行内存只增。可用 watermark + 有限窗口裁剪。
- **L3 结算日过滤可被绕过** `engine/scanner.py:92-96`:`_parse_end_date` 失败返回 0 → `days_left=-1` → 不被 `min_settlement_days` 过滤,临近结算的市场可能漏入。
- **L4 每 tick 冗余 API** `engine/monitor.py:178/287/369`:`get_open_orders` 调 3 次、`get_user_positions` 调 2 次,可合并为每 tick 各 1 次(性能,非正确性)。
- **L5 误导性 docstring** `api/polymarket_api.py:194`:`place_market_sell` 写 "amount (pUSD value to sell)",实际 SELL 的 amount 是份额(SDK 已确认),行为正确但注释错。
