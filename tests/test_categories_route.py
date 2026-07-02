"""tests/test_categories_route.py — GET /api/categories 契约。"""

from unittest.mock import MagicMock
import web.routes as routes
from models.database import Database


def _client(tmp_path, monkeypatch, manager):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", manager)
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client


def test_categories_no_manager(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, None)
    data = client.get("/api/categories").get_json()
    assert data == {"ready": False, "categories": [], "other_count": 0}


def test_categories_from_manager(tmp_path, monkeypatch):
    mgr = MagicMock()
    mgr.category_catalog.return_value = {
        "ready": True,
        "categories": [{"slug": "politics", "label": "政治", "count": 3}],
        "other_count": 1,
    }
    client = _client(tmp_path, monkeypatch, mgr)
    data = client.get("/api/categories").get_json()
    assert data["ready"] is True
    assert data["categories"][0]["slug"] == "politics"
    assert data["other_count"] == 1


def test_categories_refresh_param_forwarded(tmp_path, monkeypatch):
    """?refresh=1 -> category_catalog(refresh=True);普通请求 -> refresh=False。"""
    mgr = MagicMock()
    mgr.category_catalog.return_value = {
        "ready": True,
        "categories": [],
        "other_count": 0,
    }
    client = _client(tmp_path, monkeypatch, mgr)

    client.get("/api/categories")
    assert mgr.category_catalog.call_args.kwargs == {"refresh": False}

    client.get("/api/categories?refresh=1")
    assert mgr.category_catalog.call_args.kwargs == {"refresh": True}
