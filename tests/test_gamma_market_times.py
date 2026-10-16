"""tests/test_gamma_market_times.py — 批量取 condition_id 的市场级时间(Gamma createdAt / endDate)。"""

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
        out = PolymarketAPI.gamma_market_times(["0xc1"])
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
        out = PolymarketAPI.gamma_market_times(["0xc1", "0xc2", "0xc3"])
    assert out == {
        "0xc1": {"created_at": "2026-10-12T16:38:12Z"},
        "0xc2": {"end_date": "2026-10-16T00:00:00Z"},
    }


def test_empty_input_no_request():
    with patch("api.proxy.requests.get") as g:
        out = PolymarketAPI.gamma_market_times([])
    assert out == {}
    g.assert_not_called()


def test_full_batches_are_not_truncated_by_gamma_default_limit():
    ids = [f"0xc{i}" for i in range(120)]
    rows = {c: {"conditionId": c, "createdAt": "2026-10-12T16:38:12Z"} for c in ids}
    with patch("api.proxy.requests.get", side_effect=_fake_gamma(rows)) as g:
        out = PolymarketAPI.gamma_market_times(ids)
    assert out == {c: {"created_at": "2026-10-12T16:38:12Z"} for c in ids}
    assert g.call_count == 3  # 50 + 50 + 20,每批的 URL 都在 Gamma 的长度限制内


def test_failed_batch_does_not_discard_successful_batches():
    ids = [f"0xc{i}" for i in range(51)]
    first = _resp([{"conditionId": "0xc0", "createdAt": "2026-10-12T16:38:12Z"}])
    with patch("api.proxy.requests.get", side_effect=[first, Exception("timeout")]):
        out = PolymarketAPI.gamma_market_times(ids)
    assert out == {"0xc0": {"created_at": "2026-10-12T16:38:12Z"}}
