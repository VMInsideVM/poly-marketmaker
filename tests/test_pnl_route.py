"""tests/test_pnl_route.py — /api/pnl 每日盈亏 + 全钱包汇总。"""

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


def test_pnl_single_wallet_series_and_totals(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.upsert_daily_pnl(
        "0xA", "2026-06-01", reward=7, rebate=0.5, sell_profit=2, loss=1, fee=0.1
    )
    db.upsert_daily_pnl(
        "0xA", "2026-06-02", reward=3, rebate=0, sell_profit=0, loss=0, fee=0
    )
    r = client.get("/api/pnl?wallet=0xA&days=3650").get_json()
    assert [s["date"] for s in r["series"]] == ["2026-06-01", "2026-06-02"]
    assert r["series"][0]["reward"] == 7
    # totals: reward 10, net = (7+0.5+2-1-0.1) + 3 = 8.4 + 3 = 11.4
    assert r["totals"]["reward"] == 10
    assert abs(r["totals"]["net"] - 11.4) < 1e-9
    # 累计净利润:第2日 = 8.4 + 3 = 11.4
    assert abs(r["cumulative_net"][-1] - 11.4) < 1e-9


def test_pnl_all_aggregates_across_wallets(tmp_path, monkeypatch):
    client, db = _client_with_db(tmp_path, monkeypatch)
    db.upsert_daily_pnl("0xA", "2026-06-01", 7, 0, 0, 0, 0)
    db.upsert_daily_pnl("0xB", "2026-06-01", 3, 0, 0, 0, 0)
    r = client.get("/api/pnl?wallet=all&days=3650").get_json()
    assert r["series"][0]["reward"] == 10
    assert r["totals"]["reward"] == 10
