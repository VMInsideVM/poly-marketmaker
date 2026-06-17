"""tests/test_markets_route.py — 预演路由 + eligible per_share 派生契约。"""

import pytest
from web import routes


class _FakeAPI:
    def get_funder(self):
        return "0xfunder"

    def get_balance(self):
        return 1000.0

    def get_user_positions(self, funder):
        return []

    def get_orderbook(self, token_id):
        return {
            "bids": [
                {"price": "0.54", "size": "150"},
                {"price": "0.52", "size": "300"},
            ],
            "asks": [{"price": "0.56", "size": "150"}],
            "tick_size": "0.01",
        }


class _FakeDB:
    def get_template_for(self, addr):
        return {
            "tier_rules": [[{"upper": None, "action": {"type": "min_size"}}]],
            "max_exposure_usd": 250,
            "max_exposure_shares": 500,
            "per_share_reward_thresholds": {"100": 0.30},
        }

    def get_eligible_markets(self):
        return [
            {
                "market_id": "c1",
                "token_id": "tY",
                "outcome": "YES",
                "market_name": "Q",
                "daily_reward": 40.0,
                "rewards_min_size": 100,
                "rewards_max_spread": 4,
                "reward_range_min": 0.51,
                "reward_range_max": 0.59,
                "spread_cents": 2.0,
                "tags": [],
            }
        ]


@pytest.fixture
def client(monkeypatch):
    routes.app.config["TESTING"] = True
    monkeypatch.setattr(routes, "db", _FakeDB())
    monkeypatch.setattr(routes, "manager", None)
    monkeypatch.setattr(routes, "_wallet_apis", lambda only=None: {"0xw": _FakeAPI()})
    monkeypatch.setattr(routes, "_enrich_rows", lambda rows, key: None)
    with routes.app.test_client() as c:
        with c.session_transaction() as s:
            s["logged_in"] = True
        yield c


def test_eligible_derives_per_share(client):
    r = client.get("/api/eligible")
    row = r.get_json()["markets"][0]
    assert round(row["per_share"], 4) == 0.40  # 40 / 100
    assert row["per_share_bracket"] == 100
    assert round(row["per_share_threshold"], 2) == 0.30


def test_ladder_preview_route(client):
    r = client.get("/api/markets/c1/ladder?wallet=0xw")
    assert r.status_code == 200
    data = r.get_json()
    assert data["market_id"] == "c1"
    side = data["sides"][0]
    assert side["outcome"] == "YES"
    assert "levels" in side and side["levels"][0]["thickness"] == 1.5
