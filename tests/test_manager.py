"""tests/test_manager.py"""

import time
import pytest
from unittest.mock import MagicMock, patch
from engine.manager import EngineManager, WalletWorker


class TestRunLoopResilience:
    """F4: 监控主循环必须隔离单次 tick 故障——任一未捕获异常都不能杀死该钱包的
    监控线程(否则该钱包从此不再检测成交/不再止损,且不会自动重启)。"""

    def test_run_survives_tick_exception_and_continues(self):
        api, db = MagicMock(), MagicMock()
        worker = WalletWorker(api, db, "0xW", {"fill_check_interval_sec": 5})
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            worker._stop_event.set()  # 让循环这轮之后退出
            raise RuntimeError("tick blew up")

        worker._tick = boom
        worker._run()  # 必须正常返回,不能把异常抛出来杀死线程
        assert calls["n"] == 1


def _make_manager():
    db = MagicMock()
    db.get_settings.return_value = {
        "scan_interval_sec": 30,
        "fill_check_interval_sec": 5,
        "cooldown_minutes": 20,
        "discovery_interval_sec": 14400,
        "reward_scan_max_pages": 20,
        "min_reward_usd": 100.0,
        "max_spread_cents": 3.0,
        "min_price_cents": 10.0,
        "max_price_cents": 50.0,
        "min_settlement_days": 4,
    }
    db.list_wallets.return_value = [
        {"address": "0xABC", "encrypted_key": "enc1", "enabled": 1},
        {"address": "0xDEF", "encrypted_key": "enc2", "enabled": 1},
    ]
    db.get_open_buy_orders.return_value = []
    db.get_template_for.return_value = {
        "included_categories": ["politics"],
        "include_other": True,
        "min_reward_usd": 100.0,
        "max_buy_orders_per_wallet": 5,
        "order_size_mode": "min",
        "order_size_custom_usd": 0.0,
    }
    db.get_template.return_value = {
        "included_categories": ["politics"],
        "include_other": True,
        "min_reward_usd": 100.0,
    }
    db.get_default_template_id.return_value = 1
    manager = EngineManager(db, encryption_key=b"x" * 32)
    return manager, db


class TestEngineLifecycle:
    def test_start_creates_threads(self):
        manager, db = _make_manager()
        with patch("engine.manager.decrypt", return_value="0x_fake_key"):
            with patch("engine.manager.PolymarketAPI"):
                manager.start_all()
                assert len(manager.engines) == 2
                manager.stop_all()

    def test_stop_cancels_buy_orders(self):
        manager, db = _make_manager()
        mock_api = MagicMock()
        mock_api.get_open_orders.return_value = [
            {"id": "o1", "side": "BUY"},
            {"id": "o2", "side": "SELL"},
        ]
        with patch("engine.manager.decrypt", return_value="0x_fake_key"):
            with patch("engine.manager.PolymarketAPI", return_value=mock_api):
                manager.start_all()
                # Reset mocks after start_all so the assertion only counts cancels from stop()
                mock_api.cancel_orders.reset_mock()
                manager.stop_all()
                # Batched cancel: cancel_orders called once per wallet (2 wallets),
                # each call with only the BUY order ids
                assert mock_api.cancel_orders.call_count == 2
                assert all(
                    c.args == (["o1"],) for c in mock_api.cancel_orders.call_args_list
                )

    def test_get_status(self):
        manager, db = _make_manager()
        status = manager.get_status()
        assert isinstance(status, dict)
        assert "engines" in status

    def test_reset_discovery_cache_forces_rediscovery(self):
        # 清空内存候选池后,_should_discover 立刻为 True(即便刚扫过、离 4h 还远)。
        manager, db = _make_manager()
        manager.eligible_markets = [{"market_id": "stale"}]
        manager.last_scan_time = time.time()
        assert manager._should_discover(time.time()) is False  # 复用旧池:不发现
        manager._reset_discovery_cache()
        assert manager.eligible_markets == []
        assert manager._should_discover(time.time()) is True  # 清空后:强制发现

    def test_start_all_clears_stale_pool(self):
        # 「启动/重启引擎」须清空上一轮内存候选池,否则扫描线程因 _should_discover=False
        # 复用旧结果、市场发现一直不刷新(用户实报的「重启不刷新市场」)。
        manager, db = _make_manager()
        manager.eligible_markets = [{"market_id": "stale"}]
        manager.last_scan_time = time.time()
        with patch("engine.manager.decrypt", return_value="k"):
            with patch("engine.manager.PolymarketAPI"):
                with patch.object(manager, "_scanner_loop"):  # 不真跑循环,免它又填充池
                    manager.start_all()
                    assert manager.eligible_markets == []
                    manager.stop_all()


class TestWalletWorkerTick:
    def test_tick_runs_check_exit_between_fills_and_compliance(self):
        worker = WalletWorker(
            MagicMock(), MagicMock(), "0xABC", {"fill_check_interval_sec": 5}
        )
        worker.monitor = MagicMock()

        worker._tick()

        worker.monitor.begin_status_tick.assert_called_once()
        worker.monitor.check_buy_orders.assert_called_once()
        worker.monitor.check_exit.assert_called_once()
        worker.monitor.check_sell_orders.assert_called_once()
        worker.monitor.publish_status.assert_called_once()


class TestTestPlaceOrders:
    class _FakeScanner:
        """filter_for_template 原样返回候选池(测试里候选已是可下单形状)。

        eligible_markets 现为候选池;test_place_orders 会先 filter_for_template
        再 place_orders。这些测试不验证精筛逻辑本身(那在 test_scanner.py),
        故用假精筛原样透传,聚焦 test_place_orders 的钱包选择/下单/异常路径。
        """

        def __init__(self, api, db, addr):
            pass

        def filter_for_template(self, pool, tmpl, addr):
            return list(pool)

    def test_no_eligible_markets_returns_scan_hint(self):
        manager, db = _make_manager()
        manager.eligible_markets = []
        result = manager.test_place_orders()
        assert result == {"ok": False, "message": "请先扫描市场"}

    def test_no_enabled_wallet_returns_error(self):
        manager, db = _make_manager()
        db.list_wallets.return_value = [
            {"address": "0xABC", "encrypted_key": "e1", "enabled": 0},
        ]
        manager.eligible_markets = [{"market_competitiveness": 0.5}]
        result = manager.test_place_orders()
        assert result == {"ok": False, "message": "没有启用的钱包"}

    def test_no_running_worker_builds_transient_api_and_places(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager.eligible_markets = [{"market_competitiveness": 0.5, "name": "m"}]
        # engines empty -> first enabled wallet has no running worker;
        # must construct a transient API/worker just to place the orders.
        fake_worker = MagicMock()
        with patch("engine.manager.decrypt", return_value="0xkey"), patch(
            "engine.manager.PolymarketAPI"
        ) as mock_api_cls, patch(
            "engine.manager.WalletWorker", return_value=fake_worker
        ), patch(
            "engine.manager.MarketScanner", self._FakeScanner
        ):
            result = manager.test_place_orders()
        assert result["ok"] is True
        mock_api_cls.assert_called_once()
        fake_worker.place_orders.assert_called_once()
        _, kwargs = fake_worker.place_orders.call_args
        assert kwargs.get("limit") == 3
        # transient worker must NOT start a monitor thread
        fake_worker.start.assert_not_called()

    def test_places_on_first_enabled_running_worker_with_limit_3(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager.eligible_markets = [
            {"market_competitiveness": 0.9, "name": "high"},
            {"market_competitiveness": 0.1, "name": "low"},
        ]
        worker = MagicMock()
        worker.running = True
        # db.list_wallets()[0] is 0xABC (enabled) per _make_manager
        manager.engines = {"0xABC": worker}
        with patch("engine.manager.MarketScanner", self._FakeScanner):
            result = manager.test_place_orders()
        assert result["ok"] is True
        worker.place_orders.assert_called_once()
        args, kwargs = worker.place_orders.call_args
        passed_markets = args[0]
        # sorted ascending by competitiveness: low (0.1) before high (0.9)
        assert [m["name"] for m in passed_markets] == ["low", "high"]
        assert kwargs.get("limit") == 3

    def test_place_orders_exception_returns_error_dict(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager.eligible_markets = [{"market_competitiveness": 0.5}]
        worker = MagicMock()
        worker.running = True
        worker.place_orders.side_effect = RuntimeError("boom")
        manager.engines = {"0xABC": worker}
        with patch("engine.manager.MarketScanner", self._FakeScanner):
            result = manager.test_place_orders()
        assert result["ok"] is False
        assert "boom" in result["message"]

    def test_skips_disabled_or_not_running_picks_first_valid(self):
        manager, db = _make_manager()
        db.list_wallets.return_value = [
            {"address": "0xABC", "encrypted_key": "e1", "enabled": 0},
            {"address": "0xDEF", "encrypted_key": "e2", "enabled": 1},
        ]
        manager.eligible_markets = [{"market_competitiveness": 0.5, "name": "m"}]
        stopped = MagicMock()
        stopped.running = False
        good = MagicMock()
        good.running = True
        manager.engines = {"0xABC": stopped, "0xDEF": good}
        with patch("engine.manager.MarketScanner", self._FakeScanner):
            result = manager.test_place_orders()
        assert result["ok"] is True
        good.place_orders.assert_called_once()
        stopped.place_orders.assert_not_called()

    def test_filters_candidate_pool_per_wallet_before_placing(self):
        # 回归:eligible_markets 是候选池(按市场、无 token_id);test_place_orders
        # 必须先 filter_for_template 精筛成逐 token eligible,否则 place_orders
        # 取 token_id 会 KeyError(此前直接把候选池喂给 place_orders)。
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager.eligible_markets = [{"condition_id": "A", "tokens": [], "tags": []}]
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xABC": worker}

        filtered = [{"market_id": "A", "token_id": "A-y", "market_competitiveness": 0}]

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def filter_for_template(self, pool, tmpl, addr):
                return list(filtered)

        with patch("engine.manager.MarketScanner", FakeScanner):
            res = manager.test_place_orders()

        assert res["ok"] is True
        worker.place_orders.assert_called_once()
        passed = worker.place_orders.call_args[0][0]
        assert passed == filtered  # 收到的是精筛后的逐 token eligible,而非候选池
        assert worker.place_orders.call_args[1].get("limit") == 3


class TestScanMarketsLastScanTime:
    def test_last_scan_time_only_updates_at_round_completion(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        observed = []

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, **kw
            ):
                on_found({"market_id": "m1"})
                observed.append(manager.last_scan_time)
                on_found({"market_id": "m2"})
                observed.append(manager.last_scan_time)
                return [{"market_id": "m1"}, {"market_id": "m2"}]

            def filter_for_template(self, pool, tmpl, addr):
                return pool

        assert manager.last_scan_time == 0
        with patch("engine.manager.MarketScanner", FakeScanner):
            manager.scan_markets()
        assert observed == [0, 0]
        assert manager.last_scan_time > 0
        assert manager.scan_status == "done"
        assert manager.eligible_markets == [{"market_id": "m1"}, {"market_id": "m2"}]


class TestSharedScanWithStatus:
    def test_manual_scan_sets_scanning_then_done(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        seen = []

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, **kw
            ):
                on_progress(1, 2, "checking")
                seen.append(manager.scan_status)
                on_found({"market_id": "m1"})
                return [{"market_id": "m1"}]

            def filter_for_template(self, pool, tmpl, addr):
                return pool

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager.scan_markets()
        assert seen == ["scanning"]
        assert manager.scan_status == "done"
        assert manager.last_scan_time > 0
        assert manager.eligible_markets == [{"market_id": "m1"}]
        db.save_eligible_markets.assert_called_once_with([{"market_id": "m1"}])

    def test_place_round_filters_per_wallet_and_places(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xABC": worker}
        manager.eligible_markets = [{"market_id": "m9", "tags": []}]

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def refresh_orderbooks(self, pool):
                pass

            def filter_for_template(self, pool, tmpl, addr):
                return pool

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager._place_round()
        worker.place_orders.assert_called_once_with(
            [{"market_id": "m9", "tags": []}], cancel_dropouts=True
        )

    def test_place_round_empty_pool_skips_placement(self):
        # 空候选池 -> 不下单,避免 cancel_dropouts 误撤全部买单。
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xABC": worker}
        manager.eligible_markets = []
        manager._place_round()
        worker.place_orders.assert_not_called()

    def test_place_round_distributes_sorted_by_competitiveness(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xABC": worker}
        manager.eligible_markets = [
            {"market_id": "hi", "market_competitiveness": 0.9},
            {"market_id": "lo", "market_competitiveness": 0.1},
            {"market_id": "mid", "market_competitiveness": 0.5},
        ]

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def refresh_orderbooks(self, pool):
                pass

            def filter_for_template(self, pool, tmpl, addr):
                return list(pool)

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager._place_round()
        distributed = worker.place_orders.call_args[0][0]
        assert [m["market_id"] for m in distributed] == ["lo", "mid", "hi"]

    def test_should_discover_empty_pool_true(self):
        manager, db = _make_manager()
        manager.eligible_markets = []
        manager.last_scan_time = 1000.0
        assert manager._should_discover(1000.0) is True

    def test_should_discover_recent_false(self):
        manager, db = _make_manager()
        manager.eligible_markets = [{"market_id": "m1"}]
        manager.last_scan_time = 1000.0
        assert manager._should_discover(1030.0) is False

    def test_should_discover_stale_true(self):
        manager, db = _make_manager()
        manager.eligible_markets = [{"market_id": "m1"}]
        manager.last_scan_time = 1000.0
        assert manager._should_discover(1000.0 + 14401) is True

    def test_discover_skips_orderbook(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        captured = {}

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, **kw
            ):
                captured.update(kw)
                return [{"market_id": "m1"}]

            def filter_for_template(self, pool, tmpl, addr):
                return pool

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager._discover()
        assert captured.get("skip_orderbook") is True
        assert manager.eligible_markets == [{"market_id": "m1"}]
        assert manager.last_scan_time > 0

    def test_should_discover_at_interval_boundary_true(self):
        manager, db = _make_manager()
        manager.eligible_markets = [{"market_id": "m1"}]
        manager.last_scan_time = 1000.0
        # 恰好等于间隔 -> >= -> 该发现
        assert manager._should_discover(1000.0 + 14400) is True

    def test_discovery_fail_then_place_uses_prev_pool(self):
        # 发现失败保留上一份缓存池,本轮 _place_round 仍用它刷簿下单。
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xABC": worker}
        manager.eligible_markets = [{"market_id": "prev", "tags": []}]
        manager.last_scan_time = 1000.0
        refreshed = {}

        class FlakyScanner:
            def __init__(self, api, db, addr):
                pass

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, **kw
            ):
                raise RuntimeError("discovery down")

            def refresh_orderbooks(self, pool):
                refreshed["pool"] = pool

            def filter_for_template(self, pool, tmpl, addr):
                return pool

        with patch("engine.manager.MarketScanner", FlakyScanner):
            try:
                manager._discover()  # raises; _scan_with_status 恢复 prev_eligible
            except RuntimeError:
                pass
            manager._place_round()
        assert manager.eligible_markets == [{"market_id": "prev", "tags": []}]
        assert refreshed["pool"] == [{"market_id": "prev", "tags": []}]
        worker.place_orders.assert_called_once_with(
            [{"market_id": "prev", "tags": []}], cancel_dropouts=True
        )

    def test_scan_failure_resets_status_and_keeps_last_scan_time(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager.last_scan_time = 12345.0
        manager.eligible_markets = [{"market_id": "prev"}]

        class BoomScanner:
            def __init__(self, api, db, addr):
                pass

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, **kw
            ):
                raise RuntimeError("scanner blew up")

        with patch("engine.manager.MarketScanner", BoomScanner):
            with pytest.raises(RuntimeError):
                manager._scan_with_status()
        assert manager.scan_status == "done"
        assert manager.last_scan_time == 12345.0
        assert manager.eligible_markets == [{"market_id": "prev"}]


class TestCategoryCatalog:
    """品类计数快照:本地持久化 + 秒显、扫描/手动刷新更新,普通打开绝不联网。"""

    def _mgr(self, stored):
        db = MagicMock()
        db.get_category_catalog.return_value = stored
        return EngineManager(db, encryption_key=b"x" * 32), db

    def test_no_snapshot_returns_static_without_network(self):
        from config import CATEGORY_CATALOG

        mgr, db = self._mgr(None)
        with patch("engine.manager.MarketScanner") as MS:
            out = mgr.category_catalog()
            MS.assert_not_called()  # 普通打开绝不联网
        assert out["ready"] is False
        assert {c["slug"] for c in out["categories"]} == {
            c["slug"] for c in CATEGORY_CATALOG
        }

    def test_stored_snapshot_served_without_network(self):
        snap = {
            "ready": True,
            "categories": [{"slug": "x", "label": "X", "count": 2}],
            "other_count": 1,
            "updated_at": 5.0,
        }
        mgr, db = self._mgr(snap)
        with patch("engine.manager.MarketScanner") as MS:
            out = mgr.category_catalog()
            MS.assert_not_called()
        assert out == snap

    def test_persist_catalog_caches_and_saves(self):
        mgr, db = self._mgr(None)
        out = mgr._persist_catalog(
            {"categories": [{"slug": "x", "label": "X", "count": 1}], "other_count": 2}
        )
        assert out["ready"] is True and "updated_at" in out and out["other_count"] == 2
        db.save_category_catalog.assert_called_once_with(out)
        # 之后普通打开命中内存缓存,不读库也不联网
        db.get_category_catalog.reset_mock()
        with patch("engine.manager.MarketScanner") as MS:
            assert mgr.category_catalog() is out
            MS.assert_not_called()
        db.get_category_catalog.assert_not_called()

    def test_scan_persists_last_catalog(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        snap = {
            "categories": [{"slug": "x", "label": "X", "count": 3}],
            "other_count": 0,
        }

        class OkScanner:
            def __init__(self, api, db, addr):
                self.last_catalog = snap

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, **kw
            ):
                return [{"market_id": "m1"}]

        with patch("engine.manager.MarketScanner", OkScanner):
            manager._scan_with_status()
        db.save_category_catalog.assert_called_once()
        saved = db.save_category_catalog.call_args.args[0]
        assert saved["ready"] is True
        assert saved["categories"] == snap["categories"]


class TestScanGuard:
    """手动扫描是后台线程;守卫不重复开、并同步置位 scan_status 供前端立即可见。"""

    def test_start_scan_async_starts_when_idle(self):
        manager, db = _make_manager()
        manager.scan_status = "idle"
        with patch("engine.manager.threading.Thread") as T:
            assert manager.start_scan_async() is True
            assert manager.scan_status == "scanning"  # 同步置位
            T.assert_called_once()
            assert T.call_args.kwargs.get("target") == manager._run_scan
        T.return_value.start.assert_called_once()

    def test_start_scan_async_guards_double_scan(self):
        manager, db = _make_manager()
        manager.scan_status = "scanning"
        with patch("engine.manager.threading.Thread") as T:
            assert manager.start_scan_async() is False
        T.assert_not_called()  # 已在扫描,不再开第二个线程

    def test_run_scan_resets_stuck_status(self):
        # 无钱包时 scan_markets 在 _ensure_scanner_api 就早退,不进 _scan_with_status;
        # _run_scan 的 finally 必须把 scan_status 从 scanning 收敛掉,别永远卡住。
        manager, db = _make_manager()
        manager.scan_status = "scanning"
        gen = manager._scan_generation
        with patch.object(manager, "scan_markets"):  # no-op,不触碰 scan_status
            manager._run_scan(gen)
        assert manager.scan_status == "done"

    def test_start_scan_async_force_supersedes(self):
        manager, db = _make_manager()
        manager.scan_status = "scanning"
        manager._scan_generation = 5
        with patch("engine.manager.threading.Thread") as T:
            assert (
                manager.start_scan_async(force=True) is True
            )  # force 无视「已在扫描」
            assert manager._scan_generation == 6  # 代际 +1,旧扫描据此让位
            assert manager.scan_status == "scanning"
            assert T.call_args.kwargs.get("args") == (6,)  # 新线程带新代际
        T.return_value.start.assert_called_once()

    def test_scan_with_status_discards_when_superseded(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager._scan_generation = 2
        manager.eligible_markets = [{"market_id": "keep"}]

        class FakeScanner:
            def __init__(self, api, db, addr):
                self.last_catalog = None

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, cancel=None, **kw
            ):
                return [{"market_id": "new"}]

        with patch("engine.manager.MarketScanner", FakeScanner):
            out = manager._scan_with_status(gen=1)  # gen 1 != 当前代 2 -> 非当前代
        assert out == [{"market_id": "new"}]  # 结果仍返回上层
        assert manager.eligible_markets == [{"market_id": "keep"}]  # 但共享状态没被覆盖
        db.save_category_catalog.assert_not_called()

    def test_scan_markets_skips_save_when_superseded(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager._scan_generation = 2

        class FakeScanner:
            def __init__(self, api, db, addr):
                self.last_catalog = None

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, cancel=None, **kw
            ):
                return [{"market_id": "new"}]

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager.scan_markets(gen=1)  # gen 1 != 当前代 2
        db.save_eligible_markets.assert_not_called()  # 被接管:不落库(否则覆盖成废结果)

    def test_scan_threads_reward_scan_max_pages_from_settings(self):
        from config import ENGINE_DEFAULTS

        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        settings = dict(ENGINE_DEFAULTS)
        settings["reward_scan_max_pages"] = 42
        db.get_settings.return_value = settings
        seen = {}

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def fetch_candidates(
                self, templates, on_progress=None, on_found=None, **kw
            ):
                seen["max_pages"] = kw.get("max_pages")
                return []

            def filter_for_template(self, pool, tmpl, addr):
                return pool

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager.scan_markets()
        assert seen["max_pages"] == 42
