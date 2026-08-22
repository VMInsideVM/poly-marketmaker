"""tests/test_gamma_resolution_status.py — 批量取 condition_id 的 umaResolutionStatus。"""

from unittest.mock import patch, MagicMock
from api.polymarket_api import PolymarketAPI


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def test_maps_condition_to_uma_status():
    fake = _resp(
        [
            {"conditionId": "0xc1", "umaResolutionStatus": "proposed"},
            {"conditionId": "0xc2", "umaResolutionStatus": None},
            {"conditionId": "0xc3", "umaResolutionStatus": "resolved"},
        ]
    )
    with patch("api.proxy.requests.get", return_value=fake) as g:
        out = PolymarketAPI.gamma_resolution_status(["0xc1", "0xc2", "0xc3"])
    assert out == {"0xc1": "proposed", "0xc2": None, "0xc3": "resolved"}
    params = g.call_args.kwargs["params"]
    assert ("condition_ids", "0xc1") in params


def test_missing_status_key_maps_to_none():
    fake = _resp([{"conditionId": "0xc1"}])  # 无 umaResolutionStatus 键
    with patch("api.proxy.requests.get", return_value=fake):
        out = PolymarketAPI.gamma_resolution_status(["0xc1"])
    assert out == {"0xc1": None}


def test_empty_input_no_request():
    with patch("api.proxy.requests.get") as g:
        out = PolymarketAPI.gamma_resolution_status([])
    assert out == {}
    g.assert_not_called()


def test_dedups_condition_ids():
    fake = _resp([])
    with patch("api.proxy.requests.get", return_value=fake) as g:
        PolymarketAPI.gamma_resolution_status(["0xc1", "0xc1", "", None])
    ids = [v for (k, v) in g.call_args.kwargs["params"] if k == "condition_ids"]
    assert ids == ["0xc1"]


def test_network_failure_fails_open_returns_empty():
    # Gamma 抖动时返回 {} -> 调用方一律不撤/不跳过,绝不因接口失败误撤全仓买单。
    with patch("api.proxy.requests.get", side_effect=Exception("timeout")):
        out = PolymarketAPI.gamma_resolution_status(["0xc1"])
    assert out == {}


def test_large_input_is_split_into_safe_batches():
    ids = [f"0xc{i}" for i in range(101)]
    response = _resp([])
    with patch("api.proxy.requests.get", return_value=response) as g:
        out = PolymarketAPI.gamma_resolution_status(ids)

    assert out == {}
    assert g.call_count == 3
    batches = [
        [value for key, value in call.kwargs["params"] if key == "condition_ids"]
        for call in g.call_args_list
    ]
    assert [len(batch) for batch in batches] == [50, 50, 1]
    assert batches[0] == ids[:50]
    assert batches[-1] == ids[-1:]


def test_failed_batch_does_not_discard_successful_batches():
    ids = [f"0xc{i}" for i in range(51)]
    first = _resp([{"conditionId": "0xc0", "umaResolutionStatus": "proposed"}])
    with patch(
        "api.proxy.requests.get", side_effect=[first, Exception("timeout")]
    ):
        out = PolymarketAPI.gamma_resolution_status(ids)

    assert out == {"0xc0": "proposed"}
