# 已排除的假设 (2026-05-23)

- **止损市价单 amount 语义错误** — 排除。SDK `MarketOrderArgsV2.amount` 文档:"SELL orders: Shares to sell";`get_market_order_amounts` SELL 分支 `raw_maker_amt=round_down(amount, size)` 确认 amount=份额。代码 `place_market_sell(asset_id, size)` 传持仓份额,正确。(本地 docstring 文字错,见 findings L5。)

- **多线程共用同一 ClobClient 导致竞态** — 降级为 LOW。SDK 的 HTTP 走模块级 `get/post/delete`(每次调用新建请求,不共享 Session),L1/L2 签名按次用 signer+nonce+timestamp 生成、无可变状态;dict 缓存(tick_size/neg_risk/market_info)的 check-then-set 至多导致无害重复拉取。真正的并发隐患在共享 sqlite3 连接(findings Bug 5)。

- **take-profit 把一个持仓拆成多笔卖单** — 排除。`plan_take_profit` 设计为始终维护恰好一笔成本价卖单,多余的进 cancel_ids 撤掉(`take_profit.py:46-67`)。这正是修掉旧 per-fill 卖单的逻辑。

- **stop_loss_triggered 在缺数据时误触发** — 排除。`risk.py:15` 对 `cur<=0 或 avg<=0` 直接返回 False,不会在无报价时市价平仓。

- **eligibility 复查在盘口缺失时误撤买单** — 排除。`eligibility.py:27` 仅当 `spread_cents is not None 且 ≥阈值` 才撤;None(盘口一侧缺失)保守保留。

- **fills 把别人的成交算成自己的** — 排除。`fills.py:26-29` 按 `maker_address==funder` 且 `side==BUY` 过滤,并以 `(trade_id, order_id)` 去重。

- **黑名单不拦截 Step3 重挂** — 排除。`monitor.py:432-461` 在 compliance 最前面先查黑名单,命中则撤单且绝不重挂。
