# 每日盈亏 Telegram 日报推送 Implementation Plan（子项目2）

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development 或 executing-plans。Steps 用 `- [ ]`。

**Goal:** 每天北京 push_hour(默认9)点后，把「昨天」的全钱包盈亏汇总(6类别+累计净利润+各钱包净利)推送到 Telegram；config 页可配 token/chat_id/hour + 测试按钮。纯外发，不改交易逻辑。

**Architecture:** `engine/notify.py`(纯 `format_daily_report` + `send_telegram`)；`manager._maybe_push_daily`(挂 `_scanner_loop`，按北京日+push_hour 节流，读 settings，拉 `daily_pnl` 汇总，格式化，走扫描钱包代理发送)；`/api/push/test` 路由；config.html「远程推送」表单。

**Tech Stack:** Python、Flask、pytest；Telegram Bot API(sendMessage 走 GET，复用 `http_get`)；前端 Jinja+内联 JS(主会话手改)。

## Global Constraints

- **纯外发、不改任何交易逻辑**；`_maybe_push_daily` 整体 try/except、失败 WARNING 不阻断 `_scanner_loop`/下单。
- 报「昨天」(北京日−1)；`push_hour` 默认 9(北京，8点奖励到账后)；同北京日只推一次。
- **只 auto 模式(`_scanner_loop` 运行)时推**；Telegram POST **走扫描钱包代理**(`use_proxy(self._scanner_api.proxy_url)`)。
- 配置存 `settings` 表**明文**(与 proxy 一致)：`push_enabled`(默认False)/`tg_bot_token`/`tg_chat_id`/`push_hour`(默认9)，加入 `ENGINE_DEFAULTS` 即被 `/api/settings` 白名单自动存取。
- 数值 2 位小数、带符号；纯文本消息(不强依赖 Markdown)。含中文前端主会话手改。
- 只 Telegram 一渠道(YAGNI)。

---

## Task 1: `engine/notify.py` + `beijing_hour` + 配置键

**Files:** Create `engine/notify.py`；Modify `engine/pnl.py`(加 `beijing_hour`)、`config.py`(ENGINE_DEFAULTS 加 4 键)；Test `tests/test_notify.py`(新)、`tests/test_pnl.py`(追加)。

**Interfaces:** Produces `format_daily_report(date, totals, cumulative_net, per_wallet) -> str`、`send_telegram(token, chat_id, text, proxy=None) -> None`、`beijing_hour(ts) -> int`。

- [ ] **Step 1: 失败测试** `tests/test_notify.py`：

```python
from unittest.mock import patch, MagicMock
from engine.notify import format_daily_report, send_telegram


def test_format_daily_report_basic():
    totals = {"reward": 7.21, "rebate": 0.5, "sell_profit": 2.0, "loss": 1.0, "fee": 0.1, "net": 8.61}
    per_wallet = [{"label": "主号", "net": 5.0}, {"label": "0x1234...abcd", "net": 3.61}]
    txt = format_daily_report("2026-07-15", totals, 123.45, per_wallet)
    assert "做市日报 · 2026-07-15" in txt
    assert "净利润    +8.61" in txt
    assert "亏损      -1.00" in txt          # loss 显示为负
    assert "手续费    -0.10" in txt
    assert "累计净利润 +123.45" in txt
    assert "主号  +5.00" in txt and "0x1234...abcd  +3.61" in txt


def test_format_daily_report_empty_totals():
    zero = {k: 0 for k in ("reward", "rebate", "sell_profit", "loss", "fee", "net")}
    txt = format_daily_report("2026-07-15", zero, 0.0, [])
    assert "净利润    +0.00" in txt
    assert "【各钱包净利润】" not in txt      # 无钱包明细则不加该段


def test_send_telegram_posts_and_checks_ok():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"ok": True}
    with patch("engine.notify.http_get", return_value=resp) as g:
        send_telegram("TOK", "CHAT", "hello", proxy=None)
    url = g.call_args.args[0]
    assert "botTOK/sendMessage" in url
    assert g.call_args.kwargs["params"] == {"chat_id": "CHAT", "text": "hello"}


def test_send_telegram_raises_on_not_ok():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"ok": False, "description": "chat not found"}
    with patch("engine.notify.http_get", return_value=resp):
        try:
            send_telegram("TOK", "CHAT", "hi")
            assert False, "应抛"
        except Exception as e:
            assert "chat not found" in str(e)
```

追加 `tests/test_pnl.py`：

```python
def test_beijing_hour():
    from engine.pnl import beijing_hour
    from datetime import datetime, timezone
    # UTC 01:00 -> 北京 09:00
    assert beijing_hour(datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc).timestamp()) == 9
    # UTC 16:30 -> 北京 次日 00:30 -> 0
    assert beijing_hour(datetime(2026, 1, 1, 16, 30, tzinfo=timezone.utc).timestamp()) == 0
```

- [ ] **Step 2:** `pytest tests/test_notify.py tests/test_pnl.py -k "report or telegram or beijing_hour" -v` → FAIL。

- [ ] **Step 3: `engine/pnl.py` 加 `beijing_hour`**（`beijing_day` 之后）：

```python
def beijing_hour(ts) -> int:
    """epoch 秒 -> 北京(UTC+8)小时(0-23)。"""
    return datetime.fromtimestamp(float(ts or 0), _BJ).hour
```

- [ ] **Step 4: `engine/notify.py`**：

```python
"""engine/notify.py — 每日盈亏日报推送(Telegram)。format 纯函数;send 薄封装。"""

import logging
from api.proxy import use_proxy, http_get

logger = logging.getLogger(__name__)


def format_daily_report(date, totals, cumulative_net, per_wallet) -> str:
    """组装「昨天」日报文本。totals=全钱包6类别 dict;per_wallet=[{label,net}]。
    loss/fee 以负号展示(它们减小净利);net 已含扣减。"""
    def s(v):
        return f"{float(v or 0):+.2f}"

    lines = [
        f"📊 做市日报 · {date}",
        "",
        "【全钱包汇总】",
        f"做市奖励  {s(totals.get('reward', 0))}",
        f"做市返佣  {s(totals.get('rebate', 0))}",
        f"卖出盈利  {s(totals.get('sell_profit', 0))}",
        f"亏损      {s(-float(totals.get('loss', 0) or 0))}",
        f"手续费    {s(-float(totals.get('fee', 0) or 0))}",
        f"净利润    {s(totals.get('net', 0))}",
        "",
        f"累计净利润 {s(cumulative_net)}",
    ]
    if per_wallet:
        lines += ["", "【各钱包净利润】"]
        for w in per_wallet:
            lines.append(f"{w['label']}  {s(w['net'])}")
    return "\n".join(lines)


def send_telegram(token, chat_id, text, proxy=None) -> None:
    """发一条 Telegram 消息(sendMessage 走 GET,复用 http_get + 代理)。失败抛。"""
    with use_proxy(proxy):
        resp = http_get(
            f"https://api.telegram.org/bot{token}/sendMessage",
            params={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram 返回失败: {data}")
```

- [ ] **Step 5: `config.py` ENGINE_DEFAULTS 加键**（放在扫描/引擎参数附近）：

```python
    "push_enabled": False,
    "tg_bot_token": "",
    "tg_chat_id": "",
    "push_hour": 9,
```

- [ ] **Step 6-7:** 测试通过 + `pytest -q` 全绿；commit（`feat(notify): Telegram 日报 format+send + beijing_hour + 配置键`）。

---

## Task 2: `manager._maybe_push_daily` + 接入 `_scanner_loop`

**Files:** Modify `engine/manager.py`；Test `tests/test_daily_push.py`(新)。

**Interfaces:** `_maybe_push_daily()`(节流+开关+时辰+组装+发送，失败不抛)。

- [ ] **Step 1: 失败测试** `tests/test_daily_push.py`：

```python
from unittest.mock import MagicMock, patch
from engine.manager import EngineManager


def _mgr(settings):
    m = EngineManager.__new__(EngineManager)
    m.db = MagicMock()
    m.db.get_settings.return_value = settings
    m.db.get_daily_pnl_all.return_value = [
        {"date": "2026-07-15", "reward": 7, "rebate": 0, "sell_profit": 0, "loss": 0, "fee": 0, "net": 7}
    ]
    m.db.get_daily_pnl.return_value = [{"date": "2026-07-15", "net": 7}]
    m.db.list_wallets.return_value = [{"address": "0xAAAA1111bbbb", "remark": "主号"}]
    m._scanner_api = MagicMock(proxy_url=None)
    m._last_push_date = None
    return m


S_ON = {"push_enabled": True, "tg_bot_token": "T", "tg_chat_id": "C", "push_hour": 9}


def test_no_push_when_disabled():
    m = _mgr({**S_ON, "push_enabled": False})
    with patch("engine.manager.send_telegram") as st, patch("engine.manager.beijing_hour", return_value=10):
        m._maybe_push_daily()
    st.assert_not_called()


def test_no_push_before_hour():
    m = _mgr(S_ON)
    with patch("engine.manager.send_telegram") as st, patch("engine.manager.beijing_hour", return_value=8), \
         patch("engine.manager.beijing_day", return_value="2026-07-16"):
        m._maybe_push_daily()
    st.assert_not_called()


def test_push_once_per_day_after_hour():
    m = _mgr(S_ON)
    with patch("engine.manager.send_telegram") as st, patch("engine.manager.beijing_hour", return_value=10), \
         patch("engine.manager.beijing_day", return_value="2026-07-16"):
        m._maybe_push_daily()
        m._maybe_push_daily()  # 同日第二次 -> 节流
    assert st.call_count == 1
    # 发的是昨天(2026-07-15)、目标 token/chat 正确
    args = st.call_args.args
    assert args[0] == "T" and args[1] == "C" and "2026-07-15" in args[2]


def test_send_failure_no_raise_and_retries():
    m = _mgr(S_ON)
    with patch("engine.manager.send_telegram", side_effect=RuntimeError("net")), \
         patch("engine.manager.beijing_hour", return_value=10), patch("engine.manager.beijing_day", return_value="2026-07-16"):
        m._maybe_push_daily()  # 不抛
    assert m._last_push_date is None  # 失败未置日期 -> 次日/下轮重试
```

- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 实现**（`engine/manager.py`）：顶部加 `from engine.notify import format_daily_report, send_telegram`、`from engine.pnl import beijing_hour`（`beijing_day` 已导）、`from engine.pnl import _prev_day`（或加公共 `prev_day`）。`EngineManager.__init__` 加 `self._last_push_date = None`。新增：

```python
    def _maybe_push_daily(self):
        """每日 push_hour 点后推「昨天」盈亏日报到 Telegram(全局一次/天)。整体 try/except、
        失败 WARNING 不置日期 -> 次日/下轮重试;绝不阻断 _scanner_loop/下单。"""
        try:
            s = self.db.get_settings()
            if not s.get("push_enabled"):
                return
            now = time.time()
            today = beijing_day(now)
            if self._last_push_date == today or beijing_hour(now) < int(s.get("push_hour", 9) or 9):
                return
            yesterday = _prev_day(today)
            KEYS = ("reward", "rebate", "sell_profit", "loss", "fee", "net")
            rows = self.db.get_daily_pnl_all(yesterday, yesterday)
            totals = rows[0] if rows else {k: 0.0 for k in KEYS}
            cum = sum(r["net"] for r in self.db.get_daily_pnl_all(PNL_START_DATE, yesterday))
            per_wallet = []
            for w in self.db.list_wallets():
                wr = self.db.get_daily_pnl(w["address"], yesterday, yesterday)
                if not wr or not any(wr[0].get(k) for k in KEYS):
                    continue  # 当天无任何活动的钱包不列
                addr = w["address"]
                label = (w.get("remark") or "").strip() or f"{addr[:6]}...{addr[-4:]}"
                per_wallet.append({"label": label, "net": wr[0]["net"]})
            text = format_daily_report(yesterday, totals, cum, per_wallet)
            proxy = getattr(self._scanner_api, "proxy_url", None) if self._scanner_api else None
            send_telegram(s.get("tg_bot_token", ""), s.get("tg_chat_id", ""), text, proxy)
            self._last_push_date = today
        except Exception as e:
            logger.warning("日报推送失败: %s", e)
```

在 `_scanner_loop` 循环体内、`_place_round()` 之后加 `self._maybe_push_daily()`。若 `_prev_day` 用私有导入不妥，可在 `engine/pnl.py` 把 `_prev_day` 也导出为公共 `prev_day` 并在此用之。

- [ ] **Step 4-7:** 测试通过 + `pytest -q` + commit（`feat(push): 每日 Telegram 日报接入 _scanner_loop`）。

---

## Task 3: `/api/push/test` 路由

**Files:** Modify `web/routes.py`；Test `tests/test_push_route.py`(新，仿 test_wallet_remark_routes 夹具)。

**Interfaces:** `POST /api/push/test` → 读 settings token/chat_id，发一条固定测试消息(走第一个钱包代理或直连)，返回 `{ok}` 或 `{error}`。

- [ ] **Step 1: 失败测试**：

```python
import web.routes as routes
from models.database import Database


def _client(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "t.db")); db.init()
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    routes.app.config["TESTING"] = True
    c = routes.app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
    return c, db


def test_push_test_sends(tmp_path, monkeypatch):
    c, db = _client(tmp_path, monkeypatch)
    db.save_settings({"tg_bot_token": "T", "tg_chat_id": "C"})
    with monkeypatch.context() as mp:
        called = {}
        mp.setattr(routes, "send_telegram", lambda *a, **k: called.setdefault("hit", a))
        r = c.post("/api/push/test")
    assert r.get_json()["ok"] is True
    assert called["hit"][0] == "T" and called["hit"][1] == "C"


def test_push_test_missing_token(tmp_path, monkeypatch):
    c, db = _client(tmp_path, monkeypatch)
    r = c.post("/api/push/test")
    assert r.status_code == 400 and "error" in r.get_json()
```

- [ ] **Step 2:** FAIL。
- [ ] **Step 3: 实现**（`web/routes.py`，顶部或局部 `from engine.notify import send_telegram`）：

```python
@app.route("/api/push/test", methods=["POST"])
@login_required
def api_push_test():
    from engine.notify import send_telegram

    s = db.get_settings()
    token, chat_id = s.get("tg_bot_token", ""), s.get("tg_chat_id", "")
    if not token or not chat_id:
        return jsonify({"error": "请先填写 Telegram token 和 chat_id"}), 400
    proxy = None
    if manager and getattr(manager, "_scanner_api", None):
        proxy = getattr(manager._scanner_api, "proxy_url", None)
    try:
        send_telegram(token, chat_id, "✅ 做市助手测试推送：配置成功", proxy)
    except Exception as e:
        return jsonify({"error": f"发送失败：{e}"}), 200
    return jsonify({"ok": True})
```

（`send_telegram` 也在模块顶部 import 一次，供测试 `monkeypatch.setattr(routes, "send_telegram", ...)` 打桩——即路由内改为直接调用模块级名 `send_telegram(...)`，不用局部 import。见下：顶部 `from engine.notify import send_telegram`，路由体去掉局部 import。）

- [ ] **Step 4-7:** 测试通过 + `pytest -q` + commit（`feat(api): /api/push/test 立即发测试消息`）。

---

## Task 4: config.html「远程推送」表单 + 测试按钮（主会话手改）

**Files:** Modify `web/templates/config.html`。

- [ ] **Step 1:** 加「远程推送」`<section>`(独立表单，不搅进 engine-form)：开关 checkbox `push_enabled`、`tg_bot_token`(type=password)、`tg_chat_id`、`push_hour`(number)、「保存推送设置」按钮、「测试推送」按钮 + 结果提示 span。
- [ ] **Step 2:** JS：`loadPush()`(fetch /api/settings 填值：checkbox 用 `.checked`、其余 `.value`)；`savePush()`(收 4 字段 POST /api/settings：`push_enabled` 布尔、`tg_bot_token`/`tg_chat_id` 字符串、`push_hour` parseInt)；`testPush()`(POST /api/push/test，showToast/提示成功或 error)。页面初始化调 `loadPush()`。
- [ ] **Step 3: 校验**：无 BOM、node 读模板、渲染 `/config` 200 + 关键标记(`push-enabled`/`testPush`/`tg-bot-token`)。
- [ ] **Step 4: 提交**（`feat(ui): 配置页远程推送(Telegram)表单 + 测试按钮`）。

---

## Self-Review

**Spec coverage**：Telegram send+format→T1；beijing_hour→T1；配置键→T1；每日节流/时辰/昨天/走代理/失败不阻断→T2；测试按钮路由→T3；config 表单→T4；只 auto 模式(挂 _scanner_loop)→T2 接入点；明文存/白名单→T1 ENGINE_DEFAULTS。✓
**Placeholder**：无 TBD;`_prev_day` 导入方式给了两个明确选项(私有导入 or 导出 prev_day)，实现时择一。
**Type consistency**：`format_daily_report(date,totals,cumulative_net,per_wallet)`、`send_telegram(token,chat_id,text,proxy)`、`beijing_hour(ts)` 跨 T1/T2/T3 一致;`_maybe_push_daily` 用 `beijing_day`/`beijing_hour`/`_prev_day`/`send_telegram`/`format_daily_report` 均 T1 产出;settings 键名跨后端/前端一致。
