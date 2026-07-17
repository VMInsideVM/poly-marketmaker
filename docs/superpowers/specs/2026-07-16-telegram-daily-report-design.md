# 每日盈亏汇总远程推送（Telegram 日报）设计 / spec

> 日期：2026-07-16　　状态：已实现并合并。
> 这是「每日盈亏台账」大功能的**子项目 2（远程推送汇总）**。依赖子项目 1 的 `daily_pnl`（已完成）。
>
> **⚠️后续变更（2026-07-16）**：用户要求把 Telegram 目标**写死在代码**（`config.py` 的 `TG_BOT_TOKEN`/
> `TG_CHAT_ID`/`PUSH_HOUR`），**不走配置页**——对方更新程序即生效、无需任何配置。故下文「§四 配置」的
> settings 键 + config 页表单 + §五的 `/api/push/test` 测试按钮**已移除**；推送始终开启，仅保留每日节流 +
> 后台线程发送 + 走扫描钱包代理。其余（时机/内容/format/send_telegram 消毒）不变。

## 一、背景与目标

朋友在自己机器上跑做市程序，用户（拥有者）要**远程看到整体做市情况**（总奖励、净利润等），且朋友机器
**只向外 POST、不开端口**。方案：程序每天把盈亏汇总**推送到 Telegram**，用户在 Telegram 里收看。

台账数据（`daily_pnl`）已算好存好，本子项目只需：**格式化汇总 + 定时发到 Telegram + 配置/测试**。
**纯外发、不改任何交易逻辑**。

## 二、推送内容（每天一条消息，报「昨天」）

报「昨天」（北京日 − 1）：昨天的奖励今早北京 8 点才到账，此时数据已定稿。

- 标题：`📊 做市日报 · <YYYY-MM-DD(昨天)>`
- **全钱包汇总**（昨天，6 类别）：做市奖励 / 做市返佣 / 卖出盈利 / 亏损 / 手续费 / **净利润**。
- **累计净利润**（自 2026-06-01 至昨天）。
- **各钱包净利润**（昨天，带备注标签，看清哪个号赚多少；无备注显示短地址）。

数值保留 2 位小数，纯文本（Telegram 默认解析即可，不强依赖 Markdown）。

## 三、时机与触发

- 每天**北京时间 `push_hour`（默认 9）点之后**推一次，报「昨天」。
- 挂在 manager 的 `_scanner_loop`（全局定时循环）里做**每日节流检查** `_maybe_push_daily`（仿净值/台账的
  「跨北京日只做一次」）：`today = beijing_day(now)`；若 `_last_push_date == today` 或 `beijing_hour(now) < push_hour` → return；
  否则推昨天汇总、置 `_last_push_date = today`。**一天一条，不重复**。
- **只在引擎运行（auto 模式，`start_all` 起了 `_scanner_loop`）时推**（朋友跑做市本就 auto，可接受；manual 模式不推）。
- **代理**：Telegram 国内被墙，推送 POST **走扫描钱包的代理**（`use_proxy(self._scanner_api.proxy_url)`）；
  代理/网络不通则本次失败、记 WARNING、次日重试，**绝不影响交易**（`_maybe_push_daily` 整体 try/except，
  且在 `_place_round` 之外/之后，不阻断下单）。

## 四、配置（config 页新增「远程推送」区）

存 `settings` 表（**明文，与 proxy 一致**；本地单用户库；经 `DEFAULTS` 合并默认值）：

- `push_enabled`（bool，默认 False）：总开关。
- `tg_bot_token`（str，默认 ""）：BotFather 建 bot 拿的 token。
- `tg_chat_id`（str，默认 ""）：用户发消息给 bot 后拿到的 chat_id。
- `push_hour`（int，默认 9）：北京时间几点后推。

config.html 加「远程推送」表单（开关 + token + chat_id + push_hour）+ 一个「**测试推送**」按钮
（立即发一条测试消息，当场验证 token/chat_id/代理通不通）。token 为凭证，输入框用 `type="password"` 遮挡。

## 五、代码落点（单一职责）

- **`engine/notify.py`**（新）：
  - 纯函数 `format_daily_report(date, totals, cumulative_net, per_wallet) -> str`：`totals` = 昨天全钱包 6 类别 dict；
    `per_wallet` = `[{label, net}]`。组装文本。无 IO，全单测。
  - `send_telegram(token, chat_id, text, proxy=None) -> None`：`http_get`/`requests.post` 到
    `https://api.telegram.org/bot<token>/sendMessage`（`{chat_id, text}`），`use_proxy(proxy)` 包裹；
    HTTP 非 2xx 或 Telegram `ok:false` → 抛（调用方据此告警/重试）。
- **`engine/pnl.py`** 加 `beijing_hour(ts) -> int`（复用 `_BJ`，单测边界）。
- **`manager`**：`__init__` 加 `self._last_push_date=None`；`_maybe_push_daily()`（挂 `_scanner_loop` 循环体内，
  `_place_round` 之后）；组装数据（`db.get_daily_pnl_all(昨天,昨天)`、累计 = `get_daily_pnl_all(PNL_START_DATE,昨天)` 求 net、
  各钱包 `get_daily_pnl(w,昨天,昨天)` + `list_wallets` 取 remark），调 `format_daily_report` + `send_telegram`
  （token/chat_id/hour 从 `db.get_settings()` 读；`push_enabled` 关则不推）。失败 WARNING、不置日期 → 次日重试。
- **`/api/push/test`**（POST，`web/routes.py`，`@login_required`）：读 settings 的 token/chat_id，用扫描钱包代理
  （无引擎运行时可无代理或用第一个钱包代理）发一条固定测试文本;返回 `{ok}` 或 `{error}`。
- **config.html**：「远程推送」表单 + 测试按钮（主会话手改）。`/api/settings` POST 已按 `DEFAULTS` 白名单自动
  存取，加键即可（select/checkbox 保存需前端单独收，见 [[take-profit-position-driven]] 配置链路坑）。

## 六、测试

- `format_daily_report` 纯函数单测：正常数字、空数据（昨天无记录→全 0）、多钱包、备注/短地址标签、净利润正负。
- `beijing_hour` 边界单测（UTC 01:00 → 北京 9 点）。
- `send_telegram` 单测（mock POST：URL 含 token、payload 含 chat_id/text；HTTP 失败或 `ok:false` 抛）。
- `_maybe_push_daily` 单测（mock）：push_enabled 关不推；未到 push_hour 不推；同北京日只推一次；跨天再推；
  失败不置日期（次日重试）；不抛（不阻断 scanner loop）。
- 路由 `/api/push/test` 单测（mock send_telegram：成功/失败）。
- 前端无单测，主会话渲染走查（配置区显示、测试按钮）。

## 七、不做（YAGNI）

- 只做 Telegram 一个渠道（不做企业微信/钉钉/邮件/多渠道抽象）。
- 只报「昨天」定稿汇总，不做「今天实时」推送。
- 不做富文本/图表/图片，纯文本消息。
- 不做每钱包独立推送目标（全局一个 Telegram 目标）。
- 不加专用「推送代理」配置（复用扫描钱包代理）；不改任何交易逻辑。
- token 明文存（与 proxy 一致，本地单用户库），不做单独加密。
