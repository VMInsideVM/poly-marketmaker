"""tests/test_polymarket_api.py — PolymarketAPI 构造时的 L1 凭证派生。"""

from unittest.mock import MagicMock, patch

from api.polymarket_api import PolymarketAPI


@patch("api.polymarket_api.derive_deposit_address", return_value="0xFunder")
@patch("api.polymarket_api.ClobClient")
def test_init_creates_api_creds_for_new_wallet(mock_clob, _mock_derive):
    """新钱包从没在 Polymarket 创建过 API key,derive 会取不到。

    构造必须走 create_or_derive_api_key(先 create,失败再 derive),否则
    新导入的浏览器钱包会报 'Could not derive api key!'。
    """
    temp = MagicMock()
    temp.get_address.return_value = "0xEOA"
    mock_clob.return_value = temp  # 两次 ClobClient(...) 都返回同一个 mock

    PolymarketAPI("0xprivkey")

    temp.create_or_derive_api_key.assert_called_once()
    # derive-only 路径对新钱包会失败,不能再单独依赖它。
    temp.derive_api_key.assert_not_called()
