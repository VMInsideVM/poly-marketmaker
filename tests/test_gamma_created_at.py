"""tests/test_gamma_created_at.py — 批量取 condition_id 的市场级创建时间(Gamma createdAt)。"""

from unittest.mock import patch, MagicMock
from api.polymarket_api import PolymarketAPI


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def _fake_gamma(created):
    """仿真 Gamma /markets:不带 limit 时最多回 20 条(2026-10-13 实测)。"""

    def get(_url, params: list, **_kw):
        ids = [v for k, v in params if k == "condition_ids"]
        limit = int(dict(params).get("limit", 20))
        rows = [{"conditionId": c, "createdAt": created[c]} for c in ids]
        return _resp(rows[:limit])

    return get


def test_maps_condition_to_market_created_at():
    fake = _resp(
        [
            {"conditionId": "0xc1", "createdAt": "2026-10-12T16:38:12.155593Z"},
            {"conditionId": "0xc2", "createdAt": "2026-09-20T02:07:46.923659Z"},
        ]
    )
    with patch("api.proxy.requests.get", return_value=fake):
        out = PolymarketAPI.gamma_created_at(["0xc1", "0xc2"])
    assert out == {
        "0xc1": "2026-10-12T16:38:12.155593Z",
        "0xc2": "2026-09-20T02:07:46.923659Z",
    }


def test_missing_or_null_created_at_is_left_out():
    # 取不到就不给,调用方保留原值(fail-open),绝不拿 None 覆盖
    fake = _resp([{"conditionId": "0xc1"}, {"conditionId": "0xc2", "createdAt": None}])
    with patch("api.proxy.requests.get", return_value=fake):
        out = PolymarketAPI.gamma_created_at(["0xc1", "0xc2"])
    assert out == {}


def test_empty_input_no_request():
    with patch("api.proxy.requests.get") as g:
        out = PolymarketAPI.gamma_created_at([])
    assert out == {}
    g.assert_not_called()


def test_full_batches_are_not_truncated_by_gamma_default_limit():
    ids = [f"0xc{i}" for i in range(120)]
    created = {c: "2026-10-12T16:38:12Z" for c in ids}
    with patch("api.proxy.requests.get", side_effect=_fake_gamma(created)) as g:
        out = PolymarketAPI.gamma_created_at(ids)
    assert out == created
    assert g.call_count == 3  # 50 + 50 + 20,每批的 URL 都在 Gamma 的长度限制内


def test_failed_batch_does_not_discard_successful_batches():
    ids = [f"0xc{i}" for i in range(51)]
    first = _resp([{"conditionId": "0xc0", "createdAt": "2026-10-12T16:38:12Z"}])
    with patch("api.proxy.requests.get", side_effect=[first, Exception("timeout")]):
        out = PolymarketAPI.gamma_created_at(ids)
    assert out == {"0xc0": "2026-10-12T16:38:12Z"}
