"""tests/test_notify.py — Telegram 周报:format 纯函数 + send 薄封装。"""

from unittest.mock import patch, MagicMock

from engine.notify import format_weekly_report, send_telegram


def test_format_weekly_report_basic():
    daily_nets = [("2026-07-06", 2.0), ("2026-07-07", -1.0), ("2026-07-12", 3.0)]
    week_totals = {
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
    txt = format_weekly_report(
        "2026-07-06",
        "2026-07-12",
        daily_nets,
        week_totals,
        123.45,
        per_wallet,
        "2026-07-01",
    )
    assert "做市周报 · 2026-07-06 ~ 2026-07-12" in txt
    assert "07-06  +2.00" in txt and "07-07  -1.00" in txt
    assert "净利润 +8.61" in txt
    assert "亏损 -1.00" in txt and "手续费 -0.10" in txt  # loss/fee 显示为负
    assert "累计净利润" in txt and "自 2026-07-01" in txt and "+123.45" in txt
    assert "主号  +5.00" in txt and "0x1234...abcd  +3.61" in txt


def test_format_weekly_report_empty_wallets():
    zero = {k: 0 for k in ("reward", "rebate", "sell_profit", "loss", "fee", "net")}
    txt = format_weekly_report(
        "2026-07-06", "2026-07-12", [], zero, 0.0, [], "2026-07-01"
    )
    assert "净利润 +0.00" in txt
    assert "【各钱包本周净利润】" not in txt  # 无钱包明细则不加该段


def test_send_telegram_posts_and_checks_ok():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"ok": True}
    with patch("engine.notify.http_get", return_value=resp) as g:
        send_telegram("TOK", "CHAT", "hello", proxy=None)
    url = g.call_args.args[0]
    assert "botTOK/sendMessage" in url
    assert g.call_args.kwargs["params"] == {"chat_id": "CHAT", "text": "hello"}


def test_send_telegram_http_error_does_not_leak_token():
    # HTTPError 默认消息含 .../bot<token>/... URL;重抛必须消毒,绝不带 token。
    import requests

    resp = MagicMock()
    err = requests.exceptions.HTTPError(
        "401 Client Error: Unauthorized for url: "
        "https://api.telegram.org/botSECRETTOKEN/sendMessage"
    )
    err.response = MagicMock(status_code=401)
    resp.raise_for_status.side_effect = err
    with patch("engine.notify.http_get", return_value=resp):
        try:
            send_telegram("SECRETTOKEN", "CHAT", "hi")
            assert False, "应抛"
        except Exception as e:
            assert "SECRETTOKEN" not in str(e)  # token 不得进异常消息
            assert "401" in str(e)


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
