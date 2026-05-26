"""tests/test_update_routes.py — 更新端点(免登录 + 安全闸)。"""

import web.routes as routes
import web.update as updater


def _client():
    routes.app.config["TESTING"] = True
    return routes.app.test_client()


def test_check_is_public_and_returns_json(monkeypatch):
    monkeypatch.setattr(
        updater, "check_update", lambda: {"update_available": False, "current": "1.0.7"}
    )
    # 关键:不登录也能访问
    resp = _client().get("/api/update/check")
    assert resp.status_code == 200
    assert resp.get_json()["current"] == "1.0.7"


def test_apply_blocked_returns_409(monkeypatch):
    monkeypatch.setattr(
        updater, "start_update", lambda mgr: {"ok": False, "message": "引擎正在运行"}
    )
    resp = _client().post("/api/update/apply")
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False


def test_apply_ok_returns_200(monkeypatch):
    monkeypatch.setattr(updater, "start_update", lambda mgr: {"ok": True})
    resp = _client().post("/api/update/apply")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_status_returns_snapshot():
    updater.STATE.state = "downloading"
    updater.STATE.percent = 42
    resp = _client().get("/api/update/status")
    body = resp.get_json()
    assert body["state"] == "downloading"
    assert body["percent"] == 42
