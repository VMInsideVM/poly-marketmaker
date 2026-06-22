"""tests/test_wallet_proxy_routes.py — 每钱包代理的路由:编辑代理 + 列表回显。"""

import web.routes as routes
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
    r = client.put("/api/wallets/0xABC/proxy", json={"proxy": "h:1000:u:p"})
    assert r.status_code == 200
    assert db.list_wallets()[0]["proxy"] == "h:1000:u:p"


def test_put_empty_proxy_clears(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.add_wallet("0xABC", "enc", proxy="h:1000:u:p")
    client.put("/api/wallets/0xABC/proxy", json={"proxy": ""})
    assert db.list_wallets()[0]["proxy"] == ""


def test_list_wallets_returns_proxy_without_key(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.add_wallet("0xABC", "enc", proxy="h:1000:u:p")
    wallets = client.get("/api/wallets").get_json()
    assert wallets[0]["proxy"] == "h:1000:u:p"
    assert "encrypted_key" not in wallets[0]  # 列表不泄露私钥
