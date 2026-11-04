"""tests/test_gamma_market_details.py — 批量取 condition_id 的市场级时间(Gamma createdAt / endDate)。"""

from unittest.mock import patch, MagicMock
from api.polymarket_api import PolymarketAPI


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def _fake_gamma(rows_by_cid):
    """仿真 Gamma /markets:不带 limit 时最多回 20 条(2026-10-13 实测)。"""

    def get(_url, params: list, **_kw):
        ids = [v for k, v in params if k == "condition_ids"]
        limit = int(dict(params).get("limit", 20))
        return _resp([rows_by_cid[c] for c in ids][:limit])

    return get


def test_maps_condition_to_market_created_and_end():
    fake = _resp(
        [
            {
                "conditionId": "0xc1",
                "createdAt": "2026-10-12T16:38:12.155593Z",
                "endDate": "2026-10-16T00:00:00Z",
            },
        ]
    )
    with patch("api.proxy.requests.get", return_value=fake):
        out = PolymarketAPI.gamma_market_details(["0xc1"])
    assert out == {
        "0xc1": {
            "created_at": "2026-10-12T16:38:12.155593Z",
            "end_date": "2026-10-16T00:00:00Z",
        }
    }


def test_missing_or_null_fields_are_left_out():
    # 取不到的字段不给,调用方保留原值(fail-open),绝不拿 None 覆盖
    fake = _resp(
        [
            {"conditionId": "0xc1", "createdAt": "2026-10-12T16:38:12Z"},
            {
                "conditionId": "0xc2",
                "createdAt": None,
                "endDate": "2026-10-16T00:00:00Z",
            },
            {"conditionId": "0xc3"},
        ]
    )
    with patch("api.proxy.requests.get", return_value=fake):
        out = PolymarketAPI.gamma_market_details(["0xc1", "0xc2", "0xc3"])
    assert out == {
        "0xc1": {"created_at": "2026-10-12T16:38:12Z"},
        "0xc2": {"end_date": "2026-10-16T00:00:00Z"},
    }


def test_empty_input_no_request():
    with patch("api.proxy.requests.get") as g:
        out = PolymarketAPI.gamma_market_details([])
    assert out == {}
    g.assert_not_called()


def test_full_batches_are_not_truncated_by_gamma_default_limit():
    ids = [f"0xc{i}" for i in range(120)]
    rows = {c: {"conditionId": c, "createdAt": "2026-10-12T16:38:12Z"} for c in ids}
    with patch("api.proxy.requests.get", side_effect=_fake_gamma(rows)) as g:
        out = PolymarketAPI.gamma_market_details(ids)
    assert out == {c: {"created_at": "2026-10-12T16:38:12Z"} for c in ids}
    assert g.call_count == 3  # 50 + 50 + 20,每批的 URL 都在 Gamma 的长度限制内


def test_failed_batch_does_not_discard_successful_batches():
    ids = [f"0xc{i}" for i in range(51)]
    first = _resp([{"conditionId": "0xc0", "createdAt": "2026-10-12T16:38:12Z"}])
    with patch("api.proxy.requests.get", side_effect=[first, Exception("timeout")]):
        out = PolymarketAPI.gamma_market_details(ids)
    assert out == {"0xc0": {"created_at": "2026-10-12T16:38:12Z"}}


def test_failed_batch_is_retried_once():
    # 读请求重试无副作用;不重试的话这批市场只能退回事件时间,保护出窟窿
    ok = _resp([{"conditionId": "0xc1", "createdAt": "2026-10-12T16:38:12Z"}])
    with patch("api.proxy.requests.get", side_effect=[Exception("timeout"), ok]):
        out = PolymarketAPI.gamma_market_details(["0xc1"])
    assert out == {"0xc1": {"created_at": "2026-10-12T16:38:12Z"}}


def test_every_batch_goes_through_the_wallet_proxy():
    # 批次并发查,每个 worker 线程都必须带上调用方的代理,绝不直连
    from api.proxy import use_proxy

    ids = [f"0xc{i}" for i in range(260)]
    rows = {c: {"conditionId": c, "createdAt": "2026-10-12T16:38:12Z"} for c in ids}
    with patch("api.proxy.requests.get", side_effect=_fake_gamma(rows)) as g:
        with use_proxy("http://p:1"):
            out = PolymarketAPI.gamma_market_details(ids)
    assert len(out) == 260
    assert g.call_count == 6
    assert all(
        c.kwargs.get("proxies") == {"http": "http://p:1", "https": "http://p:1"}
        for c in g.call_args_list
    )


def test_non_list_response_is_treated_as_failed_batch():
    # Gamma 偶尔回错误对象而不是列表;当成该批失败,不能让调用方逐条 .get 时崩掉
    with patch("api.proxy.requests.get", return_value=_resp({"error": "bad"})):
        assert PolymarketAPI.gamma_market_details(["0xc1"]) == {}
        assert PolymarketAPI.gamma_resolution_status(["0xc1"]) == {}


def test_returns_market_tag_slugs_and_asks_for_them():
    fake = _resp(
        [
            {
                "conditionId": "0xc1",
                "createdAt": "2026-10-12T16:38:12Z",
                "tags": [{"slug": "sports"}, {"slug": "soccer"}],
            },
        ]
    )
    with patch("api.proxy.requests.get", return_value=fake) as g:
        out = PolymarketAPI.gamma_market_details(["0xc1"])
    assert out["0xc1"]["tag_slugs"] == ["sports", "soccer"]
    assert ("include_tag", "true") in g.call_args.kwargs["params"]


def test_missing_closed_market_is_recovered_without_requerying_open_market():
    """奖励池仍有已关闭盘；默认 Gamma 隐藏它，不能误报其标签消失。"""
    open_row = {"conditionId": "open", "tags": [{"slug": "politics"}]}
    closed_row = {
        "conditionId": "closed",
        "closed": True,
        "createdAt": "2026-10-25T00:00:00Z",
        "endDate": "2026-11-03T00:00:00Z",
        "tags": [{"slug": "elections"}],
    }
    with patch(
        "api.proxy.requests.get", side_effect=[_resp([open_row]), _resp([closed_row])]
    ) as get:
        out = PolymarketAPI.gamma_market_details(["open", "closed"])
    assert out["closed"] == {
        "created_at": "2026-10-25T00:00:00Z",
        "end_date": "2026-11-03T00:00:00Z",
        "tag_slugs": ["elections"],
    }
    assert out["open"]["tag_slugs"] == ["politics"]
    assert get.call_count == 2
    second = get.call_args_list[1].kwargs["params"]
    assert [v for k, v in second if k == "condition_ids"] == ["closed"]
    assert ("closed", "true") in second
    assert ("limit", 1) in second and ("include_tag", "true") in second


def test_closed_lookup_failure_keeps_successful_open_rows_and_retries_once():
    with patch(
        "api.proxy.requests.get",
        side_effect=[
            _resp([{"conditionId": "open", "tags": [{"slug": "politics"}]}]),
            RuntimeError("closed lookup unavailable"),
            RuntimeError("still unavailable"),
        ],
    ) as get:
        out = PolymarketAPI.gamma_market_details(["open", "closed"])
    assert out == {"open": {"tag_slugs": ["politics"]}}
    assert get.call_count == 3


def test_complete_open_batch_does_not_make_closed_lookup():
    with patch(
        "api.proxy.requests.get",
        return_value=_resp([{"conditionId": "open", "tags": [{"slug": "politics"}]}]),
    ) as get:
        assert PolymarketAPI.gamma_market_details(["open"]) == {
            "open": {"tag_slugs": ["politics"]}
        }
    assert get.call_count == 1


def test_failed_initial_read_does_not_double_network_failure_retries():
    with patch("api.proxy.requests.get", side_effect=RuntimeError("offline")) as get:
        assert PolymarketAPI.gamma_market_details(["closed"]) == {}
    assert get.call_count == 2


def test_closed_market_lookup_preserves_proxy_for_every_read():
    from api.proxy import use_proxy

    with patch(
        "api.proxy.requests.get",
        side_effect=[
            _resp([]),
            _resp([{"conditionId": "closed", "tags": [{"slug": "elections"}]}]),
        ],
    ) as get:
        with use_proxy("http://proxy.test:1234"):
            out = PolymarketAPI.gamma_market_details(["closed"])
    assert out == {"closed": {"tag_slugs": ["elections"]}}
    assert get.call_count == 2
    assert all(
        c.kwargs["proxies"]
        == {"http": "http://proxy.test:1234", "https": "http://proxy.test:1234"}
        for c in get.call_args_list
    )
