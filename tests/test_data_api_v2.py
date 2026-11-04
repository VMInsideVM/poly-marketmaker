"""V2 数据边界：真实消费者仍读旧内部字段，不能误判空仓或截断分页。"""

from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest
from requests import HTTPError

from api.polymarket_api import PolymarketAPI
from engine.positions import held_side_info


def api():
    obj = PolymarketAPI.__new__(PolymarketAPI)  # 不构造签名客户端
    obj.proxy_url = None
    obj.get_funder = lambda: "0xFUND"
    return obj


def response(rows, cursor=None):
    r = MagicMock()
    r.json.return_value = {
        "data": rows,
        "pagination": {"next_cursor": cursor, "has_more": cursor is not None},
    }
    return r


def position(**changes):
    row = {
        "token_id": "123",
        "condition_id": "0xmarket",
        "current_size": 25.5,
        "total_size": 999,
        "current_price": 0.4,
        "avg_price": 0.3,
        "title": "测试市场",
        "outcome": "Yes",
        "redeemable": False,
    }
    row.update(changes)
    return row


def test_positions_maps_current_holdings_for_engine_and_display():
    raw = position()
    before = deepcopy(raw)
    with patch("api.polymarket_api.http_get", return_value=response([raw])) as get:
        out = api().get_user_positions("0xFUND")
    p = out[0]
    assert (p["asset"], p["conditionId"], p["size"]) == ("123", "0xmarket", 25.5)
    assert (p["curPrice"], p["avgPrice"], p["title"], p["outcome"]) == (
        0.4,
        0.3,
        "测试市场",
        "Yes",
    )
    assets, values, shares = held_side_info(out)
    assert assets == {"123"}
    assert values == pytest.approx({"0xmarket": 10.2})
    assert shares == {"0xmarket": 25.5}
    assert raw == before
    assert get.call_args.args[0] == "https://data-api.polymarket.com/v2/positions"
    assert get.call_args.kwargs["params"] == {
        "user": "0xFUND",
        "limit": 500,
        "status": "OPEN",
        "filter_type": "TOKENS",
        "filter_amount": 0,
    }


def test_positions_preserves_zero_prices_and_small_sizes():
    with patch(
        "api.polymarket_api.http_get",
        return_value=response(
            [position(current_size=0.01, current_price=0, avg_price=0, redeemable=True)]
        ),
    ):
        out = api().get_user_positions("0xFUND")
    assert out[0]["size"] == 0.01
    assert out[0]["curPrice"] == out[0]["avgPrice"] == 0
    assert out[0]["redeemable"] is True
    assert held_side_info(out)[0] == {"123"}


def test_positions_cursor_walk_includes_short_and_empty_pages():
    pages = [
        response([position()], "a"),
        response([], "b"),
        response([position(token_id="456")]),
    ]
    with patch("api.polymarket_api.http_get", side_effect=pages) as get:
        out = api().get_user_positions("0xFUND")
    assert [p["asset"] for p in out] == ["123", "456"]
    params = [c.kwargs["params"] for c in get.call_args_list]
    assert [p.get("cursor") for p in params] == [None, "a", "b"]
    assert all(p["user"] == "0xFUND" and "offset" not in p for p in params)
    assert all(p["filter_amount"] == 0 and p["status"] == "OPEN" for p in params)


def _call(obj, method):
    return (
        obj.get_user_positions("0xFUND")
        if method == "positions"
        else obj.get_activity()
    )


@pytest.mark.parametrize("method", ["positions", "activity"])
def test_v2_empty_final_page_is_valid(method):
    with patch("api.polymarket_api.http_get", return_value=response([])) as get:
        assert _call(api(), method) == []
    assert get.call_count == 1


@pytest.mark.parametrize("method", ["positions", "activity"])
@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {},
        {"data": None, "pagination": {"next_cursor": None}},
        {"data": {}, "pagination": {"next_cursor": None}},
        {"data": [], "pagination": {}},
        {"data": [], "pagination": None},
        {"data": [], "pagination": {"next_cursor": 1}},
        {"data": [], "pagination": {"next_cursor": ""}},
        {"data": [], "pagination": {"next_cursor": None, "has_more": True}},
        {"data": [None], "pagination": {"next_cursor": None}},
    ],
)
def test_malformed_response_raises_instead_of_claiming_empty_or_partial(
    method, payload
):
    r = response([])
    r.json.return_value = payload
    with patch("api.polymarket_api.http_get", return_value=r):
        with pytest.raises(ValueError, match="Data API"):
            _call(api(), method)


@pytest.mark.parametrize("method", ["positions", "activity"])
def test_later_http_failure_never_returns_partial_data(method):
    row = (
        position()
        if method == "positions"
        else {"type": "REWARD", "timestamp": 1, "usdc_size": 2}
    )
    r = response([])
    r.raise_for_status.side_effect = HTTPError("503 unavailable")
    with patch("api.polymarket_api.http_get", side_effect=[response([row], "next"), r]):
        with pytest.raises(HTTPError):
            _call(api(), method)


@pytest.mark.parametrize("method", ["positions", "activity"])
def test_cursor_cycle_raises_instead_of_looping_or_returning_partial(method):
    with patch(
        "api.polymarket_api.http_get",
        side_effect=[response([], "a"), response([], "b"), response([], "a")],
    ):
        with pytest.raises(ValueError, match="Data API.*cursor"):
            _call(api(), method)


@pytest.mark.parametrize(
    "key,value",
    [
        ("token_id", ""),
        ("condition_id", None),
        ("current_size", None),
        ("current_size", "bad"),
        ("current_size", -1),
        ("current_size", True),
        ("current_size", float("nan")),
        ("current_price", float("inf")),
    ],
)
def test_invalid_core_position_never_masquerades_as_no_exposure(key, value):
    with patch(
        "api.polymarket_api.http_get", return_value=response([position(**{key: value})])
    ):
        with pytest.raises(ValueError, match="Data API"):
            api().get_user_positions("0xFUND")


def test_second_page_missing_position_size_rejects_whole_snapshot():
    bad = position(token_id="456")
    del bad["current_size"]
    with patch(
        "api.polymarket_api.http_get",
        side_effect=[response([position()], "next"), response([bad])],
    ):
        with pytest.raises(ValueError, match="Data API"):
            api().get_user_positions("0xFUND")


@pytest.mark.parametrize("method", ["positions", "activity"])
def test_every_page_uses_wallet_proxy(method):
    obj = api()
    obj.proxy_url = "http://proxy.test:1234"
    row = (
        position()
        if method == "positions"
        else {"type": "REWARD", "timestamp": 1, "usdc_size": 2}
    )
    with patch(
        "api.proxy.requests.get", side_effect=[response([row], "next"), response([])]
    ) as get:
        assert len(_call(obj, method)) == 1
    assert get.call_count == 2
    for call in get.call_args_list:
        assert call.kwargs["proxies"] == {
            "http": "http://proxy.test:1234",
            "https": "http://proxy.test:1234",
        }
