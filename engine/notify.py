"""engine/notify.py — 每日盈亏日报推送(Telegram)。format 纯函数;send 薄封装。

纯外发,不改任何交易逻辑。sendMessage 走 GET(复用 http_get + 钱包代理)。
"""

import logging

from api.proxy import use_proxy, http_get

logger = logging.getLogger(__name__)


def format_daily_report(date, totals, cumulative_net, per_wallet) -> str:
    """组装「昨天」日报文本。totals=全钱包 6 类别 dict;per_wallet=[{label,net}]。

    loss/fee 以负号展示(它们减小净利);net 已含扣减。数值 2 位小数带符号。
    """

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
