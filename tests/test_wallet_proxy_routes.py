"""tests/test_wallet_proxy_routes.py — 每钱包代理的路由:编辑代理 + 列表回显。"""

from unittest.mock import patch

import web.routes as routes
from api.proxy import ProxyUnreachable
from models.database import Database


def _client_with_db(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client, db


def test_put_proxy_updates_wallet(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.add_wallet("0xABC", "enc")
    with patch.object(routes, "probe_proxy", return_value=("h:1000:u:p", "9.9.9.9")):
        r = client.put("/api/wallets/0xABC/proxy", json={"proxy": "h:1000:u:p"})
    assert r.status_code == 200
    assert db.list_wallets()[0]["proxy"] == "h:1000:u:p"


def test_put_proxy_stores_probed_protocol_and_returns_exit_ip(tmp_path, monkeypatch):
    # 用户只填 host:port:账户:密码;探到是 SOCKS5 就把协议前缀写进库,运行期不再猜。
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.add_wallet("0xABC", "enc")
    with patch.object(
        routes, "probe_proxy", return_value=("socks5:h:1000:u:p", "1.2.3.4")
    ):
        r = client.put("/api/wallets/0xABC/proxy", json={"proxy": "h:1000:u:p"})
    assert db.list_wallets()[0]["proxy"] == "socks5:h:1000:u:p"
    body = r.get_json()
    assert body["protocol"] == "SOCKS5"
    assert body["exit_ip"] == "1.2.3.4"


def test_put_unreachable_proxy_rejected_and_wallet_untouched(tmp_path, monkeypatch):
    # 代理不通就存下来 = 该钱包每轮静默跳过(绝不直连),必须当场拒绝。
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.add_wallet("0xABC", "enc", proxy="old:1:u:p")
    with patch.object(routes, "probe_proxy", side_effect=ProxyUnreachable("连不上")):
        r = client.put("/api/wallets/0xABC/proxy", json={"proxy": "h:1000:u:p"})
    assert r.status_code == 400
    assert db.list_wallets()[0]["proxy"] == "old:1:u:p"


def test_put_empty_proxy_clears(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.add_wallet("0xABC", "enc", proxy="h:1000:u:p")
    with patch.object(routes, "probe_proxy") as p:
        client.put("/api/wallets/0xABC/proxy", json={"proxy": ""})
    p.assert_not_called()  # 清空=直连,不必探测
    assert db.list_wallets()[0]["proxy"] == ""


def test_list_wallets_returns_proxy_without_key(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.add_wallet("0xABC", "enc", proxy="h:1000:u:p")
    wallets = client.get("/api/wallets").get_json()
    assert wallets[0]["proxy"] == "h:1000:u:p"
    assert "encrypted_key" not in wallets[0]  # 列表不泄露私钥
