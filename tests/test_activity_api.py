"""Data API v2 流水：游标、金额单位、原有奖励台账口径。"""

from unittest.mock import patch

import pytest

from engine.pnl import reward_rebate_by_day
from tests.test_data_api_v2 import api, response


def test_get_activity_maps_rewards_and_rebates_without_changing_accounting():
    rows = [
        {
            "type": "REWARD",
            "usdc_size": 12.5,
            "timestamp": 1793667600,
            "token_id": "",
            "condition_id": "",
            "size": 999,
        },
        {"type": "MAKER_REBATE", "usdc_size": 0.75, "timestamp": 1793667600},
        {"type": "REDEEM", "usdc_size": 100, "timestamp": 1793667600},
    ]  # 2026-11-03 09:00 北京时间：奖励归前一天，返佣归当天，赎回不算收益。
    with patch("api.polymarket_api.http_get", return_value=response(rows)) as get:
        out = api().get_activity()
    assert [a["usdcSize"] for a in out] == [12.5, 0.75, 100]
    assert reward_rebate_by_day(out) == {
        "2026-11-02": {"reward": 12.5},
        "2026-11-03": {"rebate": 0.75},
    }
    assert get.call_args.args[0] == "https://data-api.polymarket.com/v2/activity"
    assert get.call_args.kwargs["params"]["start"] == 1


def test_get_activity_keeps_filters_on_all_cursor_pages_even_when_short():
    rows = [{"type": "REWARD", "usdc_size": 1.0, "timestamp": 150}]
    with patch(
        "api.polymarket_api.http_get",
        side_effect=[response(rows, "opaque"), response(rows)],
    ) as get:
        out = api().get_activity(types=["REWARD", "MAKER_REBATE"], start=100, end=200)
    assert len(out) == 2
    for call in get.call_args_list:
        p = call.kwargs["params"]
        assert p["user"] == "0xFUND" and p["type"] == "REWARD,MAKER_REBATE"
        assert p["start"] == 100 and p["end"] == 200
        assert "offset" not in p
    assert "cursor" not in get.call_args_list[0].kwargs["params"]
    assert get.call_args_list[1].kwargs["params"]["cursor"] == "opaque"


def test_full_final_page_stops_at_null_cursor_and_preserves_zero_amount():
    row = {"type": "REWARD", "usdc_size": 0, "timestamp": 0}
    with patch(
        "api.polymarket_api.http_get", return_value=response([row] * 500)
    ) as get:
        out = api().get_activity(start=0)
    assert len(out) == 500 and out[0]["usdcSize"] == 0
    assert get.call_count == 1
    assert get.call_args.kwargs["params"]["start"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", None),
        ("usdc_size", None),
        ("usdc_size", "bad"),
        ("usdc_size", float("nan")),
        ("timestamp", None),
        ("timestamp", -1),
    ],
)
def test_invalid_activity_fails_instead_of_overwriting_ledger_with_zero(field, value):
    row = {"type": "REWARD", "usdc_size": 1, "timestamp": 1}
    row[field] = value
    with patch("api.polymarket_api.http_get", return_value=response([row])):
        with pytest.raises(ValueError, match="Data API"):
            api().get_activity()
