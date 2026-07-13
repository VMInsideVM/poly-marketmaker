"""tests/test_tiers.py — 档位模块解析/校验纯函数(不触网)。"""

from engine.tiers import enabled_sizes, tier_for, validate_size_tiers


def _tier(size, **over):
    t = {
        "size": size,
        "enabled": True,
        "shares": size if isinstance(size, int) else size,
        "rule1_min_coeff": 0,
        "rule2_min_coeff": 0,
        "rule3_min_coeff": 0,
        "gap_high_coeff_sum_min": 20,
        "amount_value_table": [{"upper": 0.31, "value": 1}],
    }
    t.update(over)
    return t


class TestEnabledSizes:
    def test_collects_enabled_only(self):
        assert enabled_sizes([_tier(20), _tier(50, enabled=False)]) == {20}

    def test_empty_and_none(self):
        assert enabled_sizes([]) == set()
        assert enabled_sizes(None) == set()

    def test_bad_entries_skipped(self):
        assert enabled_sizes([{"enabled": True, "size": "abc"}, _tier(20)]) == {20}


class TestTierFor:
    def test_exact_match_returns_module(self):
        t20 = _tier(20)
        assert tier_for([t20, _tier(50)], 20) is t20

    def test_no_match_returns_none(self):
        assert tier_for([_tier(20)], 30) is None

    def test_disabled_not_matched(self):
        assert tier_for([_tier(20, enabled=False)], 20) is None

    def test_bad_min_size_returns_none(self):
        assert tier_for([_tier(20)], None) is None
        assert tier_for(None, 20) is None


class TestValidateSizeTiers:
    def test_ok_normalizes_types(self):
        tiers, err = validate_size_tiers([_tier(20, shares="40")])
        assert err is None
        assert tiers[0]["size"] == 20 and tiers[0]["shares"] == 40
        assert tiers[0]["enabled"] is True

    def test_not_a_list(self):
        tiers, err = validate_size_tiers({"size": 20})
        assert tiers is None and err

    def test_shares_below_size_rejected(self):
        tiers, err = validate_size_tiers([_tier(20, shares=10)])
        assert tiers is None and "挂单份数" in err

    def test_duplicate_size_rejected(self):
        tiers, err = validate_size_tiers([_tier(20), _tier(20)])
        assert tiers is None and "重复" in err

    def test_non_integer_size_rejected(self):
        tiers, err = validate_size_tiers([_tier("abc", shares=20)])
        assert tiers is None and err

    def test_negative_threshold_rejected(self):
        tiers, err = validate_size_tiers([_tier(20, rule1_min_coeff=-1)])
        assert tiers is None and err


def test_template_defaults_has_size_tiers():
    from config import TEMPLATE_DEFAULTS

    assert TEMPLATE_DEFAULTS["size_tiers"] == []
