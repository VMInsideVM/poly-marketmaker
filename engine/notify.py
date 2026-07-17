"""engine/notify.py — 每周盈亏周报推送(Telegram)。format 纯函数;send 薄封装。

纯外发,不改任何交易逻辑。sendMessage 走 GET(复用 http_get + 钱包代理)。
"""

import requests

from api.proxy import use_proxy, http_get


def format_weekly_report(
    week_start,
    week_end,
    daily_nets,
    week_totals,
    cumulative_net,
    per_wallet,
    since_date,
) -> str:
    """组装「上一周」周报文本:每日净利润(7 天)+ 本周汇总(6 类别)+ 累计净利润 + 各钱包本周净。

    daily_nets=[(date, net), …];week_totals=全钱包本周 6 类别 dict;per_wallet=[{label, net}];
    since_date=累计起点(供「(自 X)」显示)。loss/fee 以负号展示;数值 2 位小数带符号。
    """

    def s(v):
        return f"{float(v or 0):+.2f}"

    lines = [f"📊 做市周报 · {week_start} ~ {week_end}", "", "【每日净利润】"]
    for date, net in daily_nets:
        lines.append(f"{date[5:]}  {s(net)}")  # MM-DD
    lines += [
        "",
        "【本周汇总】",
        f"做市奖励 {s(week_totals.get('reward', 0))} · 返佣 {s(week_totals.get('rebate', 0))}"
        f" · 卖出盈利 {s(week_totals.get('sell_profit', 0))}",
        f"亏损 {s(-float(week_totals.get('loss', 0) or 0))} · 手续费 "
        f"{s(-float(week_totals.get('fee', 0) or 0))} · 净利润 {s(week_totals.get('net', 0))}",
        "",
        f"【累计净利润】(自 {since_date})  {s(cumulative_net)}",
    ]
    if per_wallet:
        lines += ["", "【各钱包本周净利润】"]
        for w in per_wallet:
            lines.append(f"{w['label']}  {s(w['net'])}")
    return "\n".join(lines)


def send_telegram(token, chat_id, text, proxy=None) -> None:
    """发一条 Telegram 消息(sendMessage 走 GET,复用 http_get + 代理)。失败抛。

    ⚠️ 异常消息**绝不能带请求 URL**——URL 里 `bot<token>` 含机器人 token,requests 的
    HTTPError/连接错误默认把 URL 塞进消息,会经调用方 logger.warning 泄进日志文件。故这里
    捕获 requests 异常、只保留状态码/Telegram 描述,重抛消毒后的消息。
    """
    try:
        with use_proxy(proxy):
            resp = http_get(
                f"https://api.telegram.org/bot{token}/sendMessage",
                params={"chat_id": chat_id, "text": text},
                timeout=15,
            )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        code = getattr(getattr(e, "response", None), "status_code", "?")
        raise RuntimeError(f"Telegram 请求失败(HTTP {code})") from None
    if not data.get("ok"):
        raise RuntimeError(f"Telegram 返回失败: {data.get('description') or data}")
