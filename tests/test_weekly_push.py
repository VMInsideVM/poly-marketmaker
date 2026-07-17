"""tests/test_weekly_push.py — 每周 Telegram 周报:节流/时辰/失败不阻断。

发送在后台守护线程(绝不阻塞下单),故调用后 join 线程再断言。目标 token/chat 写死常量。
"""

from unittest.mock import MagicMock, patch

from engine.manager import EngineManager
from config import TG_BOT_TOKEN, TG_CHAT_ID


def _mgr():
    m = EngineManager.__new__(EngineManager)
    m.db = MagicMock()

    def _all(start, end):
        if start == "2026-07-06":  # 上周区间的每日(跨钱包)聚合
            return [
                {
                    "date": "2026-07-06",
                    "reward": 7,
                    "rebate": 0,
                    "sell_profit": 0,
                    "loss": 0,
                    "fee": 0,
                    "net": 7,
                },
                {
                    "date": "2026-07-09",
                    "reward": 0,
                    "rebate": 0,
                    "sell_profit": 2,
                    "loss": 0,
                    "fee": 0,
                    "net": 2,
                },
            ]
        # 累计(PNL_START_DATE..week_end)
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
            "date": "2026-07-06",
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
    m._last_push_week = None
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
    ), patch("engine.manager.beijing_day", return_value="2026-07-15"):
        m._maybe_push_weekly()
    st.assert_not_called()


def test_push_once_per_week_reports_last_week():
    m = _mgr()
    with patch("engine.manager.send_telegram") as st, patch(
        "engine.manager.beijing_hour", return_value=10
    ), patch(
        "engine.manager.beijing_day", return_value="2026-07-15"
    ):  # 周三
        m._maybe_push_weekly()
        _join(m)
        m._maybe_push_weekly()  # 同周第二次 -> 节流
        _join(m)
    assert st.call_count == 1
    args = st.call_args.args
    assert args[0] == TG_BOT_TOKEN and args[1] == TG_CHAT_ID
    assert "2026-07-06 ~ 2026-07-12" in args[2]  # 报上一个完整周
    assert m._last_push_week == "2026-07-06"


def test_send_failure_no_raise_and_retries():
    m = _mgr()
    with patch("engine.manager.send_telegram", side_effect=RuntimeError("net")), patch(
        "engine.manager.beijing_hour", return_value=10
    ), patch("engine.manager.beijing_day", return_value="2026-07-15"):
        m._maybe_push_weekly()
        _join(m)
    assert m._last_push_week is None  # 失败未置 -> 下轮重试
    assert m._pushing is False
