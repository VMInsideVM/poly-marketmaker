"""tests/test_polymarket_api.py — PolymarketAPI 构造时的 L1 凭证派生。"""

from unittest.mock import MagicMock, patch

from api.polymarket_api import (
    PolymarketAPI,
    pick_funded_sig_type,
    resolve_signature_type,
)


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


class TestResolveSignatureType:
    def test_blank_funder_is_safe(self):
        assert resolve_signature_type("0xSafe", None) == 2
        assert resolve_signature_type("0xSafe", "") == 2

    def test_funder_equals_derived_safe_is_safe(self):
        # 大小写不敏感比较
        assert resolve_signature_type("0xAbCdef", "0xabcdef") == 2

    def test_funder_differs_is_proxy(self):
        # 填的存款地址 ≠ 派生 Safe → Polymarket Proxy 账户
        assert resolve_signature_type("0xSafe", "0x8CfA") == 1


class TestPickFundedSigType:
    def test_picks_sig_with_balance(self):
        # POLY_1271(智能合约钱包)账户:只有 sig=3 有钱
        assert pick_funded_sig_type({0: "0", 1: "0", 2: "0", 3: "10000000"}) == 3

    def test_all_zero_returns_none(self):
        assert pick_funded_sig_type({0: "0", 1: "0", 2: "0", 3: "0"}) is None

    def test_picks_safe_when_funded_there(self):
        assert pick_funded_sig_type({0: "0", 2: "5000000"}) == 2

    def test_ignores_error_entries(self):
        assert pick_funded_sig_type({1: "ERR boom", 3: "10000000"}) == 3


@patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
@patch("api.polymarket_api.ClobClient")
def test_init_passes_signature_type_and_funder(mock_clob, _mock_derive):
    temp = MagicMock()
    temp.get_address.return_value = "0xEOA"
    mock_clob.return_value = temp

    PolymarketAPI("0xpk", signature_type=1, funder="0xPROXY")

    # 第二次构造(带 L2 鉴权的正式 client)必须用传入的 sig 和 funder
    full_call = mock_clob.call_args_list[1]
    assert full_call.kwargs["signature_type"] == 1
    assert full_call.kwargs["funder"] == "0xPROXY"


@patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
@patch("api.polymarket_api.ClobClient")
def test_get_balance_uses_instance_signature_type(mock_clob, _mock_derive):
    temp = MagicMock()
    temp.get_address.return_value = "0xEOA"
    temp.get_balance_allowance.return_value = {"balance": "10000000"}
    mock_clob.return_value = temp

    api = PolymarketAPI("0xpk", signature_type=1, funder="0xPROXY")
    bal = api.get_balance()

    params = temp.get_balance_allowance.call_args.args[0]
    assert params.signature_type == 1
    assert bal == 10.0
