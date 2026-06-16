"""tests/test_settings_routes.py — /api/settings GET/POST 的 v4 参数 round-trip 契约。

路由本就按键归类(引擎键->settings、策略键->默认模板)且 json.dumps 存值,故对
现有代码即通过。本测试把这一契约钉死,作为 SP6a 前端依赖的回归闸。"""

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


def test_get_settings_returns_v4_params(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    data = client.get("/api/settings").get_json()
    for k in (
        "theta_loss_cents",
        "theta_stop_cents",
        "case_a_mode",
        "tier_rules",
        "per_share_reward_thresholds",
        "excluded_categories",
        "max_exposure_usd",
        "max_exposure_shares",
        "max_concurrent_markets",
        "min_price_double_cents",
        "discovery_interval_sec",
    ):
        assert k in data, f"GET /api/settings 缺 {k}"


def test_post_settings_roundtrips_structured(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    payload = {
        "theta_loss_cents": 3,
        "case_a_mode": "market",
        "excluded_categories": ["sports"],
        "per_share_reward_thresholds": {
            "20": 0.5,
            "50": 0.4,
            "100": 0.3,
            "200": 0.3,
            "250": 0.3,
        },
        "tier_rules": [
            [{"upper": None, "action": {"type": "fixed_shares", "shares": 50}}]
        ],
        "discovery_interval_sec": 7200,
    }
    resp = client.post("/api/settings", json=payload)
    assert resp.status_code == 200
    tmpl = db.get_template(db.get_default_template_id())
    assert tmpl["theta_loss_cents"] == 3
    assert tmpl["case_a_mode"] == "market"
    assert tmpl["excluded_categories"] == ["sports"]
    assert tmpl["per_share_reward_thresholds"]["20"] == 0.5
    assert tmpl["tier_rules"] == [
        [{"upper": None, "action": {"type": "fixed_shares", "shares": 50}}]
    ]
    assert db.get_settings()["discovery_interval_sec"] == 7200


def test_post_settings_routes_engine_vs_template(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    client.post("/api/settings", json={"scan_interval_sec": 45, "max_exposure_usd": 99})
    assert db.get_settings()["scan_interval_sec"] == 45
    assert db.get_template(db.get_default_template_id())["max_exposure_usd"] == 99
    assert "scan_interval_sec" not in db.get_template(db.get_default_template_id())
    assert "max_exposure_usd" not in db.get_settings()
