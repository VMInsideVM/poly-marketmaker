"""tests/test_networth.py — 账户净值:纯函数 + DB 读写(不触网)。"""

import time
from datetime import datetime

import pytest

from engine.networth import should_snapshot, positions_value
from models.database import Database


class TestShouldSnapshot:
    def test_first_time_none_records(self):
        assert should_snapshot(None, "2026-07-13") is True

    def test_same_day_skips(self):
        assert should_snapshot("2026-07-13", "2026-07-13") is False

    def test_new_day_records(self):
        assert should_snapshot("2026-07-13", "2026-07-14") is True


class TestPositionsValue:
    def test_current_value_preferred(self):
        assert (
            positions_value([{"size": 10, "curPrice": 0.5, "currentValue": 6.0}]) == 6.0
        )

    def test_fallback_size_times_cur_price(self):
        assert positions_value([{"size": 10, "curPrice": 0.5}]) == 5.0

    def test_sums_multiple_and_skips_nonpositive(self):
        ps = [
            {"size": 10, "curPrice": 0.5},  # 5.0
            {"size": "20", "curPrice": "0.25"},  # 5.0 字符串安全转换
            {"size": 0, "curPrice": 0.9},  # 忽略
            {"size": -3, "curPrice": 0.9},  # 忽略
        ]
        assert positions_value(ps) == 10.0

    def test_bad_values_skipped_not_raise(self):
        ps = [
            {"size": "abc"},
            {"size": 5, "curPrice": None},
            {"size": 5, "currentValue": "x", "curPrice": 0.2},
        ]
        # "abc" 跳过;curPrice None 按 0;currentValue 非法回退 size×curPrice=1.0
        assert positions_value(ps) == 1.0

    def test_empty_and_none(self):
        assert positions_value([]) == 0.0
        assert positions_value(None) == 0.0


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "networth.db"))
    d.init()
    yield d
    d.close()


def _insert(db, wallet, cash, pos, ts):
    """直插带指定 created_at 的行(测试回填历史用;total=cash+pos 与方法口径一致)。"""
    db.conn.execute(
        "INSERT INTO net_worth_history (wallet, cash, positions_value, total, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (wallet, cash, pos, cash + pos, ts),
    )
    db.conn.commit()


def _day(ts):
    """与 SQLite date(created_at,'unixepoch','localtime') 同口径的本地日期串。"""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


class TestNetWorthDB:
    # 固定时间戳(2023-11-15 前后),同天/异天关系与时区无关:
    # T0 与 T0+60 必同天,T0 与 T0+2 天必异天。
    T0 = 1_700_000_000

    def test_record_and_read_back(self, db):
        db.record_net_worth("0xW", 100.5, 20.5)
        rows = db.get_net_worth_daily("0xW", days=36500)
        assert len(rows) == 1
        r = rows[0]
        assert (
            r["cash"] == 100.5 and r["positions_value"] == 20.5 and r["total"] == 121.0
        )
        assert r["date"] == _day(time.time())

    def test_same_day_takes_last(self, db):
        _insert(db, "0xW", 100, 0, self.T0)
        _insert(db, "0xW", 200, 50, self.T0 + 60)  # 同一天更晚
        rows = db.get_net_worth_daily("0xW", days=36500)
        assert len(rows) == 1
        assert rows[0]["total"] == 250 and rows[0]["date"] == _day(self.T0 + 60)

    def test_multi_day_ascending(self, db):
        _insert(db, "0xW", 100, 0, self.T0)
        _insert(db, "0xW", 300, 0, self.T0 + 2 * 86400)
        rows = db.get_net_worth_daily("0xW", days=36500)
        assert [r["date"] for r in rows] == [_day(self.T0), _day(self.T0 + 2 * 86400)]

    def test_days_cutoff_and_wallet_isolation(self, db):
        _insert(db, "0xW", 100, 0, self.T0)  # 2023 年,远超 30 天窗口
        db.record_net_worth("0xW", 50, 0)
        db.record_net_worth("0xOTHER", 999, 0)
        rows = db.get_net_worth_daily("0xW", days=30)
        assert len(rows) == 1 and rows[0]["total"] == 50
