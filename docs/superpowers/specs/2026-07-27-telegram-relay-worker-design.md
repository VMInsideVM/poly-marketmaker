# 周报推送改走中继（Cloudflare Worker）设计 / spec

> 日期：2026-07-27　　状态：已批准，待写实现计划

## 一、背景

`config.py` 里写死的 Telegram bot token 随开源仓库上了 GitHub，被人扒走并用作者的 bot 发了消息
（2026-07-27 实报）。旧 token 已 revoke。

原设计的取舍写在 `config.py:46-47` 的注释里：token 随源码分发、可被提取，但这个 bot 只单向发通知到
固定 chat，风险被判定为可接受。这次事件说明这个判断偏乐观：能提取的人确实会提取，而且会用。

**加密、混淆、只放进编译后的 exe，全都无效。** 程序要能用 token 发消息，就必须能把它还原成明文，
还原所需的一切都在分发出去的程序里。这些手段只是把「复制粘贴就拿到」变成「花十分钟拿到」。

损失范围（校准用）：bot token 泄露能让人以该 bot 名义发消息、读该 bot 收到的消息、改 bot 资料。
拿不到作者的 Telegram 账号、私聊、手机号，与钱包私钥（本地 AES-256-GCM 加密，从不出机器）完全无关。
性质是骚扰，不是数据泄露。

## 二、目标与约束

1. 使用者更新程序后**零配置**继续运行，周报照常汇总到作者这里。
2. Telegram token **一次都不出现在客户端**，不进仓库、不进 exe。
3. 出事时作者能**不发版**就止损。

第 3 条是这个方案真正的价值。客户端里的任何东西都会泄露，这一点无法改变；能改变的是泄露之后
作者还剩多少控制权。

## 三、架构

```
使用者的电脑                      作者的 Cloudflare Worker              Telegram
_maybe_push_weekly                （token 只存在这里的环境变量）
  组装结构化数字  ──POST JSON──>  ENABLED 未止损 → 校验 key → 校验白名单(可选)→ 清洗字段
                                  → 用 JS 拼周报文本  ──────────────>  作者的 chat
```

客户端不再知道 token，也不再知道 chat_id，只知道一个 Worker URL 和一个 `REPORT_KEY`。

## 四、客户端改动

### 4.1 `config.py`

删 `TG_BOT_TOKEN`、`TG_CHAT_ID`。新增：

```python
# 周报中继(Cloudflare Worker)。Telegram token/chat 只存在 Worker 的环境变量里,客户端
# 一概不知道 —— 写死在这里的两个值会随源码上 GitHub,**都不是秘密**:
#   REPORT_URL  Worker 地址,公开可见
#   REPORT_KEY  只用来把随机扫描 workers.dev 的爬虫挡在门外,不是鉴权凭证
# 真正的防线是 Worker 侧的 ENABLED 止损开关(设成 "0"、几十秒传播延迟,按下后要发一次测试
# 请求、收到 503 才算停住,不用发版),以及「改 Worker 不用发版」这件事本身。
REPORT_URL = "https://<待部署>.workers.dev"
REPORT_KEY = "<部署时生成>"
PUSH_HOUR = 9  # 保留:客户端决定几点后推
```

### 4.2 `engine/notify.py`

`format_weekly_report` 换成 `build_report_payload(...) -> dict`：同样的入参，返回结构化 dict 而非文本。
排版挪进 Worker。

`send_telegram(token, chat_id, text, proxy)` 换成 `send_report(payload, proxy)`：POST JSON 到
`REPORT_URL`，头带 `X-MM-Key: REPORT_KEY`，超时沿用现有的 15 秒，失败抛。

保留代理参数。`workers.dev` 在国内与 Telegram API 一样可能不通，仍需走钱包代理。现有那条
「异常绝不带请求 URL 进日志」的消毒继续保留：URL 里已经没有 token，但保持这个习惯没有坏处。

### 4.3 `engine/manager.py`

`_maybe_push_weekly` 里 `text = format_weekly_report(...)` 换成 `payload = build_report_payload(...)`，
并补上 `senders`（见 4.4）；`_send_report` 的签名从 `(token, chat_id, text, week_key, proxy)` 变成
`(payload, week_key, proxy)`，函数体调 `send_report`。

后台线程、`_pushing` 防重入、成功才持久化 `last_push_week`、失败只 WARNING 且绝不抛进 loop，这些
现有行为一概不动。

### 4.4 payload 结构

```json
{
  "v": 1,
  "senders": ["0xabc...", "0xdef..."],
  "week_start": "2026-07-20",
  "week_end": "2026-07-26",
  "daily_nets": [["2026-07-20", 1.23], ["2026-07-21", -0.45]],
  "week_totals": {"reward": 0, "rebate": 0, "sell_profit": 0, "loss": 0, "fee": 0, "net": 0},
  "cumulative_net": 123.45,
  "since_date": "2026-05-17",
  "per_wallet": [{"label": "备注或地址缩写", "net": 1.2}]
}
```

`senders` = `db.list_wallets()` 的全部地址（小写），**不只是本周有活动的那些**。Worker 只要发现其中
任意一个在白名单里就放行，这样使用者增删钱包不会让推送突然失效。**数据面变更**：改造前外发文本里
只有本周有活动钱包的截断地址（如 `0x1234...abcd`）；现在每周上传的是使用者**全部**钱包（含本周
没动的）的**完整**地址，供 Worker 将来不发版就能启用 `ALLOW`——理由站得住，但这是一次此前没记录
的数据面变更。

## 五、Worker 侧

代码进仓库 `deploy/report-worker.js`，一起版本管理。文件里没有任何密钥。

### 5.1 环境变量（Cloudflare 控制台配置，四个）

| 变量 | 类型 | 内容 |
|---|---|---|
| `TG_TOKEN` | Secret | Telegram bot token，部署时新生成，全程不经过仓库 |
| `TG_CHAT_ID` | 普通 | 作者的 chat id |
| `CLIENT_KEY` | 普通 | 与客户端 `REPORT_KEY` 相同；随机生成即可（如 32 位十六进制） |
| `ENABLED` | 普通 | 止损开关：设成 `0` 全局停止转发（几十秒传播延迟，按下后要发一次测试请求、收到 503 才算停住）。不设即为开启，**部署时不必配** |
| `ALLOW` | 普通 | 钱包地址白名单，逗号分隔、小写。**可选，默认留空 = 不检查** |

前三个必填，后两个平时不用配。

`ALLOW` 之所以不是必填：作者并不知道使用者有哪些钱包地址，而使用者会随时导入新钱包、删除旧钱包。
要求逐个登记等于派一个维护不起的活，还会在朋友加了新钱包时让周报无声无息地断掉。这道防线的价值
撑不起它的维护成本。它留给「出事之后想收紧」时用——那时从正常收到的周报里就能看出合法地址长什么样。

### 5.2 校验链

`ENABLED != "0"` → 必须 POST → `X-MM-Key` 与 `CLIENT_KEY` 相等 → payload 是对象 →
（`ALLOW` 非空时）`senders` 与 `ALLOW` 有交集 → 三个日期字段格式合法 → 清洗字段 → 拼文本
→ 调 Telegram `sendMessage`。任何一步不过就返回 4xx/5xx 且不转发。

响应体只回 `ok` / `no`，不回显任何环境变量内容。

### 5.3 字段清洗（这一步是本方案的要害）

周报「各钱包本周净利润」一栏的 `label` 来自使用者可编辑的钱包备注，是**自由文本**。原样拼进消息
就等于把「让作者的 bot 发任意内容」这个能力还了回去，而那正是这次事件的形态。

- `label`：剥掉换行和控制字符，截断到 20 字符。
- 所有数值字段：强制 `Number()`，`NaN`/`Infinity` 归 0，绝对值超过 1e9 归 0。
- `daily_nets`：最多 7 条；`per_wallet`：最多 50 条。
- 日期字段：只接受 `^\d{4}-\d{2}-\d{2}$`，不匹配就整条拒绝。

### 5.4 限流：v1 不做

限流要额外挂 Cloudflare KV，多一步配置。默认配置（`ALLOW` 留空）下，能灌进来的人只需扒出
`REPORT_URL` 和 `REPORT_KEY`，而这两样都在公开源码里——所以这里必须把最坏情况说清楚：

他能做的极限**不是**「只有数字加一条备注」——`per_wallet` 上限 50 条、每条 label 上限 20 字符
（剥掉控制字符），但这 50 条的文字都由攻击者任选，一行一句拼起来就是一条几十行、上千字符的
自选文字消息（实测构造出 68 行 / 1284 字符，其中 50 行是自选文字）。发不到别的 chat，读不到
任何东西，仍然只是骚扰，不是泄露——风险接受的结论不变。将来若真被反复骚扰，收紧 `per_wallet`
上限比上限流更便宜。

止损手段是把 `ENABLED` 设成 `0`：几十秒传播延迟，按下后要发一次测试请求、收到 503 才算停住，
不用发版、不影响任何人的交易。要恢复就换一个 `CLIENT_KEY` 发版，或者到那时再填 `ALLOW`。这就是
中继相比「token 写死在客户端」的核心差别（最坏情况从「别人完全控制你的 bot」降到「别人能发假
数字而你一键能停」），也是不急着上限流的理由。将来真被反复骚扰再加。

## 六、部署与发版

1. Cloudflare → Workers & Pages → Create Worker → 粘贴 `deploy/report-worker.js` → Deploy。
2. Settings → Variables 加**前三个**（`TG_TOKEN` 选 Secret 类型）；`ENABLED` 与 `ALLOW` 不必配。
3. BotFather 生成**全新** token，直接填进 Cloudflare，不要经过仓库、聊天记录或截图。
4. 把 Worker URL 和 `REPORT_KEY` 填进 `config.py`，发版。

使用者更新后自动生效，零配置。**旧版本客户端会继续用已 revoke 的旧 token 尝试推送**，请求失败、
只记一行 WARNING、不影响交易，也不会有任何东西发到作者这里，属于可接受的过渡状态。

`config.py` 里那个已失效的旧 token 顺手删掉。git 历史里的那一份清不掉（要 rewrite history +
force push，代价大），但既已 revoke，留着无害。

## 七、测试

`tests/test_notify.py` 改成测 `build_report_payload`：字段齐全、数值正确、`per_wallet` 的 label
按现有规则回退到地址缩写、`senders` 全小写。再加 `send_report` 的 mock 测试：POST 的 URL、
`X-MM-Key` 头、JSON body 正确，非 2xx 时抛且异常消息不含 URL。

`tests/test_weekly_push.py` 里对 `format_weekly_report` / `send_telegram` 的引用改成新函数。

Worker 侧有 `deploy/report-worker.test.mjs`（`node --test "deploy/*.test.mjs"`，用 node 内置的
`node:test` + `node:assert`，stub 掉全局 `fetch`，零依赖、无构建步骤），覆盖急停的多种写法、
方法/鉴权、畸形 body、日期格式、`ALLOW` 空与不空、label 的控制字符剥离与按码点截断、`chat_id`
不可被 payload 覆盖。

> 这一节最初写的是「为它搭 JS 测试环境不值得，靠人工验收」。那个判断是在**没试过**的前提下做的：
> 实际成本就是一个文件，而它能把 label 清洗漏字符、急停只认 `"0"` 这类回归挡在「粘进 Cloudflare
> 控制台」之前——两者都是 2026-07-27 整分支审查真找出来的。判断已推翻，记录在此。

人工验收仍然不能省，但换了内容：部署后由作者触发一次真实周报，肉眼确认排版、数字、中文都对；
再验一次急停（`ENABLED=0` → 收到 503 且 Telegram 收不到 → 解除后恢复）。自动化测试跑不到
「Cloudflare 环境变量是否真配对了」和「Telegram 那侧渲染成什么样」这两件事。

## 八、不改的东西

台账计算（`engine/pnl.py` / `engine/pnl_ledger.py`）、推送时机与节流（`PUSH_HOUR`、
`weekly_window`、`last_push_week` 持久化）、后台线程模型、交易链路一概不动。
