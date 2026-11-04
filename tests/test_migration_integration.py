"""迁移链路回归：真实 V2 适配器接引擎/页面，外部 HTTP 和交易提交全部替身。

不构造签名客户端、不读用户数据库。socket 护栏保证遗漏的外部请求也无法出网。
"""

from unittest.mock import MagicMock

import pytest
from requests import HTTPError

from api.polymarket_api import PolymarketAPI
from engine.pnl_ledger import rebuild_wallet_pnl
from models.database import Database
from tests.test_data_api_v2 import api as data_api, position, response
from tests.test_monitor import _make_monitor
from tests.test_place_orders import _make_worker, _elig, _ob
from web import routes


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("迁移集成测试禁止真实网络/交易")

    monkeypatch.setattr("socket.socket.connect", deny)
    monkeypatch.setattr(PolymarketAPI, "__init__", deny)


def wire_positions(monkeypatch, target, rows):
    source = data_api()
    target.get_user_positions = source.get_user_positions
    target.get_funder.return_value = "0xFUND"
    target.proxy_url = None
    get = MagicMock(return_value=response(rows))
    monkeypatch.setattr("api.polymarket_api.http_get", get)
    return get


def test_v2_position_on_second_page_blocks_buying_both_outcomes(monkeypatch):
    worker, api, db = _make_worker()
    wire_positions(monkeypatch, api, [])
    monkeypatch.setattr(
        "api.polymarket_api.http_get",
        MagicMock(
            side_effect=[
                response([], "next"),
                response([position(token_id="A-y", condition_id="A")]),
            ]
        ),
    )
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes"), _elig("A", "A-n", "No")])
    api.place_limit_buy.assert_not_called()
    assert db.get_template_for.called  # 已通过原先导致整轮 return 的持仓读取点


def test_v2_empty_holdings_allows_normal_placement_decision(monkeypatch):
    worker, api, db = _make_worker()
    wire_positions(monkeypatch, api, [])
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    # 仅观察测试替身收到的意图；无签名客户端、无网络提交。
    api.place_limit_buy.assert_called_once()
    assert api.place_limit_buy.call_args.args[0] == "A-y"


def test_failed_second_page_prevents_any_new_buy(monkeypatch, caplog):
    worker, api, db = _make_worker()
    wire_positions(monkeypatch, api, [])
    broken = response([])
    broken.raise_for_status.side_effect = HTTPError("503 unavailable")
    monkeypatch.setattr(
        "api.polymarket_api.http_get",
        MagicMock(
            side_effect=[
                response([], "next"),
                broken,
            ]
        ),
    )
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_not_called()
    assert "skip placement" in caplog.text


def maker_buy(size=100):
    return {
        "id": "real-cost-trade",
        "match_time": "1793667600",
        "trader_side": "MAKER",
        "asset_id": "123",
        "maker_orders": [
            {
                "maker_address": "0xFUND",
                "asset_id": "123",
                "side": "BUY",
                "matched_amount": str(size),
                "price": "0.40",
            }
        ],
    }


def test_v2_monitor_reconstructs_cost_from_clob_not_data_api_average(monkeypatch):
    monitor, api, db = _make_monitor({"stop_loss_mode": "off"})
    wire_positions(monkeypatch, api, [position(current_size=100, avg_price=0.01)])
    api.get_trades.return_value = [maker_buy()]
    api.get_orderbook.return_value = _ob([(0.30, 500)], [(0.35, 500)])
    monitor.begin_status_tick()
    monitor.check_exit()
    api.place_limit_sell.assert_called_once_with("123", 0.40, 100.0, tick_size="0.01")
    api.place_market_sell.assert_not_called()
    assert monitor.last_position_count == 1
    assert all(c.args[0].market == "0xmarket" for c in api.get_trades.call_args_list)
    db.set_cooldown.assert_called_with("0xABC", "0xmarket", 20)


def test_v2_position_size_mismatch_keeps_unknown_cost_protection(monkeypatch):
    monitor, api, db = _make_monitor({"stop_loss_mode": "off"})
    wire_positions(monkeypatch, api, [position(current_size=100, avg_price=0.01)])
    api.get_trades.return_value = [maker_buy(size=20)]
    api.get_orderbook.return_value = _ob([(0.30, 500)], [(0.35, 500)])
    monitor.begin_status_tick()
    monitor.check_exit()
    api.place_limit_sell.assert_not_called()
    api.place_market_sell.assert_not_called()
    assert any("裸奔" in r.get("action", "") for r in monitor._status_rows)


def test_v2_activity_flows_into_existing_daily_ledger(monkeypatch):
    source = data_api()
    source.get_trades = MagicMock(return_value=[])
    db = MagicMock()
    monkeypatch.setattr(
        "api.polymarket_api.http_get",
        MagicMock(
            side_effect=[
                response(
                    [{"type": "REWARD", "timestamp": 1793667600, "usdc_size": 12.5}],
                    "next",
                ),
                response(
                    [
                        {
                            "type": "MAKER_REBATE",
                            "timestamp": 1793667600,
                            "usdc_size": 0.75,
                        }
                    ]
                ),
            ]
        ),
    )
    rebuild_wallet_pnl(source, db, "0xFUND", "2026-11-02", "2026-11-03")
    days = {c.kwargs["date"]: c.kwargs for c in db.upsert_daily_pnl.call_args_list}
    assert days["2026-11-02"]["reward"] == 12.5
    assert days["2026-11-03"]["rebate"] == 0.75


@pytest.fixture
def isolated_web(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "migration-smoke.db"))
    db.init()
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    monkeypatch.setattr(routes, "encryption_key", None)
    monkeypatch.setitem(routes.app.config, "TESTING", True)
    source = data_api()
    monkeypatch.setattr(routes, "_wallet_apis", lambda only=None: {"0xFUND": source})
    monkeypatch.setattr(routes, "_enrich_rows", lambda rows, key: None)
    monkeypatch.setattr(
        "api.polymarket_api.http_get",
        MagicMock(
            return_value=response(
                [
                    position(current_size=100, current_price=0.4, avg_price=0.3),
                ]
            )
        ),
    )
    with routes.app.test_client() as client:
        yield client, db
    db.close()


def test_web_position_api_displays_real_v2_adapter_output(isolated_web):
    client, db = isolated_web
    with client.session_transaction() as session:
        session["logged_in"] = True
    result = client.get("/api/positions")
    assert result.status_code == 200
    row = result.get_json()[0]
    assert row["condition_id"] == "0xmarket"
    assert row["market_name"] == "测试市场"
    assert row["size"] == 100 and row["current_price"] == 0.4
    assert row["buy_price"] == 0.3  # 均价仅显示用途；离场用例另验真实成交成本


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/markets",
        "/config",
        "/orders",
        "/history",
        "/logs",
        "/blacklist",
        "/help",
        "/pnl",
        "/api/settings",
        "/api/categories",
        "/api/templates",
        "/api/eligible",
        "/api/pnl?wallet=all",
    ],
)
def test_web_pages_and_read_apis_render_with_isolated_database(isolated_web, path):
    client, db = isolated_web
    with client.session_transaction() as session:
        session["logged_in"] = True
    assert client.get(path).status_code == 200
    assert routes.manager is None  # 页面读取不自动启动引擎


def test_unauthenticated_reads_still_require_login(isolated_web):
    client, db = isolated_web
    assert client.get("/api/positions").status_code == 302
    assert client.get("/").status_code == 302
