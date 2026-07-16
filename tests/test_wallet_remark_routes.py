"""tests/test_wallet_remark_routes.py — 每钱包备注的路由:编辑 + 列表回显 + 截断。"""

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


def test_put_remark_updates_wallet(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.add_wallet("0xABC", "enc")
    r = client.put("/api/wallets/0xABC/remark", json={"remark": "主号"})
    assert r.status_code == 200
    assert db.list_wallets()[0]["remark"] == "主号"


def test_put_empty_remark_clears(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.add_wallet("0xABC", "enc", remark="主号")
    client.put("/api/wallets/0xABC/remark", json={"remark": ""})
    assert db.list_wallets()[0]["remark"] == ""


def test_put_remark_truncated_to_40(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.add_wallet("0xABC", "enc")
    client.put("/api/wallets/0xABC/remark", json={"remark": "x" * 50})
    assert db.list_wallets()[0]["remark"] == "x" * 40


def test_list_wallets_returns_remark(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.add_wallet("0xABC", "enc", remark="小号2")
    wallets = client.get("/api/wallets").get_json()
    assert wallets[0]["remark"] == "小号2"
    assert "encrypted_key" not in wallets[0]
