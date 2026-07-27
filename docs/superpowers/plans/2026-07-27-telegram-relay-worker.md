# 周报推送改走中继 Worker 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把每周盈亏周报的推送链路从「客户端直连 Telegram（token 写死在源码里）」改成「客户端 POST 结构化数据给 Cloudflare Worker，由 Worker 用只存在它环境变量里的 token 转发」，使 Telegram token 一次都不出现在分发出去的程序里。

**Architecture:** 客户端只知道一个 Worker URL 和一个非秘密的 `REPORT_KEY`；周报文本在 Worker 侧拼装，客户端只传数字，所以扒出凭证的人最多往模板里塞假数字，无法让 bot 发任意内容。出事时把 Worker 的 `ENABLED` 设成 `0` 即可全局止损，不需要给使用者发新版本。

> **执行中的设计变更（2026-07-27，用户提出）**：`ALLOW` 钱包地址白名单由必填改为**可选、默认留空**，另加 `ENABLED` 止损开关。原因是作者并不知道使用者有哪些钱包地址、而使用者会随时增删，逐个登记维护不起，还会在朋友加了新钱包时让周报无声无息地断掉。Task 1 Step 4 和 Task 3 Step 4 保留了变更前的形态（本文件是执行记录）；**以 spec 5.1/5.2/5.4 与 `deploy/report-worker.js` 的当前内容为准**。

**Tech Stack:** Python 3.11 / requests / pytest；Cloudflare Workers（原生 JS，无构建步骤、无依赖）。

## Global Constraints

- **spec 见** `docs/superpowers/specs/2026-07-27-telegram-relay-worker-design.md`，与本计划冲突时以 spec 为准。
- **分支已开好**：`feat/telegram-relay-worker`（spec 已提交在上面）。不要在 `main` 上直接实现。
- **任何真实的 token、chat id、Worker URL、REPORT_KEY 都不许写进代码或文档。** 计划里出现的一律是占位符，真值在 Task 3 由人工填入 Cloudflare 控制台和 `config.py`。
- UI 文案与注释一律简体中文，与现有代码保持一致；`CLAUDE.md` 通篇英文，写进那里的段落用英文。
- **保存的 .py 会被格式化 hook 整文件重排**。每次提交前跑 `git diff --stat`，若无关代码被重新折行就还原，只留本任务的改动。
- 文件一律 UTF-8 **无 BOM**。
- 推送是纯外发功能，**绝不能影响交易链路**：后台线程、`_pushing` 防重入、成功才持久化 `last_push_week`、失败只 WARNING 且不抛进 loop，这些现有行为一概保持。
- 跑测试：`pytest`（全量，当前基线 856 项通过）或 `pytest tests/test_notify.py -v`（单文件）。

---

### Task 1: 客户端全链路切换

这一步是原子的：`engine/notify.py` 的两个旧函数被替换，`engine/manager.py` 的调用点和 `config.py` 的常量必须同时改，否则 import 直接断。所以四个源文件加两个测试文件在同一个任务里改完。

**Files:**
- Modify: `api/proxy.py`（`http_get` 之后新增 `http_post`）
- Modify: `config.py:46-50`（删两个 Telegram 常量，加两个中继常量）
- Modify: `engine/notify.py`（整文件替换两个函数）
- Modify: `engine/manager.py:16,24`（import）、`:930-953`（`_maybe_push_weekly` 尾段）、`:958-967`（`_send_report`）
- Test: `tests/test_notify.py`（整文件重写）、`tests/test_weekly_push.py`（改 patch 目标与断言）

**Interfaces:**
- Consumes: `api.proxy.use_proxy(url)` 上下文管理器、`api.proxy.current_proxy` contextvar、`api.proxy._retry_on_connect_error(fn)`（均已存在）
- Produces:
  - `api.proxy.http_post(url, **kw) -> requests.Response`
  - `engine.notify.build_report_payload(week_start, week_end, daily_nets, week_totals, cumulative_net, per_wallet, since_date, senders) -> dict`
  - `engine.notify.send_report(payload: dict, proxy=None) -> None`
  - `config.REPORT_URL: str`、`config.REPORT_KEY: str`
  - Task 2 的 Worker 消费 `build_report_payload` 产出的 JSON 结构，字段名必须与这里完全一致。

**命名提醒：** `engine/manager.py` 里已有一个**方法** `_send_report`（后台线程体），与 `engine/notify.py` 里新增的**函数** `send_report` 不是一回事。方法名保持 `_send_report` 不变，它的函数体从调 `send_telegram` 改成调 `send_report`。

- [ ] **Step 1: 写失败的测试（notify）**

整个替换 `tests/test_notify.py` 的内容为：

```python
"""tests/test_notify.py — 周报中继:payload 纯函数 + send 薄封装。

客户端不再持有 Telegram token,只把结构化数字 POST 给中继 Worker(排版在 Worker 侧)。
"""

from unittest.mock import patch, MagicMock

from engine.notify import build_report_payload, send_report


def _payload(**over):
    args = {
        "week_start": "2026-07-06",
        "week_end": "2026-07-12",
        "daily_nets": [("2026-07-06", 2.0), ("2026-07-07", -1.0)],
        "week_totals": {
            "reward": 7.21,
            "rebate": 0.5,
            "sell_profit": 2.0,
            "loss": 1.0,
            "fee": 0.1,
            "net": 8.61,
        },
        "cumulative_net": 123.45,
        "per_wallet": [{"label": "主号", "net": 5.0}],
        "since_date": "2026-07-01",
        "senders": ["0xAAAA1111bbbbCCCC"],
    }
    args.update(over)
    return build_report_payload(**args)


def test_payload_carries_every_field():
    p = _payload()
    assert p["v"] == 1
    assert p["week_start"] == "2026-07-06" and p["week_end"] == "2026-07-12"
    assert p["daily_nets"] == [["2026-07-06", 2.0], ["2026-07-07", -1.0]]
    assert p["week_totals"]["net"] == 8.61 and p["week_totals"]["loss"] == 1.0
    assert p["cumulative_net"] == 123.45
    assert p["since_date"] == "2026-07-01"
    assert p["per_wallet"] == [{"label": "主号", "net": 5.0}]


def test_senders_lowercased_and_blanks_dropped():
    """Worker 按小写地址查白名单;空项会让白名单判断出现假阳性,先在客户端剔掉。"""
    p = _payload(senders=["0xAAAA1111bbbbCCCC", "", None, "0xBBBB"])
    assert p["senders"] == ["0xaaaa1111bbbbcccc", "0xbbbb"]


def test_numbers_coerced_to_float():
    """None/字符串数字统一成 float,免得 JSON 里混进 null 让 Worker 的模板出 NaN。"""
    p = _payload(
        cumulative_net=None,
        week_totals={"reward": "3", "rebate": None},
        daily_nets=[("2026-07-06", None)],
        per_wallet=[{"label": "x", "net": None}],
    )
    assert p["cumulative_net"] == 0.0
    assert p["week_totals"]["reward"] == 3.0
    assert p["week_totals"]["rebate"] == 0.0
    assert p["week_totals"]["net"] == 0.0  # 缺的键补 0,六个键必须齐
    assert p["daily_nets"] == [["2026-07-06", 0.0]]
    assert p["per_wallet"] == [{"label": "x", "net": 0.0}]


def test_empty_wallets_yields_empty_list():
    assert _payload(per_wallet=[])["per_wallet"] == []


def test_label_not_sanitised_client_side():
    """客户端**不**做清洗:改过客户端的人绕得过去,清洗只在 Worker 侧才有意义。
    这条测试钉住这个分工,免得有人「顺手」在这里加截断而误以为安全。"""
    p = _payload(per_wallet=[{"label": "a" * 50 + "\n<script>", "net": 1}])
    assert p["per_wallet"][0]["label"] == "a" * 50 + "\n<script>"


def test_send_report_posts_json_with_key():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    with patch("engine.notify.http_post", return_value=resp) as post, patch(
        "engine.notify.REPORT_URL", "https://relay.example/report"
    ), patch("engine.notify.REPORT_KEY", "KEY123"):
        send_report({"v": 1}, proxy=None)
    assert post.call_args.args[0] == "https://relay.example/report"
    assert post.call_args.kwargs["json"] == {"v": 1}
    assert post.call_args.kwargs["headers"] == {"X-MM-Key": "KEY123"}
    assert post.call_args.kwargs["timeout"] == 15


def test_send_report_http_error_does_not_leak_url_or_key():
    """requests 的 HTTPError 默认把 URL 塞进消息,经调用方 logger.warning 会进日志文件。
    URL 里已经没有 token 了,但请求头里有 REPORT_KEY,保持只回状态码的写法。"""
    import requests

    resp = MagicMock()
    err = requests.exceptions.HTTPError(
        "403 Client Error for url: https://relay.example/report"
    )
    err.response = MagicMock(status_code=403)
    resp.raise_for_status.side_effect = err
    with patch("engine.notify.http_post", return_value=resp), patch(
        "engine.notify.REPORT_URL", "https://relay.example/report"
    ), patch("engine.notify.REPORT_KEY", "KEY123"):
        try:
            send_report({"v": 1})
            assert False, "应抛"
        except Exception as e:
            assert "relay.example" not in str(e)
            assert "KEY123" not in str(e)
            assert "403" in str(e)


def test_send_report_raises_on_connection_error():
    import requests

    with patch(
        "engine.notify.http_post",
        side_effect=requests.exceptions.ConnectionError("boom"),
    ):
        try:
            send_report({"v": 1})
            assert False, "应抛"
        except RuntimeError:
            pass
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_notify.py -v`
Expected: FAIL，collection 阶段就报 `ImportError: cannot import name 'build_report_payload' from 'engine.notify'`。

- [ ] **Step 3: 给 `api/proxy.py` 加 `http_post`**

在 `http_get`（第 89-98 行）之后插入：

```python
def http_post(url, **kw):
    """requests.post 包装:current_proxy 非空时注入 proxies=(http/https 同一代理)。

    与 http_get 同源。_retry_on_connect_error 只重试「连接从未建立」的失败——请求一次都
    没送达过,所以对 POST 同样安全,不会重复提交。
    """
    proxy = current_proxy.get()
    if proxy:
        kw.setdefault("proxies", {"http": proxy, "https": proxy})
    return _retry_on_connect_error(lambda: requests.post(url, **kw))
```

- [ ] **Step 4: 换 `config.py` 的常量**

把 `config.py:45-50`（`# 每日盈亏日报远程推送(Telegram)…` 那段注释加 `TG_BOT_TOKEN`/`TG_CHAT_ID`/`PUSH_HOUR` 三行）整段替换成：

```python
# 周报中继(Cloudflare Worker)。Telegram token/chat 只存在 Worker 的环境变量里,客户端
# 一概不知道。写死在这里的两个值会随源码上 GitHub,**都不是秘密**:
#   REPORT_URL  Worker 地址,公开可见
#   REPORT_KEY  只用来把随机扫描 workers.dev 的爬虫挡在门外,不是鉴权凭证
# 真正的防线是 Worker 侧的钱包地址白名单(ALLOW),以及「改 Worker 不用发版」这件事本身。
# ⚠️ 已变更,见顶部说明:ALLOW 后来改为可选、默认留空,真正的止损开关是 ENABLED。
# 历史:上一版把 bot token 写死在这里,被人从公开仓库扒走盗用(2026-07-27),已 revoke。
REPORT_URL = "https://REPLACE-ME.workers.dev"
REPORT_KEY = "REPLACE-ME"
PUSH_HOUR = 9  # 北京时间几点后推(8点奖励到账之后)
```

两个 `REPLACE-ME` 占位值由 Task 3 部署后填真值。在那之前推送会因域名解析失败而报 WARNING，不影响交易，测试全是 mock 不受影响。

- [ ] **Step 5: 重写 `engine/notify.py`**

整个文件替换为：

```python
"""engine/notify.py — 每周盈亏周报推送(经中继 Worker)。payload 纯函数;send 薄封装。

纯外发,不改任何交易逻辑。**客户端不持有 Telegram token**:数据 POST 给中继 Worker,由它
用只存在自己环境变量里的 token 转发到 Telegram(见 deploy/report-worker.js)。周报文本也
在 Worker 侧拼,客户端只传结构化数字——这样即使有人扒出 REPORT_URL 和 REPORT_KEY,他能做
的极限是往模板里塞假数字,而不是让 bot 发任意内容。
"""

import requests

from api.proxy import use_proxy, http_post
from config import REPORT_URL, REPORT_KEY

_TOTAL_KEYS = ("reward", "rebate", "sell_profit", "loss", "fee", "net")


def build_report_payload(
    week_start,
    week_end,
    daily_nets,
    week_totals,
    cumulative_net,
    per_wallet,
    since_date,
    senders,
) -> dict:
    """组装「上一周」周报的结构化 payload(排版在 Worker 侧做)。

    daily_nets=[(date, net), …];week_totals=全钱包本周 6 类别 dict;per_wallet=[{label, net}];
    since_date=累计起点;senders=本机全部钱包地址(供 Worker 查白名单,统一小写、剔空)。

    数值一律转 float(免得 JSON 里混进 null 让 Worker 的模板出 NaN),字符串**不做清洗**:
    清洗是 Worker 的职责,客户端这边做了也挡不住改过客户端的人。
    """

    def f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    return {
        "v": 1,
        "senders": [str(a).lower() for a in (senders or []) if a],
        "week_start": week_start,
        "week_end": week_end,
        "daily_nets": [[d, f(n)] for d, n in (daily_nets or [])],
        "week_totals": {k: f((week_totals or {}).get(k)) for k in _TOTAL_KEYS},
        "cumulative_net": f(cumulative_net),
        "since_date": since_date,
        "per_wallet": [
            {"label": w.get("label", ""), "net": f(w.get("net"))}
            for w in (per_wallet or [])
        ],
    }


def send_report(payload, proxy=None) -> None:
    """把周报 payload POST 给中继 Worker。失败抛。

    ⚠️ 异常消息**绝不能带请求 URL 或请求头**:requests 的 HTTPError/连接错误默认把 URL 塞
    进消息,会经调用方 logger.warning 泄进日志文件。URL 本身不是秘密,但 REPORT_KEY 在头里,
    沿用上一版(URL 含 bot token 时)的消毒写法,只保留状态码。
    """
    try:
        with use_proxy(proxy):
            resp = http_post(
                REPORT_URL,
                json=payload,
                headers={"X-MM-Key": REPORT_KEY},
                timeout=15,
            )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        code = getattr(getattr(e, "response", None), "status_code", "?")
        raise RuntimeError(f"周报中继请求失败(HTTP {code})") from None
```

- [ ] **Step 6: 跑 notify 测试确认通过**

Run: `pytest tests/test_notify.py -v`
Expected: PASS，9 项全绿。

- [ ] **Step 7: 改 `engine/manager.py` 的 import**

第 16 行：

```python
from config import CATEGORY_CATALOG, PUSH_HOUR
```

第 24 行：

```python
from engine.notify import build_report_payload, send_report
```

- [ ] **Step 8: 改 `_maybe_push_weekly` 的尾段**

现在的 `per_wallet` 循环开头是 `for w in self.db.list_wallets():`。先把钱包列表取出来复用（`senders` 要用它，不该再查一次库）：

```python
            wallets = self.db.list_wallets()
            per_wallet = []
            for w in wallets:
```

循环体不动。循环之后，把 `text = format_weekly_report(...)` 那一整段（到 `PNL_START_DATE,` 收尾的右括号）替换成：

```python
            payload = build_report_payload(
                week_start,
                week_end,
                daily_nets,
                week_totals,
                cum,
                per_wallet,
                PNL_START_DATE,
                [w["address"] for w in wallets],
            )
```

再把线程的 `args=` 改成：

```python
                args=(payload, week_key, proxy),
```

`proxy = ...`、`self._pushing = True`、`threading.Thread(...)`、`.start()` 都不动。

- [ ] **Step 9: 改 `_send_report` 方法**

替换成：

```python
    def _send_report(self, payload, week_key, proxy):
        """后台线程体:把周报 payload 发给中继 Worker。成功才**持久化** last_push_week
        (失败下轮重试、不阻塞 loop)。send_report 已消毒异常(不带 URL/请求头),故此处
        WARNING 不泄露 REPORT_KEY。"""
        try:
            send_report(payload, proxy)
            self.db.set_last_push_week(week_key)
        except Exception as e:
            logger.warning("周报推送失败: %s", e)
        finally:
            self._pushing = False
```

- [ ] **Step 10: 改 `tests/test_weekly_push.py`**

删掉第 9 行的 `from config import TG_BOT_TOKEN, TG_CHAT_ID`。

把四处 `patch("engine.manager.send_telegram"...)` 里的名字改成 `engine.manager.send_report`（三处 `as st`，一处带 `side_effect=RuntimeError("net")`）。

把 `test_push_once_per_week_recent7_and_persists` 里这三行断言：

```python
    args = st.call_args.args
    assert args[0] == TG_BOT_TOKEN and args[1] == TG_CHAT_ID
    assert "2026-07-10 ~ 2026-07-16" in args[2]  # 最近7整天(截止昨天07-16)
```

替换成：

```python
    payload = st.call_args.args[0]
    assert payload["week_start"] == "2026-07-10"  # 最近7整天(截止昨天07-16)
    assert payload["week_end"] == "2026-07-16"
    assert payload["senders"] == ["0xaaaa1111bbbbcccc"]  # 供 Worker 查白名单,小写
    assert payload["since_date"] == "2026-05-17"
```

文件顶部 docstring 末句「目标 token/chat 写死常量。」改成「token/chat 只在中继 Worker 侧,客户端不持有。」

- [ ] **Step 11: 跑全量确认零回归**

Run: `pytest`
Expected: PASS，全绿（基线 856 项，`test_notify.py` 由 5 项变 9 项，故为 860）。

若 `tests/test_weekly_push.py` 报 `senders` 断言失败，检查 Step 8 是否真的把 `wallets` 复用了（`_mgr` 夹具的 `list_wallets` 是 `MagicMock`，多调一次仍返回同样的值，所以这条不会因此挂；真挂了多半是 `[w["address"] for w in wallets]` 写错了键名）。

- [ ] **Step 12: 提交**

```bash
git diff --stat
git add api/proxy.py config.py engine/notify.py engine/manager.py tests/test_notify.py tests/test_weekly_push.py
git commit -m "feat(notify): 周报改走中继 Worker,客户端不再持有 Telegram token"
```

---

### Task 2: Worker 代码

**Files:**
- Create: `deploy/report-worker.js`

**Interfaces:**
- Consumes: Task 1 的 `build_report_payload` 产出的 JSON 结构（字段 `v` / `senders` / `week_start` / `week_end` / `daily_nets` / `week_totals` / `cumulative_net` / `since_date` / `per_wallet`），以及请求头 `X-MM-Key`
- Produces: 一个可直接粘进 Cloudflare 控制台的 Worker；Task 3 部署它

这个文件没有 Python 测试覆盖（跨语言，为它搭 JS 测试环境不值得）。验收在 Task 3 用一次真实推送做，所以这一步只需把代码写对并确认语法。

- [ ] **Step 1: 建目录与文件**

创建 `deploy/report-worker.js`，内容：

```javascript
/**
 * deploy/report-worker.js — 周报中继（Cloudflare Worker）。
 *
 * 客户端 POST 结构化周报数据到这里，本 Worker 用只存在环境变量里的 Telegram token 转发。
 * 客户端一概不持有 token / chat_id，所以扒源码或反编译 exe 都拿不到它们。
 *
 * 环境变量（Cloudflare 控制台 Settings → Variables）：
 *   TG_TOKEN     Secret 类型。Telegram bot token。
 *   TG_CHAT_ID   接收周报的 chat id。
 *   CLIENT_KEY   与客户端 config.py 的 REPORT_KEY 相同。不是鉴权凭证，只挡随机扫描。
 *   ALLOW        允许的钱包地址，逗号分隔，小写。清空它即可全局止损（30 秒生效，不用发版）。
 *   ⚠️ 已变更，见本文件顶部「执行中的设计变更」说明：ALLOW 后来改为可选、默认留空，
 *      真正的止损开关是 ENABLED。
 *
 * 安全要点：周报文本在这里拼，客户端只传数字。凡是会原样进入消息的字符串都必须先过关卡 ——
 * 三个日期字段用正则校验（不匹配整条拒绝），label 来自使用者可编辑的钱包备注、是自由文本，
 * 必须剥控制字符并截断。少做任何一样，都等于把「让 bot 发任意内容」的能力还回去，而那正是
 * 2026-07-27 那次 token 泄露事故的形态。
 */

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) && Math.abs(n) < 1e9 ? n : 0;
}

function money(v) {
  const n = num(v);
  return (n >= 0 ? "+" : "") + n.toFixed(2);
}

function label(v) {
  return String(v ?? "")
    .replace(/[\u0000-\u001f\u007f]/g, "")
    .slice(0, 20);
}

function buildText(p) {
  const lines = [
    `📊 做市周报 · ${p.week_start} ~ ${p.week_end}`,
    "",
    "【每日净利润】",
  ];
  for (const row of (p.daily_nets || []).slice(0, 7)) {
    if (!Array.isArray(row) || !DATE_RE.test(String(row[0]))) continue;
    lines.push(`${String(row[0]).slice(5)}  ${money(row[1])}`);
  }
  const t = p.week_totals || {};
  lines.push(
    "",
    "【本周汇总】",
    `做市奖励 ${money(t.reward)} · 返佣 ${money(t.rebate)} · 卖出盈利 ${money(
      t.sell_profit
    )}`,
    `亏损 ${money(-num(t.loss))} · 手续费 ${money(-num(t.fee))} · 净利润 ${money(
      t.net
    )}`,
    "",
    `【累计净利润】(自 ${p.since_date})  ${money(p.cumulative_net)}`
  );
  const pw = (p.per_wallet || []).slice(0, 50);
  if (pw.length) {
    lines.push("", "【各钱包本周净利润】");
    for (const w of pw) {
      lines.push(`${label(w && w.label)}  ${money(w && w.net)}`);
    }
  }
  return lines.join("\n");
}

export default {
  async fetch(req, env) {
    if (req.method !== "POST") return new Response("no", { status: 405 });
    if (req.headers.get("x-mm-key") !== env.CLIENT_KEY) {
      return new Response("no", { status: 403 });
    }

    let p;
    try {
      p = await req.json();
    } catch {
      return new Response("no", { status: 400 });
    }

    // 白名单:senders 里任意一个地址在 ALLOW 中即放行(使用者增删钱包不该让推送失效)。
    const allow = new Set(
      String(env.ALLOW || "")
        .split(",")
        .map((a) => a.trim().toLowerCase())
        .filter(Boolean)
    );
    const senders = Array.isArray(p.senders) ? p.senders : [];
    if (!senders.some((a) => allow.has(String(a).toLowerCase()))) {
      return new Response("no", { status: 403 });
    }

    // 三个会原样进消息的日期字段:不合格式整条拒绝(否则等于任意文本注入)。
    for (const d of [p.week_start, p.week_end, p.since_date]) {
      if (!DATE_RE.test(String(d))) return new Response("no", { status: 400 });
    }

    const resp = await fetch(
      `https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ chat_id: env.TG_CHAT_ID, text: buildText(p) }),
      }
    );
    // 只回状态,不回显 Telegram 的响应体(可能含 token 相关描述)。
    return new Response(resp.ok ? "ok" : "no", { status: resp.ok ? 200 : 502 });
  },
};
```

- [ ] **Step 2: 确认语法与编码**

Run: `node --check deploy/report-worker.js`
Expected: 无输出（语法正确）。若本机没有 node，改用 `python -c "import ast"` 之类无法校验 JS，此时人工逐行核对括号配对，并在报告里说明未能自动校验。

Run: `python -c "b=open(r'deploy/report-worker.js','rb').read(); print('BOM' if b[:3]==b'\xef\xbb\xbf' else 'no BOM'); b.decode('utf-8'); print('utf-8 ok')"`
Expected: `no BOM` 和 `utf-8 ok`。

- [ ] **Step 3: 核对字段名与客户端一致**

逐个对照 `engine/notify.py` 的 `build_report_payload` 返回的键，确认 Worker 读的是同名字段：
`senders`、`week_start`、`week_end`、`daily_nets`（二元数组的数组）、`week_totals.reward/rebate/sell_profit/loss/fee/net`、`cumulative_net`、`since_date`、`per_wallet[].label`、`per_wallet[].net`。
任何一个对不上，消息里就会出现 `undefined` 或 `+0.00`。把核对结果写进报告。

- [ ] **Step 4: 提交**

```bash
git add deploy/report-worker.js
git commit -m "feat(deploy): 周报中继 Worker(token 只存环境变量,白名单+字段清洗)"
```

---

### Task 3: 部署、验收与文档

这个任务包含**必须由人（仓库作者）操作**的步骤：Cloudflare 部署和真实推送验收。执行到这里时，把需要作者做的部分交回给他，不要试图代劳，也不要为了「跑通」而把任何真实凭证写进文件。

**Files:**
- Modify: `config.py`（把两个 `REPLACE-ME` 换成真值）
- Modify: `CLAUDE.md`（架构段新增一小节）

**Interfaces:**
- Consumes: Task 1 的 `config.REPORT_URL` / `config.REPORT_KEY`、Task 2 的 `deploy/report-worker.js`

- [ ] **Step 1: 作者在 Cloudflare 部署（人工）**

1. dash.cloudflare.com → Workers & Pages → Create → Create Worker → 起个名字 → Deploy（先部署默认模板）。
2. Edit code → 全选删掉 → 粘贴 `deploy/report-worker.js` 的内容 → Deploy。
3. Settings → Variables and Secrets，加**三个**：
   - `TG_TOKEN`：类型选 **Secret**。值是 BotFather 现在**新生成**的 token（不要用任何贴过、发过、进过仓库的 token）。
   - `TG_CHAT_ID`：接收周报的 chat id。
   - `CLIENT_KEY`：随机字符串，比如 `python -c "import secrets;print(secrets.token_hex(16))"` 生成的 32 位十六进制。

   另两个变量平时不配：`ENABLED`（止损开关，出事时才加、设成 `0`）、`ALLOW`（钱包地址白名单，
   留空即不检查——作者并不知道使用者有哪些钱包地址，而且他们会随时增删，见 spec 5.1）。
4. 记下 Worker 的 URL（形如 `https://<名字>.<子域>.workers.dev`）。

- [ ] **Step 2: 填进 `config.py`**

把 `REPORT_URL` 的 `https://REPLACE-ME.workers.dev` 换成第 1 步的真实 URL，`REPORT_KEY` 的 `REPLACE-ME` 换成第 1 步的 `CLIENT_KEY`。两者都会随源码公开，这是设计使然，不要因此改用别的存法。

- [ ] **Step 3: 真实推送验收（人工，不可跳过）**

在临时目录（不是仓库里）写一个一次性脚本，用真实的 Worker 地址发一条假数据周报，确认端到端通、排版对。脚本内容：

```python
import sys

sys.path.insert(0, r"C:\Users\Hank\PycharmProjects\poly简单做市")
from engine.notify import build_report_payload, send_report

p = build_report_payload(
    "2026-07-20",
    "2026-07-26",
    [("2026-07-20", 2.0), ("2026-07-21", -1.5)],
    {"reward": 7.21, "rebate": 0.5, "sell_profit": 2.0, "loss": 1.0, "fee": 0.1, "net": 8.61},
    123.45,
    [{"label": "主号", "net": 5.0}, {"label": "0x1234...abcd", "net": 3.61}],
    "2026-05-17",
    ["<填一个在 ALLOW 里的钱包地址>"],
)
send_report(p, proxy=None)   # 直连不通就填 parse_proxy 处理过的代理串
print("sent")
```

验收清单（Telegram 里逐项看）：
1. 收到消息，标题是 `📊 做市周报 · 2026-07-20 ~ 2026-07-26`。
2. 每日净利润两行，`07-20  +2.00` 和 `07-21  -1.50`。
3. 汇总行的六个数字对得上，亏损和手续费显示为负（`-1.00`、`-0.10`）。
4. `【累计净利润】(自 2026-05-17)  +123.45`。
5. 各钱包两行，中文备注「主号」没有乱码。

再验一次**止损开关**确实管用（这是出事时唯一的手段，没验过等于没有）：到 Cloudflare 加一个变量
`ENABLED = 0` 并 Deploy，重跑上面的脚本，应该报 `HTTP 503` 且 Telegram 收不到任何东西；然后把
`ENABLED` 删掉或改成 `1`，再跑一次确认恢复正常。

删掉临时脚本。**它含真实钱包地址，不要留在仓库里，也不要提交。**

- [ ] **Step 4: 更新 `CLAUDE.md`**

在 Architecture 段落里（`**Per-wallet HTTP proxy (api/proxy.py).**` 那一段之后）插入新的一段：

⚠️ 已变更，见本文件顶部「执行中的设计变更」说明：下面这段是当时写入 CLAUDE.md 的原文，
`allowlist (ALLOW, clearing it stops everything within seconds)` 那句已过时——ALLOW 后来
改为可选、默认留空，真正的止损开关是 ENABLED；以 CLAUDE.md 当前内容为准。

```
**Weekly report push goes through a relay, never Telegram directly.** `engine/notify.py`
POSTs a structured payload (numbers only, no prose) to a Cloudflare Worker
(`deploy/report-worker.js`); the Worker holds the Telegram bot token in its own env vars
and renders the message text itself. The client never sees the token or the chat id — an
earlier version hardcoded the token in `config.py`, it was scraped from the public repo and
abused (2026-07-27, token since revoked). `REPORT_URL` / `REPORT_KEY` in `config.py` ship
with the source and are **not secrets**: the real controls are the Worker's wallet-address
allowlist (`ALLOW`, clearing it stops everything within seconds) and the fact that changing
the Worker needs no client release. Two invariants the Worker must keep: every string that
reaches the message is either a date matched against `^\d{4}-\d{2}-\d{2}$` (whole request
rejected otherwise) or a `label` stripped of control characters and truncated to 20 chars —
`label` is a user-editable wallet remark, i.e. free text, and skipping that step hands back
the "make the bot say anything" capability the incident was about. Never move the message
rendering back to the client to avoid the duplication: that is what makes the payload
un-abusable.
```

- [ ] **Step 5: 跑全量测试**

Run: `pytest`
Expected: PASS，全绿（860 项）。`config.py` 换成真值不影响任何测试（测试全用 mock 或 patch 常量）。

- [ ] **Step 6: 提交**

```bash
git diff --stat
git add config.py CLAUDE.md
git commit -m "chore: 填入中继 Worker 地址 + CLAUDE.md 记录 token 不进客户端的不变量"
```

---

## 收尾（不属于任何 Task，由主会话决定）

- 版本号：客户端行为对使用者没有可见变化（推送本来就是静默的），但推送目标整体换了链路，按 `docs/版本号规范.md` 属**次版本**。
- 发版后旧版本客户端仍会拿已 revoke 的旧 token 尝试推送：请求失败、记一行 WARNING、不影响交易，也不会有任何东西到达作者这里。属可接受的过渡状态，不需要为它做兼容。
- `config.py` 里那个已失效的旧 token 在 Task 1 Step 4 被删掉。git 历史里的那一份清不掉（要 rewrite history + force push），既已 revoke，留着无害。
- 合并方式（直接合 main / PR / 保留分支）由用户选，不要自行合并。

## Self-Review

**1. Spec coverage**

| spec 条目 | 落点 |
|---|---|
| 三、架构（客户端只知道 URL + KEY） | Task 1 Step 4/5 |
| 4.1 `config.py` 换常量 | Task 1 Step 4；真值 Task 3 Step 2 |
| 4.2 `build_report_payload` / `send_report`、保留代理、保留异常消毒 | Task 1 Step 5 + 对应测试 |
| 4.3 `manager.py` 调用点、后台线程等行为不变 | Task 1 Step 7/8/9，`test_weekly_push.py` 守住行为 |
| 4.4 payload 结构与 `senders` 取全部钱包 | Task 1 Step 5/8，`test_senders_lowercased_and_blanks_dropped` |
| 五、Worker（环境变量、校验链、字段清洗、不做限流） | Task 2 Step 1 |
| 六、部署与发版 | Task 3 Step 1/2 + 收尾小节 |
| 七、测试（含 Worker 靠人工验收） | Task 1 Step 1/11、Task 3 Step 3 |
| 八、不改的东西 | 计划未触碰 pnl/pnl_ledger/推送时机与节流 |

**2. Placeholder scan** — 代码步骤都给了可直接粘贴的完整内容。`config.py` 里的两个 `REPLACE-ME` 是**有意的待填值**，Task 3 Step 2 明确了何时被谁替换成什么，不是含糊其辞。Task 3 Step 3 脚本里的 `<填一个在 ALLOW 里的钱包地址>` 同理：真实地址不能进计划文档。

**3. Type consistency** — `build_report_payload` 的 8 个参数顺序在 Task 1 Step 5（定义）、Step 8（manager 调用）、Task 3 Step 3（验收脚本）三处一致；`send_report(payload, proxy=None)` 在定义、`_send_report` 调用、测试三处一致；`http_post(url, **kw)` 在 Task 1 Step 3 定义、Step 5 使用；Worker 读的字段名与 Step 5 产出的键在 Task 2 Step 3 有专门的核对步骤。manager 的方法 `_send_report` 与 notify 的函数 `send_report` 同名不同物，已在 Task 1 的命名提醒里点出。
