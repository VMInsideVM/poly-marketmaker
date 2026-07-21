"""tests/test_wallet_routes.py — 钱包路由(删除清缓存,避免 sig/funder 改了仍读旧实例)。"""

from unittest.mock import MagicMock, patch

import web.routes as routes
from api.proxy import ProxyUnreachable


def _client_logged_in():
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client


def test_remove_wallet_evicts_api_cache(monkeypatch):
    # 旧的余额查询客户端(可能是 sig=2 + 错 funder)缓存在这
    routes._api_cache["0xCAFE"] = object()
    monkeypatch.setattr(routes, "manager", None)
    monkeypatch.setattr(routes, "db", MagicMock())

    resp = _client_logged_in().delete("/api/wallets/0xCAFE")

    assert resp.status_code == 200
    # 删除后必须清掉,否则重新导入(sig/funder 变了)仍命中旧实例 → 余额读错
    assert "0xCAFE" not in routes._api_cache


def test_preview_returns_eoa_and_derived_funder():
    # 纯派生(无网络/不落库):返回签名 EOA + 自动识别的 Safe 地址
    key = "0x" + "1" * 64
    resp = _client_logged_in().post("/api/wallets/preview", json={"private_key": key})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["address"].startswith("0x") and len(body["address"]) == 42
    assert body["derived_funder"].startswith("0x") and len(body["derived_funder"]) == 42


def test_add_wallet_rejected_when_proxy_unreachable(monkeypatch):
    # 代理探测在导入(要走该代理联网)之前:不通就直接拒,既不联网也不落库。
    db = MagicMock()
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "encryption_key", b"k" * 32)
    with patch("api.polymarket_api.PolymarketAPI"):  # 导入本身能成功,只有代理不通
        with patch.object(routes, "probe_proxy", side_effect=ProxyUnreachable("连不上")):
            resp = _client_logged_in().post(
                "/api/wallets",
                json={"private_key": "0x" + "1" * 64, "proxy": "1.2.3.4:12324:u:p"},
            )
    assert resp.status_code == 400
    assert "代理" in resp.get_json()["error"]
    db.add_wallet.assert_not_called()


def test_preview_rejects_bad_key():
    resp = _client_logged_in().post(
        "/api/wallets/preview", json={"private_key": "nope"}
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()
