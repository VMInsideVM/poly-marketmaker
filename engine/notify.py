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
