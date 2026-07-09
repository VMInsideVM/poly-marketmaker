# 资金路径 Debug 执行摘要

**会话:** 2026-06-02 17:28 · **范围:** `engine/** + api/**` · **模式:** 深度排查,只报告不修
**结果:** 8 确认(1 已修)/ 4 排除 · 假设 12 · 调查文件 ~13/18 · 全程 360 单测绿

## 一句话结论

除已修的 neg_risk(F1,CRITICAL),最该优先处理的是 **F2:下单封装从不检查交易所应用层 `success/status`**——它让止损在薄盘 FOK 未成交时记下"幻影止损"却仍持仓裸奔,是 F1 之外第二个能直接伤到资金安全的缺陷。

## 按优先级建议处理顺序

| 优先级 | 编号 | 严重度 | 一句话 | 修复成本 |
|---|---|---|---|---|
| 0(已修) | F1 | CRITICAL | 卖单没贯穿 neg_risk,负风险仓裸奔 | 已完成 |
| 1 | F2 | HIGH | 下单不查 success/status,被拒/未成交当成功 | 中 |
| 2 | F3 | MEDIUM | 止损 FOK 全有或全无,大仓薄盘止不了损 | 中 |
| 3 | F5 | MEDIUM | 止损只信 curPrice,glitch 即砸盘 | 中 |
| 4 | F4 | MEDIUM | tick 无兜底,异常静默杀死监控线程 | 低 |
| 5 | F6 | MEDIUM | balance/custom 份额每单全额,超额挂单 | 低 |
| 6 | F7/F8 | LOW | 卖价>1.0 上限;get_open_orders 失败仍止损 | 低 |

F2/F3/F5 互相叠加:**薄盘 + 负风险/大仓 + 数据 glitch** 三者组合时,止损既可能被 glitch 误触发、又因 FOK 打不出去、还被记成已平仓——风控在最需要它的场景下同时三处失效。

## 关键证据链(F2)

```
monitor.place_market_sell(size)               # 止损市价单
 └─ api.place_market_sell → return client.create_and_post_market_order(...)  # 不看返回
     └─ post_order → _post → helpers.request()
         └─ 仅 HTTP≠200 才 raise PolyApiException
            ↑ Polymarket 对 FOK 未成交返回 200 + status:"unmatched"
 → 无异常 → monitor 记 record_trade(stop_loss) + action  # 幻影成交
 → 仓位实际仍在 → 下个 tick 再触发再记                      # 反复污染
```

## 修复进度(2026-06-02 同会话,逐条 TDD,369 单测全绿)

| 编号 | 状态 | 处理 |
|---|---|---|
| F1 | ✅ 已修 | 封装 neg_risk 默认 False→None,交底层自查 |
| F2 | ✅ 已修 | 新增 `OrderRejected` + `_check_order_resp`,success=False/status=unmatched 抛错;monitor 不再记幻影成交 |
| F3 | ✅ 已修 | 止损市价单 FOK→FAK(能卖多少先卖多少) |
| F4 | ✅ 已修 | `_run` 包 try/except,单 tick 异常不再杀线程 |
| F5 | ✅ 已修 | 止损加实时盘口买一二次确认,glitch 不砸盘(`⚠️跳过·盘口未确认`) |
| F8 | ✅ 已修 | get_open_orders 失败时跳过本 tick 止损(不空表市价卖) |
| F6 | ⏸ 不改(待定) | 复查后判定为**刻意设计**:刷奖励靠驻留挂单、交易所拒超额成交;有显式测试+CLAUDE.md 记载。等用户裁决 |
| F7 | ⏸ 不改(待定) | LOW;F2 落地后被拒卖单已不再记幻影,实际危害大幅下降;naive 夹取会破坏"卖价不低于成本"不变式。等用户裁决 |

## 未覆盖 / 后续

- 实盘确认 F2:需要在测试钱包用一笔注定被拒/未成交的单,抓 POST /order 的真实响应体,锁定 `success/status` 字段集合。
- `web/routes.py`(`/api/positions`、`/api/history`、`balance-sigs` 等)不在本轮范围;若要审 UI 与资金展示一致性可单开一轮。
- 修复阶段建议逐条 TDD(本轮明确只报告未改动任何业务代码)。
