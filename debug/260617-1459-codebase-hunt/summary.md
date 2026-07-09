# 执行摘要 — 代码库 Bug 通查(260617-1459)

范围:整个代码库(25 py + 11 html)。基线 417 tests 绿。方法:5 个分区猎手并行 + 主 agent 逐条读码验证。深度:单轮深挖,合计测假设 >30。

## 结论
- 确认 **14 条** real findings(去重 + 排除"刻意行为"后)。无 CRITICAL(无远程代码执行/资金直接被盗路径;crypto 与自动更新 SHA 校验、SQL 参数化均读码确认安全)。
- **已 TDD 修复 4 条**(分支 bugfix-codebase-hunt,419 tests 绿):
  - F1 scanner null 崩溃(or 0 兜底)
  - F2 monitor 离场三路 place 失败静默裸奔 → 出 ⚠️ 状态行(资金安全可见性)
  - F3 市场发现 spreadCell 误显"在限内✓"
  - F4 README 离场描述纠错(SP3 三段式 θ 限损,非"不亏本卖")
- **10 条仅报告**(改动有行为风险/边缘/低危,见 findings.md F5-F14):cancel_orders 无 success 检查(F5)、reconcile 同价重复单(F6)、get_user_positions 无分页(F7)、update/apply 无鉴权(F8,SHA 已护)、logout 不清密钥(F9)、前端若干鲁棒性(F10-F12)、pick_port/迁移原子性(F13-F14)。

## 建议优先级(若继续修)
1. F5 cancel_orders 至少在 success=false 时 log(防幻影撤单)。
2. F6 reconcile 同价去重(防上轮撤单失败留双倍仓)。
3. F7 get_user_positions 传 limit/分页(多市场防漏离场)。
4. **CLAUDE.md** monitor 段过期(描述已被 SP3 取代的护栏)——本会话曾因此误写 README,建议同步更正以免再误导。

## 待用户决定
- 把 bugfix-codebase-hunt(F1-F4)合并回 main?
- 是否继续修 F5/F6/F7 + 更正 CLAUDE.md?
- 是否再跑一轮猎手(深挖剩余风险)?
