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
