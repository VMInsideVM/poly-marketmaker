"""tests/test_daily_push.py — 每日 Telegram 日报:节流/开关/时辰/失败不阻断。"""

from unittest.mock import MagicMock, patch

from engine.manager import EngineManager


def _mgr(settings):
    m = EngineManager.__new__(EngineManager)
    m.db = MagicMock()
    m.db.get_settings.return_value = settings
    m.db.get_daily_pnl_all.return_value = [
        {
            "date": "2026-07-15",
            "reward": 7,
            "rebate": 0,
            "sell_profit": 0,
            "loss": 0,
            "fee": 0,
            "net": 7,
        }
    ]
    m.db.get_daily_pnl.return_value = [
        {
            "date": "2026-07-15",
            "reward": 7,
            "rebate": 0,
            "sell_profit": 0,
            "loss": 0,
            "fee": 0,
            "net": 7,
        }
    ]
    m.db.list_wallets.return_value = [
        {"address": "0xAAAA1111bbbbCCCC", "remark": "主号"}
    ]
    m._scanner_api = MagicMock(proxy_url=None)
    m._last_push_date = None
    return m


S_ON = {"push_enabled": True, "tg_bot_token": "T", "tg_chat_id": "C", "push_hour": 9}


def test_no_push_when_disabled():
    m = _mgr({**S_ON, "push_enabled": False})
    with patch("engine.manager.send_telegram") as st, patch(
        "engine.manager.beijing_hour", return_value=10
    ):
        m._maybe_push_daily()
    st.assert_not_called()


def test_no_push_before_hour():
    m = _mgr(S_ON)
    with patch("engine.manager.send_telegram") as st, patch(
        "engine.manager.beijing_hour", return_value=8
    ), patch("engine.manager.beijing_day", return_value="2026-07-16"):
        m._maybe_push_daily()
    st.assert_not_called()


def test_push_once_per_day_after_hour():
    m = _mgr(S_ON)
    with patch("engine.manager.send_telegram") as st, patch(
        "engine.manager.beijing_hour", return_value=10
    ), patch("engine.manager.beijing_day", return_value="2026-07-16"):
        m._maybe_push_daily()
        m._maybe_push_daily()  # 同日第二次 -> 节流
    assert st.call_count == 1
    args = st.call_args.args
    assert args[0] == "T" and args[1] == "C" and "2026-07-15" in args[2]


def test_send_failure_no_raise_and_retries():
    m = _mgr(S_ON)
    with patch("engine.manager.send_telegram", side_effect=RuntimeError("net")), patch(
        "engine.manager.beijing_hour", return_value=10
    ), patch("engine.manager.beijing_day", return_value="2026-07-16"):
        m._maybe_push_daily()  # 不抛
    assert m._last_push_date is None  # 失败未置日期 -> 下轮重试
