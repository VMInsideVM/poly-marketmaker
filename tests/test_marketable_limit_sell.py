"""tests/test_marketable_limit_sell.py — FAK 限价卖原语(mock client,不触网)。"""

from unittest.mock import MagicMock
from py_clob_client_v2.clob_types import OrderType
from api.polymarket_api import PolymarketAPI


def test_marketable_limit_sell_uses_fak_order_type():
    api = object.__new__(PolymarketAPI)  # 跳过重构造(funder 派生等)
    api.client = MagicMock()
    api.client.create_and_post_order.return_value = {
        "orderID": "x",
        "status": "matched",
    }
    api.place_marketable_limit_sell("tok1", 0.09, 100, tick_size="0.01")
    args, kwargs = api.client.create_and_post_order.call_args
    assert args[2] == OrderType.FAK
    order_args = args[0]
    assert getattr(order_args, "side", None) == "SELL"
    assert abs(float(getattr(order_args, "price")) - 0.09) < 1e-9
