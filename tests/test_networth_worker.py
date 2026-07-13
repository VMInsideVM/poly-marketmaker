"""tests/test_networth_worker.py — worker 净值快照:首 tick 必记、跨天补记、失败不阻断。"""

from unittest.mock import MagicMock

from engine.manager import WalletWorker


def _worker():
    api, db = MagicMock(), MagicMock()
    api.get_balance.return_value = 100.0
    api.get_funder.return_value = "0xF"
    api.get_user_positions.return_value = [{"size": 10, "curPrice": 0.5}]
    w = WalletWorker(api, db, "0xW", {"fill_check_interval_sec": 5})
    return w, api, db


def test_first_call_records_cash_plus_positions():
    w, api, db = _worker()
    w._maybe_snapshot_networth()
    db.record_net_worth.assert_called_once_with("0xW", 100.0, 5.0)


def test_same_day_second_call_skips():
    w, api, db = _worker()
    w._maybe_snapshot_networth()
    w._maybe_snapshot_networth()
    assert db.record_net_worth.call_count == 1


def test_failure_no_raise_and_retries_next_tick():
    w, api, db = _worker()
    api.get_balance.side_effect = RuntimeError("proxy down")
    w._maybe_snapshot_networth()  # 不抛
    db.record_net_worth.assert_not_called()
    api.get_balance.side_effect = None  # 网络恢复 -> 下一拍补记
    w._maybe_snapshot_networth()
    db.record_net_worth.assert_called_once()


def test_tick_invokes_snapshot():
    w, api, db = _worker()
    w.monitor = MagicMock()  # 只验证 _tick 链路,监控步骤全 mock
    w._tick()
    db.record_net_worth.assert_called_once()
