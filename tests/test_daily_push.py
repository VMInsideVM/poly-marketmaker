"""tests/test_daily_push.py — 每日 Telegram 日报:目标写死常量、始终开启、节流/时辰/失败不阻断。

发送在后台守护线程(绝不阻塞下单循环),故调用后 join 线程再断言。
"""

from unittest.mock import MagicMock, patch

from engine.manager import EngineManager
from config import TG_BOT_TOKEN, TG_CHAT_ID


def _mgr():
    m = EngineManager.__new__(EngineManager)
    m.db = MagicMock()
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
    ), patch("engine.manager.beijing_day", return_value="2026-07-16"):
        m._maybe_push_daily()
    st.assert_not_called()


def test_push_once_per_day_after_hour_uses_hardcoded_target():
    m = _mgr()
    with patch("engine.manager.send_telegram") as st, patch(
        "engine.manager.beijing_hour", return_value=10
    ), patch("engine.manager.beijing_day", return_value="2026-07-16"):
        m._maybe_push_daily()
        _join(m)  # 发送在后台线程
        m._maybe_push_daily()  # 同日第二次 -> 节流(_last_push_date 已置)
        _join(m)
    assert st.call_count == 1
    args = st.call_args.args
    # 目标写死常量(非配置页),报的是昨天
    assert args[0] == TG_BOT_TOKEN and args[1] == TG_CHAT_ID and "2026-07-15" in args[2]


def test_send_failure_no_raise_and_retries():
    m = _mgr()
    with patch("engine.manager.send_telegram", side_effect=RuntimeError("net")), patch(
        "engine.manager.beijing_hour", return_value=10
    ), patch("engine.manager.beijing_day", return_value="2026-07-16"):
        m._maybe_push_daily()  # 不抛
        _join(m)
    assert m._last_push_date is None  # 失败未置日期 -> 下轮重试
    assert m._pushing is False  # 重入标志已复位
