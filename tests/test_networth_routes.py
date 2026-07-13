"""tests/test_networth_routes.py — /api/networth 契约。"""

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


def test_networth_series_roundtrip(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.record_net_worth("0xW", 100.0, 20.0)
    data = client.get("/api/networth?wallet=0xW").get_json()
    assert data["wallet"] == "0xW"
    assert len(data["series"]) == 1
    row = data["series"][0]
    assert row["total"] == 120.0 and set(row) == {
        "date",
        "cash",
        "positions_value",
        "total",
    }


def test_networth_missing_wallet_400(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    assert client.get("/api/networth").status_code == 400


def test_networth_bad_days_falls_back_default(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.record_net_worth("0xW", 1.0, 0.0)
    data = client.get("/api/networth?wallet=0xW&days=abc").get_json()
    assert len(data["series"]) == 1  # days 非法回落 90,不 500
