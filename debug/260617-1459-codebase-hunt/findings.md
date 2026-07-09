# 代码库 Bug 通查 — Findings(260617-1459)

范围:整个代码库。基线:417 tests 绿。方法:5 个分区猎手 subagent 并行测假设 → 主 agent 逐条读码验证(不信报告)。

## 已确认 — 本轮修复(安全、高价值)

### [HIGH] F1 scanner.py:210 — `rewards_min_size: null` 触发 `int(None)` 崩溃
- 位置:`engine/scanner.py:210` `min_size = int(market.get("rewards_min_size", 0))`
- 证据:API 返回 `null` 时 `.get(...,0)` 给 `None`(键存在),`int(None)` 抛 TypeError;`filter_for_template` 该处无 per-market try/except → 该钱包本轮精筛/下单中断。`engine/manager.py:236` 同语义用了 `or 0` 兜底,前后不一致。
- 修:`int(market.get("rewards_min_size", 0) or 0)`(与 manager.py 一致)。

### [MEDIUM-HIGH] F2 monitor 离场三路 place 失败 → 静默裸奔
- 位置:`engine/monitor.py:325`(rest/place_limit_sell)、`:369`(market/place_market_sell)、`:403`(sweep/place_marketable_limit_sell)
- 证据:三个 place 调用均未包 try/except。`_check_order_resp` 在 success=false 或 status 非 live/matched/delayed 时抛 `OrderRejected`。rest/market 路径在 place 前已撤旧卖单;若 place 抛出,`check_exit:242` 的 per-position handler 捕获并 `logger.error` 后继续——但该 place 之后的 `_record_action`/`_status_add` 不执行 → **无 ⚠️ 状态行**,持仓零卖单裸奔且用户在监控页看不到。止损(B0 market)静默失败尤其危险。下个 tick 重试(若条件不变会再次失败)。
- 修:仿现有 `⚠️跳过·裸奔` 模式,三路各包 try/except,失败时 `logger.error` + 出 `⚠️挂卖失败·裸奔` / `⚠️止损失败·裸奔` 状态行后 return。纯增量、不改 happy path。

### [MEDIUM] F3 markets.html `spreadCell` 恒显 "≤N¢ ✓"
- 位置:`web/templates/markets.html:68-71`
- 证据:`/api/eligible` 服务的是**未经价差过滤**的候选池(过滤在 filter_for_template/下单时),spread_cents 在路由现算。`spreadCell` 只要 `maxSpread!=null` 就拼 "≤N¢ ✓",不比 `spread_cents <= maxSpread`。候选池里价差超限的市场也会显 ✓,误导。
- 修:仅当 `spread_cents <= maxSpread` 显 ✓,否则显 ✗(或不显勾)。

### [HIGH-文档] F4 CLAUDE.md + 本次 README 误述离场策略(已被 SP3 取代)
- 位置:`CLAUDE.md`(monitor 段)、`README.md`(本次会话写的"持仓驱动的止盈"条)
- 证据:二者称"卖价 = max(ceil_to_tick(成本), 最优买价+tick)(穿价护栏:不亏本卖)"。但 `engine/take_profit.py::plan_exit`(SP3 三段式离场)`_rest_price()` 返回 **best_ask**(无成本下限),B_park 可低于成本挂(认亏 park)、B_sweep 以 `ceil(成本−θ_loss)` 限损扫单、B0 市价止损——**会按 θ 阈值主动认亏**,并非"不亏本卖"。README 由本会话从过期的 CLAUDE.md 抄来,误述了风险行为。
- 修:更正 README 该条为 SP3 三段式离场(θ 限损);CLAUDE.md 的过期 monitor 段单独提示用户(其为项目指令文件,改动较大)。

## 已确认 — 仅报告(改动有行为风险/边缘,留给用户决定)

- **[MEDIUM] F5 `api/polymarket_api.py:381` cancel_orders 无 success 检查**:直通 client 响应,应用级 `{success:false}` 不抛异常 → 调用方(都只 catch 网络异常)误以为已撤 → 可能挂重复卖单/残留买单。**未自动改**:cancel_orders 若改为抛异常会影响 monitor/manager 多处 catch 语义(rest 路径会提前 return、market 路径"仍继续"),需评估;建议至少在 success=false 时 log。
- **[LOW] F6 `engine/laddering.py reconcile_buy_orders` 同价重复单都保留**:`keep` 是价集合非按单计数;同价两笔都满足 target 时都不进 cancel_ids → 双倍 size。触发:上轮 cancel 失败留下重复单。修法:`keep` 改记每价保留的 order id,多余进 cancel。
- **[MEDIUM] F7 `get_user_positions` 无分页**:单请求,>服务端页大小(约 100/500)的持仓被静默截断 → 漏离场/止损裸奔。本 app `max_concurrent_markets=10` → 持仓约 ≤20,实际风险低;建议传 `limit` 或游标分页防御。
- **[LOW] F8 `/api/update/apply`+`/status` 无 @login_required**:设计为登录前可更新弹窗(`/check` 故意开放)。`/apply` 触发下载+SHA 校验+装+重启;127.0.0.1 本地面,CSRF 只能强制装**已 SHA 校验的真实 Release**(非任意代码),危害低。报告,改动会破坏登录前更新流。
- **[LOW] F9 logout 未清 `encryption_key`/`_api_cache`**:`web/routes.py:242` 仅 session.clear();密钥留在进程内存至退出。单用户本地桌面风险低;可在 logout 清 `_api_cache`(清 key 需顾及运行中引擎+再登录)。
- **[LOW] F10 markets.html 轮询重置 `expanded`**:扫描中每 2s `expanded={}`+重建 DOM,展开的梯队被收起、in-flight 预演 fetch 完成时 `setLadder` 静默 no-op。仅扫描期;空闲不轮询不受影响。
- **[LOW] F11 config.html serializeTierRules 空值静默存 0**:fixed_amount/fixed_shares 留空 → `parseFloat('')||0` 存 0(该档变"不挂"),无校验提示。
- **[LOW] F12 前端 fetch 无 `.ok` 检查**:会话过期跳登录返回 HTML 时 `.json()` 抛、表静默陈旧(多处)。
- **[LOW] F13 `utils/net.py:21` pick_port 两候选都失败时返回不可绑端口**:Flask 随后崩+晦涩报错。port=0 几乎不会失败,情形罕见。
- **[LOW] F14 `models/database.py` delete_template/模板迁移 原子性边缘**:三条 DML 无显式事务包裹;迁移在特定 commit 间崩溃可留半迁移态。单用户串行写实际风险低。

## 已排除 / 非 bug(eliminated)

- **A1 fixed_amount min_size 地板"超支"**:`resolve_tier_share` 对 `s<min_size` 上调至 min_size 是**刻意**(低于 min_size 的单不赚奖励),有测试固化;受单市场敞口上限约束。属策略设计,不自动改(改动需用户决定)。
- **onclick 内 hex ID(地址/condition_id)未转义**:hex 串不含引号/尖括号,**不可利用**,仅结构性。
- **B_park 可低于成本挂 best_ask**:SP3 刻意(θ 限损、park 等回升)。
- **avgPrice 禁用为卖出成本 / 停引擎停止损 / get_trades 用 market=conditionId 过滤 / balance 不发 funder**:均文档化的刻意行为。
- **crypto(PBKDF2 600k+随机盐+AESGCM 验 tag)、自动更新 SHA-256 校验后才装、DB 全参数化查询**:读码确认无误。
- **F2 旧审计 F1-F5/F8 已修;F6/F7 经用户确认刻意不改**:不重复报告。
