"""tests/test_weekly_push.py — 每周 Telegram 周报:持久化节流/台账门控/时辰/失败不阻断。

发送在后台守护线程(绝不阻塞下单),故调用后 join 线程再断言。目标 token/chat 写死常量。
"""

from unittest.mock import MagicMock, patch

from engine.manager import EngineManager
from config import TG_BOT_TOKEN, TG_CHAT_ID


def _mgr(ledger_empty=False):
    m = EngineManager.__new__(EngineManager)
    m.db = MagicMock()

    def _all(start, end):
        if ledger_empty:
            return []  # 台账没爬好 -> 门控应拦下
        if start == "2026-07-10":  # 最近7天窗口(昨天07-16往前7天)
            return [
                {
                    "date": "2026-07-14",
                    "reward": 7,
                    "rebate": 0,
                    "sell_profit": 0,
                    "loss": 0,
                    "fee": 0,
                    "net": 7,
                },
                {
                    "date": "2026-07-15",
                    "reward": 3,
                    "rebate": 0,
                    "sell_profit": 0,
                    "loss": 0,
                    "fee": 0,
                    "net": 3,
                },
            ]
        # 累计 / 门控(PNL_START_DATE..week_end)
        return [
            {
                "date": "2026-07-01",
                "reward": 0,
                "rebate": 0,
                "sell_profit": 0,
                "loss": 0,
                "fee": 0,
                "net": 100,
            }
        ]

    m.db.get_daily_pnl_all.side_effect = _all
    m.db.get_daily_pnl.return_value = [
        {
            "date": "2026-07-14",
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
    # 持久化节流:用 dict 模拟 DB
    state = {"week": None}
    m.db.get_last_push_week.side_effect = lambda: state["week"]
    m.db.set_last_push_week.side_effect = lambda w: state.__setitem__("week", w)
    m._scanner_api = MagicMock(proxy_url=None)
    m._pushing = False
    m._push_thread = None
    return m


def _join(m):
    if getattr(m, "_push_thread", None):
        m._push_thread.join(timeout=5)


def test_no_push_before_hour():
    m = _mgr()
    with patch("engine.manager.send_telegram") as st, patch(
        "engine.manager.beijing_hour", return_value=8
    ), patch("engine.manager.beijing_day", return_value="2026-07-17"):
        m._maybe_push_weekly()
    st.assert_not_called()


def test_no_push_when_ledger_empty():
    # 台账还没爬好(daily_pnl 空)-> 先不发,避免发 0(用户实报的核心问题)
    m = _mgr(ledger_empty=True)
    with patch("engine.manager.send_telegram") as st, patch(
        "engine.manager.beijing_hour", return_value=10
    ), patch("engine.manager.beijing_day", return_value="2026-07-17"):
        m._maybe_push_weekly()
        _join(m)
    st.assert_not_called()


def test_push_once_per_week_recent7_and_persists():
    m = _mgr()
    with patch("engine.manager.send_telegram") as st, patch(
        "engine.manager.beijing_hour", return_value=10
    ), patch(
        "engine.manager.beijing_day", return_value="2026-07-17"
    ):  # 周五
        m._maybe_push_weekly()
        _join(m)
        m._maybe_push_weekly()  # 同周第二次 -> 持久化节流拦下
        _join(m)
    assert st.call_count == 1
    args = st.call_args.args
    assert args[0] == TG_BOT_TOKEN and args[1] == TG_CHAT_ID
    assert "2026-07-10 ~ 2026-07-16" in args[2]  # 最近7整天(截止昨天07-16)
    m.db.set_last_push_week.assert_called_once_with("2026-07-13")  # 持久化本周周一


def test_send_failure_no_persist_and_retries():
    m = _mgr()
    with patch("engine.manager.send_telegram", side_effect=RuntimeError("net")), patch(
        "engine.manager.beijing_hour", return_value=10
    ), patch("engine.manager.beijing_day", return_value="2026-07-17"):
        m._maybe_push_weekly()
        _join(m)
    m.db.set_last_push_week.assert_not_called()  # 失败不持久化 -> 下轮重试
    assert m._pushing is False
