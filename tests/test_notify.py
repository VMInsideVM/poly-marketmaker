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


def test_label_passed_through_verbatim():
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
