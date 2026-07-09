# 已排除的假设(同样有价值——避免重复排查)

- **MarketOrderArgsV2.amount 语义错配** — 证伪。底层 dataclass 文档:`"BUY orders: $$$ Amount to buy. SELL orders: Shares to sell"`。`engine/monitor.py:416` 给止损传持仓份额,与 SELL 的 shares 语义一致。

- **成本重建会把已平仓的旧买单当成本 / 卖价低于成本** — 证伪。`engine/take_profit.py:74 position_cost_with_lots` 按时间正序回放买卖、FIFO 对冲,剩余 lots 即当前持仓;`take_profit_price = max(ceil_to_tick(cost), best_bid+tick)` 穿价护栏保证卖价恒 ≥ 成本。`tests/test_take_profit.py` 覆盖充分。

- **黑名单只在部分新买路径拦截** — 证伪。三处新买路径全覆盖:`engine/manager.py:160`(place_orders)、`engine/scanner.py:98`(scan)、`engine/monitor.py:528`(Step3 重挂)。止盈/止损卖单刻意不查黑名单——平仓既有持仓不应被黑名单阻断,符合预期。

- **watermark `int(after)` 截断导致漏成交** — 证伪。`engine/monitor.py:146` 的 `after` 仅作 get_trades 拉取下界,真正幂等靠 `(trade_id, order_id)` 去重(`_seen_fill_keys`);截断只会多拉一点,不会漏。

- **_tick 各步骤未捕获异常** — 部分证伪。`check_buy_orders/check_take_profit/check_stop_loss/check_sell_orders/publish_status` 各自对网络调用与 per-item 循环都有 try/except。残留缺口只在 ① `_run` 对 `_tick` 整体无兜底、② `check_stop_loss` 首行 `get_settings()` 在 try 外 —— 已归入 F4。

- **止盈把仓位拆成多笔卖单 / 按每笔 maker 价定价** — 证伪。现版本是持仓驱动、每仓恰好一笔卖单,`plan_take_profit` 做对账(keep/replace),价格来自 get_trades 加权成本,不再用 per-fill maker 价(旧 bug 已重构掉)。

- **held 用 condition_id 导致漏挂同市场另一腿** — 非 bug。`engine/positions.py held_condition_ids` 按 condition_id 聚合,持有某市场任一腿即跳过该市场新买,属做市避免同市场两腿对冲的合理取舍,非缺陷。
