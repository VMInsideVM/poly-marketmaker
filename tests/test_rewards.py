"""tests/test_rewards.py"""

from engine.rewards import extract_max_spread, extract_daily_rate


def test_top_level_rewards_max_spread():
    items = [
        {
            "condition_id": "0x1",
            "rewards_max_spread": 99,
            "rewards_config": [{"rate_per_day": 0.25}],
        }
    ]
    assert extract_max_spread(items) == 99


def test_string_and_float_values_parsed_as_float():
    assert extract_max_spread([{"rewards_max_spread": "3"}]) == 3.0
    assert extract_max_spread([{"rewards_max_spread": 3.0}]) == 3.0


def test_fractional_cents_preserved_not_truncated():
    # Live 0.1-cent markets report e.g. 4.5 / 3.5 cents; truncating to 4/3
    # narrows the reward band, so the fraction must survive.
    assert extract_max_spread([{"rewards_max_spread": 4.5}]) == 4.5
    assert extract_max_spread([{"rewards_max_spread": "3.5"}]) == 3.5


def test_empty_list_returns_none():
    assert extract_max_spread([]) is None
    assert extract_max_spread(None) is None


def test_item_without_field_returns_none():
    assert extract_max_spread([{"condition_id": "0x1"}]) is None


def test_first_item_missing_second_has_value():
    items = [{"condition_id": "0x1"}, {"rewards_max_spread": 4}]
    assert extract_max_spread(items) == 4


def test_unparseable_value_skipped():
    assert extract_max_spread([{"rewards_max_spread": "abc"}]) is None


def test_daily_rate_sums_multiple_configs():
    items = [{"rewards_config": [{"rate_per_day": 10}, {"rate_per_day": 5}]}]
    assert extract_daily_rate(items) == 15.0


def test_daily_rate_sums_across_data_items():
    items = [
        {"rewards_config": [{"rate_per_day": 10}]},
        {"rewards_config": [{"rate_per_day": 5}]},
    ]
    assert extract_daily_rate(items) == 15.0


def test_daily_rate_zero_is_zero_not_none():
    # 0 是「奖励真归零」,调用方要据此撤单;绝不能和「取不到」(None)混为一谈。
    result = extract_daily_rate([{"rewards_config": [{"rate_per_day": 0}]}])
    assert result == 0.0
    assert result is not None


def test_daily_rate_string_values_parsed():
    assert extract_daily_rate([{"rewards_config": [{"rate_per_day": "2.5"}]}]) == 2.5


def test_daily_rate_empty_returns_none():
    assert extract_daily_rate([]) is None
    assert extract_daily_rate(None) is None


def test_daily_rate_no_rewards_config_returns_none():
    assert (
        extract_daily_rate([{"condition_id": "0x1", "rewards_max_spread": 3}]) is None
    )


def test_daily_rate_unparseable_skipped_but_rest_summed():
    items = [{"rewards_config": [{"rate_per_day": "abc"}, {"rate_per_day": 3}]}]
    assert extract_daily_rate(items) == 3.0


def test_daily_rate_all_unparseable_returns_none():
    assert extract_daily_rate([{"rewards_config": [{"rate_per_day": "abc"}]}]) is None
