"""tests/test_notify.py — Telegram 日报:format 纯函数 + send 薄封装。"""

from unittest.mock import patch, MagicMock

from engine.notify import format_daily_report, send_telegram


def test_format_daily_report_basic():
    totals = {
        "reward": 7.21,
        "rebate": 0.5,
        "sell_profit": 2.0,
        "loss": 1.0,
        "fee": 0.1,
        "net": 8.61,
    }
    per_wallet = [
        {"label": "主号", "net": 5.0},
        {"label": "0x1234...abcd", "net": 3.61},
    ]
    txt = format_daily_report("2026-07-15", totals, 123.45, per_wallet)
    assert "做市日报 · 2026-07-15" in txt
    assert "净利润    +8.61" in txt
    assert "亏损      -1.00" in txt  # loss 显示为负
    assert "手续费    -0.10" in txt
    assert "累计净利润 +123.45" in txt
    assert "主号  +5.00" in txt and "0x1234...abcd  +3.61" in txt


def test_format_daily_report_empty_totals():
    zero = {k: 0 for k in ("reward", "rebate", "sell_profit", "loss", "fee", "net")}
    txt = format_daily_report("2026-07-15", zero, 0.0, [])
    assert "净利润    +0.00" in txt
    assert "【各钱包净利润】" not in txt  # 无钱包明细则不加该段


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
