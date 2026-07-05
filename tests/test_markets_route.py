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


def test_eligible_derives_display_metrics(client):
    r = client.get("/api/eligible")
    row = r.get_json()["markets"][0]
    # 无订单簿(闲时/DB)时 reward_range/spread 覆盖为 None -> 前端显「—」,不沿用落库占位值
    assert row["reward_range_min"] is None
    assert row["spread_cents"] is None


def test_ladder_preview_route(client):
    r = client.get("/api/markets/c1/ladder?wallet=0xw")
    assert r.status_code == 200
    data = r.get_json()
    assert data["market_id"] == "c1"
    side = data["sides"][0]
    assert side["outcome"] == "YES"
    assert "levels" in side and side["levels"][0]["thickness"] == 1.5


def test_ladder_reads_tokens_from_pool_shape(client, monkeypatch):
    # 内存候选池形态:一条市场 + tokens 列表、无顶层 token_id —— 不能 KeyError,
    # 要按 tokens 迭代出各侧。
    from unittest.mock import MagicMock

    mgr = MagicMock()
    mgr.eligible_markets = [
        {
            "market_id": "c1",
            "tokens": [{"token_id": "tY", "outcome": "YES"}],
            "rewards_min_size": 100,
            "rewards_max_spread": 4,
        }
    ]
    monkeypatch.setattr(routes, "manager", mgr)
    r = client.get("/api/markets/c1/ladder?wallet=0xw")
    assert r.status_code == 200
    data = r.get_json()
    assert data["market_id"] == "c1"
    assert data["sides"][0]["outcome"] == "YES"


def test_ladder_returns_json_error_on_exception(client, monkeypatch):
    # 预演中途抛异常时必须回 JSON 错误(带 500),不能吐 HTML 页 —— 否则前端 r.json()
    # 会炸「Unexpected token '<'」把真因盖住。
    class BoomAPI(_FakeAPI):
        def get_balance(self):
            raise RuntimeError("boom balance")

    monkeypatch.setattr(routes, "_wallet_apis", lambda only=None: {"0xw": BoomAPI()})
    r = client.get("/api/markets/c1/ladder?wallet=0xw")
    assert r.status_code == 500
    data = r.get_json()  # 若返回的是 HTML,这里会是 None
    assert data is not None and "boom balance" in data["error"]


def test_scan_status_no_manager(client):
    d = client.get("/api/engine/scan-status").get_json()
    assert d["scan_status"] == "idle" and d["scan_total"] == 0 and d["found"] == 0


def test_scan_status_from_manager(client, monkeypatch):
    from unittest.mock import MagicMock

    mgr = MagicMock()
    mgr.scan_status = "scanning"
    mgr.scan_checked = 5
    mgr.scan_total = 20
    mgr.eligible_markets = [{"market_id": "a"}, {"market_id": "b"}]
    mgr.last_scan_time = 111.0
    monkeypatch.setattr(routes, "manager", mgr)
    d = client.get("/api/engine/scan-status").get_json()
    assert d["scan_status"] == "scanning"
    assert d["scan_checked"] == 5 and d["scan_total"] == 20
    assert d["found"] == 2


def test_scan_delegates_and_reports_started(client, monkeypatch):
    from unittest.mock import MagicMock

    mgr = MagicMock()
    mgr.start_scan_async.return_value = False  # 已在扫描
    monkeypatch.setattr(routes, "manager", mgr)
    d = client.post("/api/engine/scan").get_json()
    mgr.start_scan_async.assert_called_once()
    assert d["ok"] is True and d["started"] is False and d["scanning"] is True


def test_scan_force_param_forwarded(client, monkeypatch):
    from unittest.mock import MagicMock

    mgr = MagicMock()
    mgr.start_scan_async.return_value = True
    monkeypatch.setattr(routes, "manager", mgr)
    client.post("/api/engine/scan")
    assert mgr.start_scan_async.call_args.kwargs == {"force": False}
    client.post("/api/engine/scan?force=1")
    assert mgr.start_scan_async.call_args.kwargs == {"force": True}
