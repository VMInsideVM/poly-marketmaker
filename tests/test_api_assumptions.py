"""接口自检自身的回归测试；HTTP 全部替身，不访问钱包或真实网络。"""

from copy import deepcopy
from unittest.mock import Mock

import pytest
import requests

import check_api_assumptions as checks
from api.proxy import current_proxy


# 按官方 Data API v2 OpenAPI 的 Position / Activity 原始字段构造，绝不使用适配器输出。
# https://data-api.polymarket.com/v2/openapi.json
PUBLIC_EXAMPLE_USER = "0x983eedfbd75803602e4a6e6ea9aab6dc6b9c6748"
POSITION = {
    "proxy_wallet": PUBLIC_EXAMPLE_USER,
    "token_id": "12345",
    "condition_id": "0x" + "ab" * 32,
    "current_size": 10.0,
    "avg_price": 0.4,
    "current_price": 0.5,
    "current_value": 5.0,
    "entry_cost_usdc": 4.0,
    "entry_fees_usdc": 0.0,
    "total_cost_usdc": 4.0,
    "total_size": 25.0,
    "realized_pnl": 0.0,
    "unrealized_pnl": 1.0,
    "total_pnl": 1.0,
    "percent_pnl": 25.0,
    "percent_realized_pnl": 0.0,
    "status": "OPEN",
    "redeemable": False,
    "mergeable": False,
    "negative_risk": False,
    "archived": False,
    "title": "Synthetic test market",
    "slug": "synthetic-test-market",
    "icon": "",
    "event_id": "123",
    "event_slug": "synthetic-test-event",
    "outcome": "Yes",
    "outcome_index": 0,
    "opposite_outcome": "No",
    "opposite_token_id": "54321",
    "end_date": "2026-12-01",
    "last_event_at": 1793664000,
    "first_entry_at": 1793663000,
    "name": "",
    "profile_image": "",
    "verified": False,
}
ACTIVITY = {
    "proxy_wallet": PUBLIC_EXAMPLE_USER,
    "timestamp": 1793664000,
    "condition_id": "0x" + "ab" * 32,
    "type": "TRADE",
    "size": 10.0,
    "usdc_size": 4.0,
    "transaction_hash": "0x" + "cd" * 32,
    "price": 0.4,
    "token_id": "12345",
    "side": "BUY",
    "outcome_index": 0,
    "title": "Synthetic test market",
    "slug": "synthetic-test-market",
    "icon": "",
    "event_slug": "synthetic-test-event",
    "outcome": "Yes",
    "name": "",
    "pseudonym": "",
    "bio": "",
    "profile_image": "",
    "profile_image_optimized": "",
}


def page(rows, cursor=None):
    return {
        "data": deepcopy(rows),
        "pagination": {
            "limit": 1,
            "offset": 0,
            "has_more": cursor is not None,
            "next_cursor": cursor,
        },
    }


@pytest.fixture(autouse=True)
def clear_results(monkeypatch):
    monkeypatch.setattr(checks, "results", [])
    # 自检只允许匿名 GET，不能通过构造带私钥的 API 或发订单绕过检查。
    monkeypatch.setattr(
        checks.PolymarketAPI, "__init__", Mock(side_effect=AssertionError("no client"))
    )
    monkeypatch.setattr(
        checks, "http_post", Mock(side_effect=AssertionError("no POST"))
    )


def install_response(monkeypatch, payload, status=200):
    response = Mock()
    response.status_code = status
    response.json.return_value = payload
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(f"{status} Gone")
    get = Mock(return_value=response)
    # raising=False 让先写的测试在尚未引入 http_get 时也能观察 main 缺少新检查。
    monkeypatch.setattr(checks, "http_get", get, raising=False)
    return get


@pytest.mark.parametrize(
    "endpoint,row", [("positions", POSITION), ("activity", ACTIVITY)]
)
def test_raw_v2_contract_passes_and_uses_only_public_read(monkeypatch, endpoint, row):
    get = install_response(monkeypatch, page([row], "opaque-next"))

    getattr(checks, f"check_data_api_{endpoint}")()

    assert [r[0] for r in checks.results] == ["PASS"]
    args, kwargs = get.call_args
    assert args == (f"https://data-api.polymarket.com/v2/{endpoint}",)
    assert kwargs["params"] == {"user": PUBLIC_EXAMPLE_USER, "limit": 1}
    assert get.call_count == 1  # 抽样契约检查不遍历公开钱包的完整历史。


@pytest.mark.parametrize("endpoint", ["positions", "activity"])
def test_empty_page_warns_instead_of_claiming_row_contract_passed(
    monkeypatch, endpoint
):
    install_response(monkeypatch, page([]))

    getattr(checks, f"check_data_api_{endpoint}")()

    assert [r[0] for r in checks.results] == ["WARN"]
    assert "字段" in checks.results[0][2]


@pytest.mark.parametrize("endpoint", ["positions", "activity"])
def test_retired_endpoint_http_error_is_fail(monkeypatch, endpoint):
    install_response(monkeypatch, {"error": "Data API v1 retired"}, status=410)

    getattr(checks, f"check_data_api_{endpoint}")()

    assert checks.results[0][0] == "FAIL"
    assert "410" in checks.results[0][2]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"data": None, "pagination": {"next_cursor": None}},
        {"data": []},
        {"data": [], "pagination": {}},
        {"data": [], "pagination": {"next_cursor": 123, "has_more": True}},
        {"data": [], "pagination": {"next_cursor": None, "has_more": True}},
    ],
)
@pytest.mark.parametrize("endpoint", ["positions", "activity"])
def test_invalid_envelope_is_fail_even_with_empty_data(monkeypatch, endpoint, payload):
    install_response(monkeypatch, payload)

    getattr(checks, f"check_data_api_{endpoint}")()

    assert checks.results[0][0] == "FAIL"


@pytest.mark.parametrize(
    "endpoint,row,field",
    [
        ("positions", POSITION, "token_id"),
        ("positions", POSITION, "condition_id"),
        ("positions", POSITION, "current_size"),
        ("positions", POSITION, "current_price"),
        ("positions", POSITION, "avg_price"),
        ("activity", ACTIVITY, "condition_id"),
        ("activity", ACTIVITY, "token_id"),
        ("activity", ACTIVITY, "usdc_size"),
        ("activity", ACTIVITY, "transaction_hash"),
        ("activity", ACTIVITY, "timestamp"),
    ],
)
def test_missing_raw_field_fails_instead_of_silent_adapter_default(
    monkeypatch, endpoint, row, field
):
    broken = deepcopy(row)
    del broken[field]
    install_response(monkeypatch, page([broken]))

    getattr(checks, f"check_data_api_{endpoint}")()

    assert checks.results[0][0] == "FAIL"
    assert field in checks.results[0][2]


@pytest.mark.parametrize("value", [None, "10", True, float("nan"), float("inf")])
def test_positions_current_size_must_be_finite_json_number(monkeypatch, value):
    broken = dict(POSITION, current_size=value)
    install_response(monkeypatch, page([broken]))

    checks.check_data_api_positions()

    assert checks.results[0][0] == "FAIL"
    assert "current_size" in checks.results[0][2]


def test_non_trade_activity_can_have_empty_market_and_token_fields(monkeypatch):
    reward = dict(
        ACTIVITY, type="REWARD", condition_id="", token_id="", side="", size=0, price=0
    )
    install_response(monkeypatch, page([reward]))

    checks.check_data_api_activity()

    assert checks.results[0][0] == "PASS"


def test_main_runs_data_checks_under_existing_proxy_and_fails_release(monkeypatch):
    # 旧自检照常通过，只有新增的原始 Data API 410 能阻止发布。
    monkeypatch.setattr(
        checks.sys,
        "argv",
        ["check_api_assumptions.py", "--proxy", "http://127.0.0.1:7890"],
    )
    monkeypatch.setattr(
        checks.PolymarketAPI, "get_rewards_markets", Mock(return_value=[{}] * 50)
    )
    for name in (
        "check_category_tags",
        "check_gamma_tags_match_categories",
        "check_resolution_field",
        "check_gamma_batch",
        "check_books_by_asset",
        "check_precise_reward",
        "report_event_level_drift",
    ):
        monkeypatch.setattr(checks, name, Mock())
    seen = []

    def get(url, **kwargs):
        seen.append((url, current_proxy.get()))
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError("410 Gone")
        return response

    monkeypatch.setattr(checks, "http_get", get, raising=False)

    assert checks.main() == 1
    assert seen == [
        ("https://data-api.polymarket.com/v2/positions", "http://127.0.0.1:7890"),
        ("https://data-api.polymarket.com/v2/activity", "http://127.0.0.1:7890"),
    ]
    assert len([r for r in checks.results if r[0] == "FAIL"]) == 2
