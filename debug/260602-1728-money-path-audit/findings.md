# 资金路径 Debug — 确认发现

会话:2026-06-02 17:28 · 范围 `engine/** + api/**` · 深度 30+ · 只报告不修
全程 360 单测全绿;实验以直接代码审查 + 底层 `py_clob_client_v2` 源码追踪为主(本机不接实盘)。

severity 速览:CRITICAL×1(本会话已修) · HIGH×1 · MEDIUM×4 · LOW×2

---

## [CRITICAL] F1：卖单路径没把 neg_risk 贯穿到底层(本会话已修复)

- **位置:** `api/polymarket_api.py` 三个下单封装 + 调用处 `engine/monitor.py:318`(止盈)、`:416`(止损)
- **根因:** 封装层 `place_limit_sell/place_market_sell` 默认 `neg_risk=False` 并无条件塞进 `PartialCreateOrderOptions`;底层 `client.create_*_order` 只在 `options.neg_risk is None` 时才回退 `get_neg_risk(token_id)`(`client.py:749-752`)。写死 `False` → 负风险市场的卖单签到错误交易所合约被拒。买单路径传了真实值,故"能买不能卖"。
- **影响:** 负风险持仓**永远挂不出止盈卖单、也市价平不掉仓**(双重裸奔)。这正是用户朋友"一个账户成交4小时不挂卖单"的根因。
- **状态:** 已按 TDD 修复——三个封装默认改 `bool|None=None`,未指定时交底层自查;新增 `tests/test_polymarket_api.py::TestNegRiskAutoResolve`。360 绿。

---

## [HIGH] F2：下单封装从不检查响应 success/status,被拒/未成交被当成功

- **位置:** `api/polymarket_api.py` `place_limit_buy/limit_sell/market_sell` 全是 `return self.client.create_and_post_*order(...)`;消费方 `engine/monitor.py:317-333`(止盈记 action)、`:416-437`(止损 `record_trade`+action)
- **证据:** 底层 `http_helpers/helpers.py:request()` 仅在 **HTTP≠200** 时抛 `PolyApiException`。Polymarket 下单接口对"逻辑失败"常返回 **HTTP 200 + 失败体**:典型是 FOK 市价单深度不足时返回 `status:"unmatched"`(未成交)。封装不看 `success`/`status` 直接返回 → monitor 视作"已下单/已成交"。
- **复现:** 一个 FOK 止损市价单在薄盘口未成交 → 底层返回 200/unmatched → `place_market_sell` 正常返回 → monitor 走到 `db.record_trade(side="stop_loss", pnl=(cur-cost)*size)` + `stoploss_market_sell` action。**仓位实际没卖出**,但 DB 记了一笔幻影止损;下个 tick 仍持仓、仍触发、**再记一笔**。
- **影响:**
  - 止损:幻影成交污染 PnL/交易历史;用户以为已止损,实际仓位仍在、仍在亏 → 风控失效且不自知。
  - 止盈:被拒时记假"卖单理由(已按成本挂单)",而 `get_open_orders` 下个 tick 查无此单 → 反复重挂重记,日志噪声 + 误导。
- **根因:** 封装把"HTTP 成功"等同于"下单/成交成功",忽略了交易所的应用层 `success/status`。
- **建议修复:** 封装内统一检查 `res.get("success") is False` 或 `res.get("status")` 不在成功集合({"matched","live","delayed"}等)→ 抛错或返回失败标志;消费方据此**不记 action / 不写 record_trade**。止损尤其要确认真成交再记。

---

## [MEDIUM] F3:止损用 FOK 市价单,全有或全无

- **位置:** `engine/monitor.py:416` `place_market_sell` → `api/polymarket_api.py:283` `OrderType.FOK`
- **影响:** 持仓份额 > 买一档总深度时,FOK 整笔无法成交→被杀,**大仓在薄盘永远止不了损**。叠加 F2,还会被记成已平仓。
- **根因:** 止损用 FOK(fill-or-kill)而非 FAK(fill-and-kill)/限价穿价。止损语义应"能卖多少先卖多少",FOK 与之相悖。
- **建议:** 止损改用 FAK 或对手价限价单分批吃单;或在 FOK 失败时回退分批。

## [MEDIUM] F4:监控 tick 循环无顶层兜底 → 单次异常静默杀死该钱包监控

- **位置:** `engine/manager.py:65-70` `_run`:`while not stop: self._tick(); wait` —— `_tick` 外层无 try。`check_stop_loss` 首行 `settings = self.db.get_settings()` 也在 try 之外(`monitor.py:347`)。
- **影响:** 任一未捕获异常冒泡出 `_tick` → 该钱包监控线程**永久死亡**:不再检测成交、不再止盈/止损、不会自动重启。唯一外部症状是监控状态表对该钱包停更。这是排查友人故障时列为可能病因③的那一条。
- **根因:** 缺少 defense-in-depth:线程主循环未隔离单 tick 故障。
- **建议:** `_run` 内把 `self._tick()` 包进 try/except,记日志后继续下一轮;状态表对"上次 tick 过久未更新"给显式告警。

## [MEDIUM] F5:止损只信 Data API curPrice,无盘口交叉校验/去抖

- **位置:** `engine/monitor.py:372`(`cur = pos.curPrice`)、`:395` `stop_loss_triggered(cur, cost, pct)`
- **影响:** 止损触发完全依赖 Data API 的 `curPrice`,无任何二次确认。该 API 的 `avgPrice` 已因"新开仓瞬时 glitch"被全局禁用(见 CLAUDE.md / 记忆);`curPrice` 同源,一次瞬时偏低读数 → 立即 FOK 市价砸盘,在真实价并未跌破时造成实亏。
- **根因:** 单数据源、单次读数即触发不可逆市价单,无 debounce、无 orderbook best_bid 交叉校验。
- **建议:** 触发前用 `get_orderbook` 的 best_bid 交叉确认,或要求连续 N tick 触发,再市价平仓。

## [MEDIUM] F6:balance/custom 下单量模式按"每单全额",不分摊到槽位 → 超额挂单

- **位置:** `engine/order_sizing.py:30`(`size=floor(预算/price)`,balance 模式 预算=全余额)+ `engine/manager.py:217-230` 循环内**刻意不递减余额**
- **影响:** balance 模式下每个市场都按"花光余额"定份额;循环不递减余额 → 一批可挂到 `max_buy_orders_per_wallet` 笔、每笔都想花光全部余额。并发成交时后续成交因余额不足被交易所拒。custom 模式同理(N×custom_usd 可超余额)。
- **根因:** "maker 买单不锁仓、同一笔余额垫付所有挂单"的 non-decrement 设计对 `min` 份额成立(每单很小),但对 balance/custom 份额(每单≈全额)就变成 N 倍超额承诺。
- **建议:** balance/custom 模式按剩余可用槽位分摊预算(预算/slots),或在循环内对这两种模式递减一个"已承诺预算"。

## [LOW] F7:take_profit_price 可能 > 价格上限被拒

- **位置:** `engine/take_profit.py:138` `max(ceil_to_tick(cost), round(best_bid+tick))`
- **影响:** 买一=0.99、tick=0.01 → 卖价 1.00,超出有效价区间被拒;再经 F2 记成"已挂"。仅高价位持仓(默认价带 10–50c,但买后涨上去也可能)触发。
- **建议:** 对 want 做上限夹取(如 `min(want, 1-tick)`)。

## [LOW] F8:get_open_orders 失败时 check_stop_loss 用空表继续

- **位置:** `engine/monitor.py:357-361`:`get_open_orders` 失败时 `open_orders=[]` 并继续(不 return)
- **影响:** 此时 `_check_pos_sl` 找不到既有止盈卖单(sell_ids=[]),跳过撤单直接市价卖 → 该 token 可能同时存在一笔残留止盈限价卖单 + 一笔市价卖,瞬间双挂(多出的限价卖单后续多半因份额不足成废单)。窗口仅限 get_open_orders 恰好失败时。
- **建议:** get_open_orders 失败时对该 tick 的止损直接 return(与 check_take_profit 的处理一致)。
