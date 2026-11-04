"""初筛翻页必须有进度、支持让位；不连接真实接口或钱包。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.polymarket_api import PolymarketAPI
from engine.scanner import MarketScanner, ScanSuperseded


def response(rows, cursor="LTE="):
    r = MagicMock()
    r.json.return_value = {"data": rows, "next_cursor": cursor}
    return r


def test_rewards_reports_each_page_without_changing_results(monkeypatch):
    get = MagicMock(
        side_effect=[response([{"condition_id": "a"}], "next"), response([])]
    )
    monkeypatch.setattr("api.polymarket_api.http_get", get)
    progress = []
    rows = PolymarketAPI.get_rewards_markets(
        max_pages=2, on_page=lambda page, count: progress.append((page, count))
    )
    assert rows == [{"condition_id": "a"}]
    assert progress == [(1, 1), (2, 1)]


def test_rewards_cancel_at_page_boundary_never_requests_next_page(monkeypatch):
    get = MagicMock(return_value=response([{"condition_id": "a"}], "next"))
    monkeypatch.setattr("api.polymarket_api.http_get", get)
    completed = []

    def check():
        if completed:
            raise ScanSuperseded()

    with pytest.raises(ScanSuperseded):
        PolymarketAPI.get_rewards_markets(
            max_pages=20,
            on_page=lambda *args: completed.append(args),
            check_cancel=check,
        )
    assert get.call_count == 1


def test_rewards_cancellation_before_retry_is_not_swallowed(monkeypatch):
    get = MagicMock(side_effect=RuntimeError("timeout"))
    monkeypatch.setattr("api.polymarket_api.http_get", get)
    monkeypatch.setattr("api.polymarket_api.time.sleep", lambda _: None)

    def check():
        if get.call_count:
            raise ScanSuperseded()

    with pytest.raises(ScanSuperseded):
        PolymarketAPI.get_rewards_markets(check_cancel=check)
    assert get.call_count == 1


def test_discovery_reports_category_and_full_pages_before_any_candidate():
    messages = []
    found = []

    def rewards(tag_slug=None, on_page=None, check_cancel=None, **kwargs):
        # 第一次网络读取前即要有阶段提示，不能卡在 Starting。
        assert messages and not found
        check_cancel()
        on_page(1, 100)
        assert any("第 1 页" in msg and "100" in msg for msg in messages)
        return []

    api = MagicMock()
    api.get_rewards_markets.side_effect = rewards
    db = MagicMock()
    db.get_blacklist_ids.return_value = set()
    scanner = MarketScanner(api, db, "")
    scanner.discover_candidates(
        [{"included_categories": ["weather"], "include_other": True}],
        on_progress=lambda checked, total, msg: messages.append(msg),
        on_found=found.append,
    )
    assert any("天气" in msg for msg in messages)
    assert any("全品类" in msg for msg in messages)


def test_discovery_does_not_treat_cancelled_category_as_empty():
    api = MagicMock()

    def rewards(check_cancel=None, **kwargs):
        check_cancel()
        return []

    api.get_rewards_markets.side_effect = rewards
    scanner = MarketScanner(api, MagicMock(), "")
    with pytest.raises(ScanSuperseded):
        scanner.discover_candidates(
            [{"included_categories": ["weather"], "include_other": False}],
            cancel=lambda: True,
        )


def test_scan_status_exposes_current_phase(tmp_path, monkeypatch):
    import web.routes as routes
    from models.database import Database

    db = Database(str(tmp_path / "test.db"))
    db.init()
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(
        routes,
        "manager",
        SimpleNamespace(
            scan_status="scanning",
            scan_checked=0,
            scan_total=0,
            scan_progress="抓取品类：天气，第 4 页，已读取 400 条",
            eligible_markets=[],
            last_scan_time=0,
        ),
    )
    monkeypatch.setitem(routes.app.config, "TESTING", True)
    try:
        with routes.app.test_client() as client:
            with client.session_transaction() as session:
                session["logged_in"] = True
            data = client.get("/api/engine/scan-status").get_json()
            assert data["scan_progress"] == routes.manager.scan_progress
    finally:
        db.close()
