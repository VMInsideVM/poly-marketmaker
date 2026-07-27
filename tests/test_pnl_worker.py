"""tests/test_pnl_worker.py — worker 台账补漏:后台线程全量补(6/1起)、跨天重算、失败不阻断。

_maybe_rebuild_pnl 在后台守护线程跑(绝不阻塞 _tick),故测试里调用后 join 线程再断言。
"""

from unittest.mock import MagicMock, patch

from engine.manager import WalletWorker


def _worker():
    api, db = MagicMock(), MagicMock()
    api.get_funder.return_value = "0xF"
    return WalletWorker(api, db, "0xW", {"fill_check_interval_sec": 5}), api, db


def _join(w):
    if w._pnl_thread:
        w._pnl_thread.join(timeout=5)


def test_first_call_backfills_from_start_date():
    w, api, db = _worker()
    with patch("engine.manager.rebuild_wallet_pnl") as rb, patch(
        "engine.manager.beijing_day", return_value="2026-07-16"
    ):
        w._maybe_rebuild_pnl()
        _join(w)
    rb.assert_called_once_with(api, db, "0xW", "2026-05-17", "2026-07-16")


def test_same_day_second_call_skips():
    w, api, db = _worker()
    with patch("engine.manager.rebuild_wallet_pnl") as rb, patch(
        "engine.manager.beijing_day", return_value="2026-07-16"
    ):
        w._maybe_rebuild_pnl()
        _join(w)  # 首次成功 -> _last_pnl_date=今天
        w._maybe_rebuild_pnl()  # 同日 -> 节流,不再 spawn
        _join(w)
    assert rb.call_count == 1


def test_day_crossing_reruns():
    w, api, db = _worker()
    with patch("engine.manager.rebuild_wallet_pnl") as rb:
        with patch("engine.manager.beijing_day", return_value="2026-07-16"):
            w._maybe_rebuild_pnl()
            _join(w)
        with patch("engine.manager.beijing_day", return_value="2026-07-17"):
            w._maybe_rebuild_pnl()
            _join(w)
    assert rb.call_count == 2


def test_failure_no_raise_and_retries():
    w, api, db = _worker()
    with patch("engine.manager.beijing_day", return_value="2026-07-16"):
        with patch("engine.manager.rebuild_wallet_pnl", side_effect=RuntimeError("x")):
            w._maybe_rebuild_pnl()  # 不抛
            _join(w)
        assert w._last_pnl_date is None  # 失败未置日期,下拍重试
        assert w._pnl_rebuilding is False  # 重入标志已复位
        with patch("engine.manager.rebuild_wallet_pnl") as rb:
            w._maybe_rebuild_pnl()
            _join(w)
            rb.assert_called_once()
