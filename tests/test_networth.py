"""tests/test_networth.py — 账户净值:纯函数 + DB 读写(不触网)。"""

from engine.networth import should_snapshot, positions_value


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
