"""tests/test_activity_api.py — get_activity 拉 Data API /activity（分页 + 参数）。"""

from unittest.mock import patch, MagicMock
from api.polymarket_api import PolymarketAPI


def _api():
    api = PolymarketAPI.__new__(PolymarketAPI)  # 不走 __init__（免私钥/网络）
    api.get_funder = lambda: "0xFUND"
    api.proxy_url = None
    return api


def _resp(items):
    m = MagicMock()
    m.json.return_value = items
    m.raise_for_status.return_value = None
    return m


def test_get_activity_paginates_until_short_page():
    api = _api()
    page1 = [{"type": "REWARD", "usdcSize": 1.0, "timestamp": 1} for _ in range(500)]
    page2 = [{"type": "TRADE", "usdcSize": 2.0, "timestamp": 2}]
    with patch(
        "api.polymarket_api.http_get", side_effect=[_resp(page1), _resp(page2)]
    ) as g:
        out = api.get_activity()
    assert len(out) == 501
    assert g.call_count == 2  # 满页续拉、短页停


def test_get_activity_passes_type_and_window():
    api = _api()
    with patch("api.polymarket_api.http_get", return_value=_resp([])) as g:
        api.get_activity(types=["REWARD", "MAKER_REBATE"], start=100, end=200)
    params = g.call_args.kwargs["params"]
    assert params["user"] == "0xFUND"
    assert params["type"] == "REWARD,MAKER_REBATE"
    assert params["start"] == 100 and params["end"] == 200
