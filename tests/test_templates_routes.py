"""tests/test_templates_routes.py — /api/templates CRUD + 钱包绑定(Flask client + 真 DB)。"""

import web.routes as routes
from models.database import Database


def _client(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client, db


def test_list_templates_includes_default(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    rows = client.get("/api/templates").get_json()
    assert any(r["is_default"] for r in rows)
    assert all({"id", "name", "is_default"} <= set(r) for r in rows)


def test_create_get_save_roundtrip(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    tid = client.post("/api/templates", json={"name": "激进"}).get_json()["id"]
    got = client.get(f"/api/templates/{tid}").get_json()
    assert "max_exposure_usd" in got  # 返回合并默认值
    client.put(
        f"/api/templates/{tid}",
        json={
            "max_exposure_usd": 99,
            "tier_rules": [[{"upper": None, "action": {"type": "min_size"}}]],
        },
    )
    saved = db.get_template(tid)
    assert saved["max_exposure_usd"] == 99
    assert saved["tier_rules"] == [[{"upper": None, "action": {"type": "min_size"}}]]


def test_create_duplicate_name_400(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    client.post("/api/templates", json={"name": "dup"})
    resp = client.post("/api/templates", json={"name": "dup"})
    assert resp.status_code == 400


def test_create_empty_name_400(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    assert client.post("/api/templates", json={"name": "  "}).status_code == 400


def test_rename_route(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    tid = client.post("/api/templates", json={"name": "x"}).get_json()["id"]
    client.put(f"/api/templates/{tid}/name", json={"name": "y"})
    assert any(t["name"] == "y" for t in db.list_templates())
    client.post("/api/templates", json={"name": "z"})
    assert (
        client.put(f"/api/templates/{tid}/name", json={"name": "z"}).status_code == 400
    )


def test_delete_template_and_default_guard(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    tid = client.post("/api/templates", json={"name": "tmp"}).get_json()["id"]
    assert client.delete(f"/api/templates/{tid}").status_code == 200
    assert all(t["id"] != tid for t in db.list_templates())
    default_id = db.get_default_template_id()
    assert client.delete(f"/api/templates/{default_id}").status_code == 400


def test_bind_wallet_to_template(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    db.add_wallet("0xW", "enc", "0xF", 2)
    tid = client.post("/api/templates", json={"name": "bind"}).get_json()["id"]
    client.put(f"/api/templates/{tid}", json={"max_exposure_usd": 123})
    resp = client.post("/api/wallets/0xW/template", json={"template_id": tid})
    assert resp.status_code == 200
    assert db.get_template_for("0xW")["max_exposure_usd"] == 123


def test_put_template_filters_non_strategy_keys(tmp_path, monkeypatch):
    client, db = _client(tmp_path, monkeypatch)
    tid = client.post("/api/templates", json={"name": "f"}).get_json()["id"]
    client.put(
        f"/api/templates/{tid}", json={"scan_interval_sec": 99, "max_exposure_usd": 7}
    )
    saved = db.get_template(tid)
    assert saved["max_exposure_usd"] == 7
    assert "scan_interval_sec" not in saved  # 引擎键被丢弃,不进模板
