"""tests/test_rewards_tag_param.py — get_rewards_markets 的 tag_slug 参数构造(不触网)。"""

from unittest.mock import patch, MagicMock
from api.polymarket_api import PolymarketAPI


def _fake_resp(data):
    m = MagicMock()
    m.json.return_value = {"data": data, "next_cursor": "LTE="}
    m.raise_for_status.return_value = None
    return m


def test_tag_slug_single_passed_as_param():
    with patch("api.polymarket_api.requests.get", return_value=_fake_resp([])) as g:
        PolymarketAPI.get_rewards_markets(tag_slug="sports")
        _, kwargs = g.call_args
        assert kwargs["params"]["tag_slug"] == "sports"


def test_tag_slug_none_absent_from_params():
    with patch("api.polymarket_api.requests.get", return_value=_fake_resp([])) as g:
        PolymarketAPI.get_rewards_markets()
        _, kwargs = g.call_args
        assert "tag_slug" not in kwargs["params"]
