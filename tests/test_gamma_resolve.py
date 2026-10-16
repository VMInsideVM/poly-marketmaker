"""tests/test_gamma_resolve.py"""

from unittest.mock import patch, MagicMock
from api.polymarket_api import PolymarketAPI


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def test_maps_gamma_fields():
    fake = _resp(
        [
            {
                "conditionId": "0xc1",
                "question": "Q1",
                "slug": "s1",
                "events": [{"slug": "e1"}],
            },
            {"conditionId": "0xc2", "question": "Q2", "slug": "s2", "events": []},
        ]
    )
    with patch("api.proxy.requests.get", return_value=fake) as g:
        out = PolymarketAPI.gamma_markets_by_condition(["0xc1", "0xc2"])
    assert out["0xc1"] == {"name": "Q1", "market_slug": "s1", "event_slug": "e1"}
    assert out["0xc2"] == {"name": "Q2", "market_slug": "s2", "event_slug": ""}
    params = g.call_args.kwargs["params"]
    assert ("condition_ids", "0xc1") in params
    assert ("condition_ids", "0xc2") in params


def test_empty_input_no_request():
    with patch("api.proxy.requests.get") as g:
        out = PolymarketAPI.gamma_markets_by_condition([])
    assert out == {}
    g.assert_not_called()


def test_dedups_and_drops_empty():
    fake = _resp([])
    with patch("api.proxy.requests.get", return_value=fake) as g:
        PolymarketAPI.gamma_markets_by_condition(["0xc1", "0xc1", "", None])
    ids = [v for (k, v) in g.call_args.kwargs["params"] if k == "condition_ids"]
    assert ids == ["0xc1"]


def _fake_gamma(open_rows, closed_rows=()):
    """仿真 Gamma /markets:不带 limit 时最多回 20 条;默认(及 closed=false)不回已关闭
    市场,closed=true 只回已关闭的(2026-10-13/16 实测)。"""

    def get(_url, params: list, **_kw):
        ids = [v for k, v in params if k == "condition_ids"]
        limit = int(dict(params).get("limit", 20))
        rows = closed_rows if dict(params).get("closed") == "true" else open_rows
        hit = [r for r in rows if r["conditionId"] in ids]
        return _resp(hit[:limit])

    return get


def _row(cid):
    return {"conditionId": cid, "question": "Q" + cid, "slug": "s", "events": []}


def test_full_batch_is_not_truncated_by_gamma_default_limit():
    # 少回的 30 个会被调用方当成「Gamma 查无此市场」写负缓存,名字从此永远是空的
    ids = [f"0xc{i}" for i in range(50)]
    with patch("api.proxy.requests.get", side_effect=_fake_gamma([_row(c) for c in ids])):
        out = PolymarketAPI.gamma_markets_by_condition(ids)
    assert set(out) == set(ids)


def test_closed_markets_are_resolved_too():
    # 历史页里多是已结算的市场,Gamma 默认不回它们
    fake = _fake_gamma([_row("0xopen")], [_row("0xclosed")])
    with patch("api.proxy.requests.get", side_effect=fake):
        out = PolymarketAPI.gamma_markets_by_condition(["0xopen", "0xclosed"])
    assert out["0xopen"]["name"] == "Q0xopen"
    assert out["0xclosed"]["name"] == "Q0xclosed"


def test_failure_raises_so_caller_does_not_negative_cache():
    import pytest

    with patch("api.proxy.requests.get", side_effect=Exception("timeout")):
        with pytest.raises(Exception):
            PolymarketAPI.gamma_markets_by_condition(["0xc1"])
