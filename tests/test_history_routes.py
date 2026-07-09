"""tests/test_history_routes.py — /api/actions 分页契约。"""

import web.routes as routes
from models.database import Database


def _client_with_db(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    # 避免 _enrich_rows 的 Gamma 补名打网络:桩掉返回空(走负缓存)。
    monkeypatch.setattr(routes, "_gamma_fetch", lambda cids: {})
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client, db


def _seed(db, n):
    for i in range(n):
        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.1, 1, f"a{i}", "b")


def test_actions_page1_shape_and_slice(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    _seed(db, 5)
    data = client.get("/api/actions?page=1&page_size=2").get_json()
    assert data["total"] == 5
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert [r["reason"] for r in data["rows"]] == ["a4", "a3"]


def test_actions_page2_slice(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    _seed(db, 5)
    data = client.get("/api/actions?page=2&page_size=2").get_json()
    assert [r["reason"] for r in data["rows"]] == ["a2", "a1"]


def test_actions_out_of_range_page_empty(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    _seed(db, 1)
    data = client.get("/api/actions?page=99&page_size=100").get_json()
    assert data["rows"] == []
    assert data["total"] == 1


def test_actions_default_page_size_100(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    _seed(db, 3)
    data = client.get("/api/actions").get_json()
    assert data["page"] == 1
    assert data["page_size"] == 100
    assert len(data["rows"]) == 3
