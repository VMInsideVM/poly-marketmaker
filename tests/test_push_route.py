"""tests/test_push_route.py — /api/push/test 立即发测试消息。"""

import web.routes as routes
from models.database import Database


def _client(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    routes.app.config["TESTING"] = True
    c = routes.app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
    return c, db


def test_push_test_sends(tmp_path, monkeypatch):
    c, db = _client(tmp_path, monkeypatch)
    db.save_settings({"tg_bot_token": "T", "tg_chat_id": "C"})
    called = {}
    monkeypatch.setattr(
        routes, "send_telegram", lambda *a, **k: called.setdefault("hit", a)
    )
    r = c.post("/api/push/test")
    assert r.get_json()["ok"] is True
    assert called["hit"][0] == "T" and called["hit"][1] == "C"


def test_push_test_missing_token(tmp_path, monkeypatch):
    c, db = _client(tmp_path, monkeypatch)
    r = c.post("/api/push/test")
    assert r.status_code == 400 and "error" in r.get_json()


def test_push_test_send_failure_reports_error(tmp_path, monkeypatch):
    c, db = _client(tmp_path, monkeypatch)
    db.save_settings({"tg_bot_token": "T", "tg_chat_id": "C"})

    def _boom(*a, **k):
        raise RuntimeError("chat not found")

    monkeypatch.setattr(routes, "send_telegram", _boom)
    r = c.post("/api/push/test")
    assert "error" in r.get_json() and "chat not found" in r.get_json()["error"]
