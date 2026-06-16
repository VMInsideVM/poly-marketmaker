"""engine/manager.py — Multi-wallet engine manager.

Architecture:
- One shared scanner thread: scans markets once, produces eligible list
- Per-wallet threads: each wallet uses the shared eligible list to place orders,
  then monitors fills/stop-loss independently
"""

import logging
import threading
import time
from api.polymarket_api import PolymarketAPI
from engine.scanner import MarketScanner
from engine.monitor import OrderMonitor
from engine.positions import held_side_info
from utils.crypto import decrypt

logger = logging.getLogger(__name__)


class WalletWorker:
    """Per-wallet worker: places orders from shared eligible list, monitors fills."""

    def __init__(self, api: PolymarketAPI, db, wallet_address: str, settings: dict):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address
        self.settings = settings
        self.monitor = OrderMonitor(api, db, wallet_address)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.running = True
        logger.info("Wallet worker started for %s", self.wallet_address)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=30)
        self.running = False
        self._cancel_buy_orders()
        logger.info("Wallet worker stopped for %s", self.wallet_address)

    def _cancel_buy_orders(self):
        """Cancel all open buy orders on the exchange in one batched request."""
        try:
            open_orders = self.api.get_open_orders()
            buy_ids = [o["id"] for o in open_orders if o.get("side") == "BUY"]
            if buy_ids:
                self.api.cancel_orders(buy_ids)
                logger.info(
                    "Cancelled %d buy orders for %s", len(buy_ids), self.wallet_address
                )
        except Exception as e:
            logger.error(
                "Error cancelling buy orders for %s: %s", self.wallet_address, e
            )

    def _run(self):
        """Monitor loop: run one tick at each check_interval.

        Each tick is isolated: an unhandled exception in any step is logged and
        the loop continues. Without this guard a single transient error killed
        the wallet's monitor thread for good — no fill detection, no stop-loss,
        no restart, the only symptom a frozen monitor-status table (audit F4).
        """
        check_interval = self.settings["fill_check_interval_sec"]
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.exception(
                    "Monitor tick crashed for %s (continuing): %s",
                    self.wallet_address,
                    e,
                )
            self._stop_event.wait(timeout=check_interval)

    def _tick(self):
        """One monitor pass: detect fills, run three-tier exit, strategy
        compliance. check_exit runs right after fill detection so the exit
        decision reflects the latest fills."""
        self.monitor.begin_status_tick()
        self.monitor.check_buy_orders()
        self.monitor.check_exit()
        self.monitor.check_sell_orders()
        self.monitor.publish_status()

    def place_orders(
        self,
        eligible_markets: list[dict],
        limit: int | None = None,
        cancel_dropouts: bool = False,
    ):
        """多档挂单:按市场分组,下单时算 K 档/边,整市场敞口共享,§8 <10¢ 双边。

        eligible_markets = filter_for_template 的轻量 per-token 条目。
        limit 设置时,达到该数量的成功下单后停止。
        """
        from engine.laddering import (
            compute_market_ladders,
            apply_double_sided_floor,
            reconcile_buy_orders,
        )
        from engine.strategy import reward_price_range

        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed for %s: %s", self.wallet_address, e)
            return
        try:
            positions = self.api.get_user_positions(self.api.get_funder())
        except Exception as e:
            logger.error(
                "get_user_positions failed for %s, skip placement: %s",
                self.wallet_address,
                e,
            )
            return
        held_assets, held_value, held_shares = held_side_info(positions)
        blacklist = self.db.get_blacklist_ids()
        tmpl = self.db.get_template_for(self.wallet_address)
        tier_rules = tmpl.get("tier_rules") or []
        if not tier_rules:
            # 模板没配档位规则表 -> 一单都不会挂。显式告警,避免"引擎在跑却
            # 静默不下单"被误认为"没机会",便于排查模板配置问题。
            logger.warning(
                "place_orders skipped for %s: empty tier_rules (模板未配档位规则表)",
                self.wallet_address,
            )
            return
        # max_exposure_usd 是「单市场」敞口上限(YES+NO 合计);跨市场不设全局
        # 美元锁(maker 买单不锁仓,一笔余额垫付所有挂单),总量由 max_concurrent
        # _markets × 单市场敞口 约束。
        max_exposure_usd = float(tmpl.get("max_exposure_usd", 250))
        max_exposure_shares = int(tmpl.get("max_exposure_shares", 500))
        max_concurrent = int(tmpl.get("max_concurrent_markets", 10))
        min_price_double_cents = float(tmpl.get("min_price_double_cents", 10))

        buy_orders = [o for o in open_orders if o.get("side") == "BUY"]
        buys_by_token, markets_with_open = {}, set()
        for o in buy_orders:
            buys_by_token.setdefault(o.get("asset_id", ""), []).append(o)
            mkt = o.get("market", "")
            if mkt:
                markets_with_open.add(mkt)

        grouped, order = {}, []
        for e in eligible_markets:
            mid = e["market_id"]
            if mid not in grouped:
                grouped[mid] = []
                order.append(mid)
            grouped[mid].append(e)

        # SP5a-1 跨出 eligible 整仓撒买单：有在挂买单、不在本轮 eligible、且不在
        # 冷却的市场 -> 撒掉该市场全部 BUY（持仓/卖单不动，仓由 check_exit 卖出）。
        # 只在真正下单轮(_do_scan / place_all_orders)开启；冷却市场只是「暂不挂新单」
        # 故豆免，避免撒掉正赚奖励的旧买单、并不与 SP5b「另一侧照常运行」冲突。
        if cancel_dropouts:
            eligible_mids = set(grouped.keys())
            dropped = {
                o.get("market", "")
                for o in buy_orders
                if o.get("market")
                and o.get("market") not in eligible_mids
                and not self.db.is_in_cooldown(self.wallet_address, o.get("market"))
            }
            drop_ids = [
                o["id"]
                for o in buy_orders
                if o.get("market") in dropped and o.get("id")
            ]
            if drop_ids:
                try:
                    self.api.cancel_orders(drop_ids)
                    markets_with_open -= dropped
                    for mkt in dropped:
                        self.db.record_action(
                            wallet=self.wallet_address,
                            market_id=mkt,
                            action_type="dropout_cancel",
                            side="-",
                            price=-1,
                            size=0,
                            reason="市场跨出 eligible（不再满足筛选门槛），撒掉该市场全部买单；持仓仓由离场卖出",
                            price_basis="跨出 eligible；来源:CLOB get_open_orders + filter_for_template",
                        )
                except Exception as ex:
                    logger.warning("Dropout cancel failed: %s", ex)

        placed = 0
        for mid in order:
            if mid in blacklist:
                continue
            if self.db.is_in_cooldown(self.wallet_address, mid):
                continue
            if (
                mid not in markets_with_open
                and len(markets_with_open) >= max_concurrent
            ):
                continue

            sides = []
            for e in grouped[mid]:
                token_id = e["token_id"]
                try:
                    ob = self.api.get_orderbook(token_id)
                except Exception as ex:
                    logger.warning(
                        "Orderbook failed for %s: %s", e.get("market_name", ""), ex
                    )
                    continue
                bids = sorted(
                    ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True
                )
                asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
                if not bids or not asks:
                    continue
                best_bid, best_ask = float(bids[0]["price"]), float(asks[0]["price"])
                midpoint = (best_bid + best_ask) / 2
                max_spread = float(e.get("rewards_max_spread", 2))
                rmin, rmax = reward_price_range(midpoint, max_spread)
                sides.append(
                    {
                        "token_id": token_id,
                        "outcome": e.get("outcome", ""),
                        "neg_risk": e.get("neg_risk", False),
                        "tick_size_str": ob.get("tick_size", "0.01"),
                        "min_size": int(e.get("rewards_min_size", 0) or 0),
                        "bids": bids,
                        "reward_range_min": rmin,
                        "reward_range_max": rmax,
                        "max_spread": max_spread,
                    }
                )
            if not sides:
                continue

            side_a = sides[0]
            side_b = sides[1] if len(sides) > 1 else None
            balance = self.api.get_balance()
            budget = max(0.0, min(balance, max_exposure_usd) - held_value.get(mid, 0.0))
            shares_budget = max(0, max_exposure_shares - int(held_shares.get(mid, 0.0)))
            budget_ok = budget > 0 and shares_budget > 0
            ladders = {"a": [], "b": []}
            if budget_ok:
                ca = None if side_a["token_id"] in held_assets else side_a
                cb = None if (side_b and side_b["token_id"] in held_assets) else side_b
                ladders = compute_market_ladders(
                    ca, cb, tier_rules, budget, shares_budget
                )
                ladders = apply_double_sided_floor(ladders, min_price_double_cents)

            for key, side in (("a", side_a), ("b", side_b)):
                if side is None:
                    continue
                token_id = side["token_id"]
                resting = buys_by_token.get(token_id, [])
                if token_id in held_assets:
                    # 成交后单侧暂停:撤光该侧全部在挂买单、不挂新单(SP5b Q1)
                    cancel_ids, to_place = reconcile_buy_orders([], resting)
                    cancel_reason = "成交后单侧暂停:撤掉该侧全部买单,直至该侧持仓平掉"
                    cancel_action = "side_pause_cancel"
                elif budget_ok:
                    cancel_ids, to_place = reconcile_buy_orders(
                        ladders.get(key, []), resting
                    )
                    cancel_reason = "撤改收敛:撤掉价漂移/量不符的旧买单(目标多档梯已变)"
                    cancel_action = "buy_reconcile_cancel"
                else:
                    # 预算不足(扣减后):活跃侧保持不动
                    continue
                if cancel_ids:
                    try:
                        self.api.cancel_orders(cancel_ids)
                        self.db.record_action(
                            wallet=self.wallet_address,
                            market_id=mid,
                            action_type=cancel_action,
                            side="-",
                            price=-1,
                            size=0,
                            reason=cancel_reason,
                            price_basis=f"撤 {len(cancel_ids)} 笔 BUY；来源：CLOB get_open_orders",
                        )
                    except Exception as ex:
                        logger.warning(
                            "Reconcile/pause cancel %s failed: %s", token_id, ex
                        )
                for price, shares in to_place:
                    try:
                        self.api.place_limit_buy(
                            token_id,
                            price,
                            shares,
                            tick_size=side["tick_size_str"],
                            neg_risk=side["neg_risk"],
                        )
                        placed += 1
                        markets_with_open.add(mid)
                        self._record_place_buy_tier(mid, side, price, shares)
                        if limit is not None and placed >= limit:
                            return
                    except Exception as ex:
                        logger.error("place_limit_buy failed %s: %s", token_id, ex)

    def _record_place_buy_tier(self, market_id, side, price, shares):
        """记一档买单到 actions(不抛异常)。"""
        try:
            self.db.record_action(
                wallet=self.wallet_address,
                market_id=market_id,
                action_type="place_buy",
                side="买入",
                price=price,
                size=shares,
                reason="多档:在奖励区间内按累加厚度规则表挂买单",
                price_basis=(
                    f"档价 {price:.4f}（{side.get('outcome','')}）；"
                    f"奖励区间[{side['reward_range_min']:.4f},{side['reward_range_max']:.4f}]；"
                    f"来源：CLOB get_orderbook"
                ),
            )
        except Exception as e:
            logger.warning("record_action(place_buy) failed: %s", e)


class EngineManager:
    """Manages the shared scanner and per-wallet workers.

    Architecture:
    - One scanner thread runs periodically, producing an eligible markets list
    - Per-wallet workers use that list to place orders
    - Each wallet also has its own monitor thread for fills/stop-loss
    """

    def __init__(self, db, encryption_key: bytes):
        self.db = db
        self.encryption_key = encryption_key
        self.engines: dict[str, WalletWorker] = {}
        self._scanner_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._scanner_api: PolymarketAPI | None = None  # Shared API for scanning
        self.eligible_markets: list[dict] = []  # Latest scan results
        self.last_scan_time: float = 0
        self.scan_status: str = "idle"  # idle, scanning, done
        self.scan_progress: str = ""  # e.g. "Checking 5/120..."
        self.scan_total: int = 0
        self.scan_checked: int = 0

    # === Auto mode: full engine lifecycle ===

    def start_all(self):
        """Start everything: wallet workers + recovery + auto scanner loop."""
        wallets = self.db.list_wallets()
        for w in wallets:
            if w["enabled"]:
                self.start_wallet(w["address"], w["encrypted_key"])

        self.startup_recovery()

        # Start auto scanner loop
        self._stop_event.clear()
        self._scanner_thread = threading.Thread(target=self._scanner_loop, daemon=True)
        self._scanner_thread.start()
        logger.info("Engine started: %d wallets + auto scanner", len(self.engines))

    def stop_all(self):
        """Stop everything: scanner + all wallet workers, cancel all buy orders."""
        self._stop_event.set()
        if self._scanner_thread:
            self._scanner_thread.join(timeout=30)
            self._scanner_thread = None

        for address in list(self.engines.keys()):
            self.stop_wallet(address)
        logger.info("Engine stopped")

    def restart_all(self):
        """Restart with fresh settings."""
        self.stop_all()
        self.start_all()

    # === Manual mode: step-by-step controls ===

    def start_monitors(self):
        """Start wallet workers (monitor only, no auto scanning)."""
        wallets = self.db.list_wallets()
        for w in wallets:
            if w["enabled"]:
                self.start_wallet(w["address"], w["encrypted_key"])

        self.startup_recovery()
        logger.info("Started %d wallet monitors", len(self.engines))

    def cancel_all_buy_orders(self):
        """Cancel all buy orders across all wallets."""
        for address, worker in self.engines.items():
            if worker.running:
                try:
                    worker._cancel_buy_orders()
                except Exception as e:
                    logger.error("Error cancelling orders for %s: %s", address, e)
        logger.info("Cancelled all buy orders across all wallets")

    def scan_markets(self):
        """Run a single scan to produce the eligible markets list.

        Updates scan_status/scan_progress in real-time for frontend polling.
        """
        if not self._scanner_api:
            if self.engines:
                self._scanner_api = next(iter(self.engines.values())).api
            else:
                # No workers running, create API from first enabled wallet
                wallets = self.db.list_wallets()
                enabled = [w for w in wallets if w["enabled"]]
                if not enabled:
                    logger.error("No wallets available for scanning")
                    return
                pk = decrypt(enabled[0]["encrypted_key"], self.encryption_key)
                self._scanner_api = PolymarketAPI(
                    pk,
                    signature_type=enabled[0].get("signature_type", 2),
                    funder=enabled[0].get("funder") or None,
                )
                logger.info("Created scanner API from wallet %s", enabled[0]["address"])

        eligible = self._scan_with_status()

        # Persist to database (replace old data)
        self.db.save_eligible_markets(eligible)
        logger.info("Saved %d eligible markets to database", len(eligible))

    def place_all_orders(self):
        """每钱包按自己模板从候选池精筛后下单。"""
        if not self.eligible_markets:
            logger.warning("No candidate pool to place orders on")
            return
        for address, worker in self.engines.items():
            if not worker.running:
                continue
            try:
                tmpl = self.db.get_template_for(address)
                scanner = MarketScanner(self._scanner_api, self.db, "")
                eligible = scanner.filter_for_template(
                    self.eligible_markets, tmpl, address
                )
                eligible.sort(
                    key=lambda m: float(m.get("market_competitiveness", 0) or 0)
                )
                worker.place_orders(eligible, cancel_dropouts=True)
            except Exception as e:
                logger.error("Error placing orders for %s: %s", address, e)

    def test_place_orders(self) -> dict:
        """Place up to 3 strategy-compliant test buys on the first enabled
        wallet, iterating eligible markets until 3 succeed.

        Does not require the monitor to be running: if the first enabled
        wallet already has a running worker it is reused (so fills get
        monitored and written to history); otherwise a transient API/worker
        is constructed just to place the orders (no monitor thread)."""
        if not self.eligible_markets:
            return {"ok": False, "message": "请先扫描市场"}

        wallet = next((w for w in self.db.list_wallets() if w["enabled"]), None)
        if wallet is None:
            return {"ok": False, "message": "没有启用的钱包"}

        address = wallet["address"]
        existing = self.engines.get(address)
        if existing and existing.running:
            worker = existing
        else:
            try:
                private_key = decrypt(wallet["encrypted_key"], self.encryption_key)
                funder = wallet.get("funder", "")
                api = PolymarketAPI(
                    private_key,
                    signature_type=wallet.get("signature_type", 2),
                    funder=funder or None,
                )
                settings = self.db.get_settings()
                worker = WalletWorker(api, self.db, address, settings)
            except Exception as e:
                logger.error("Error building API for test orders: %s", e)
                return {"ok": False, "message": f"测试挂单失败：{e}"}

        # eligible_markets 现为候选池(按市场、无 token_id/价格);必须先按该钱包
        # 模板精筛成逐 token 的可下单 eligible,否则 place_orders 取 token_id 会 KeyError。
        tmpl = self.db.get_template_for(address)
        scanner = MarketScanner(self._scanner_api, self.db, "")
        sorted_markets = scanner.filter_for_template(
            self.eligible_markets, tmpl, address
        )
        sorted_markets.sort(
            key=lambda m: float(m.get("market_competitiveness", 0) or 0)
        )

        try:
            worker.place_orders(sorted_markets, limit=3)
        except Exception as e:
            logger.error("Error placing test orders: %s", e)
            return {"ok": False, "message": f"测试挂单失败：{e}"}
        return {
            "ok": True,
            "message": "已对符合策略的市场提交最多 3 个测试买单，请到订单管理查看",
        }

    def start_wallet(self, address: str, encrypted_key: str = None):
        if address in self.engines and self.engines[address].running:
            return

        wallets = self.db.list_wallets()
        wallet = next((w for w in wallets if w["address"] == address), None)
        if not wallet:
            return
        encrypted_key = wallet["encrypted_key"]
        funder = wallet.get("funder", "")

        private_key = decrypt(encrypted_key, self.encryption_key)
        api = PolymarketAPI(
            private_key,
            signature_type=wallet.get("signature_type", 2),
            funder=funder or None,
        )
        settings = self.db.get_settings()
        worker = WalletWorker(api, self.db, address, settings)
        self.engines[address] = worker
        worker.start()

        # Use first wallet's API for scanning (shared, avoids extra auth)
        if self._scanner_api is None:
            self._scanner_api = api

    def stop_wallet(self, address: str):
        worker = self.engines.pop(address, None)
        if worker:
            worker.stop()

    def _scanner_loop(self):
        """Shared scanner: runs once per scan_interval, feeds all wallets."""
        settings = self.db.get_settings()
        scan_interval = settings["scan_interval_sec"]

        while not self._stop_event.is_set():
            if self._scanner_api and self.engines:
                try:
                    self._do_scan()
                except Exception as e:
                    logger.error("Scanner error: %s", e)

            self._stop_event.wait(timeout=scan_interval)

    def _active_templates(self) -> list[dict]:
        """所有启用钱包绑定模板(按 excluded_categories 去重),供采集器算并集/交集。"""
        try:
            wallets = self.db.list_wallets()
        except Exception:
            wallets = []
        seen = {}
        for w in wallets:
            if not w.get("enabled"):
                continue
            tmpl = self.db.get_template_for(w["address"])
            # 去重键须含采集器实际用到的两个维度:品类排除集 + 奖励下限
            # (min_reward_usd 决定预筛 min_floor);只按品类去重会丢掉下限差异。
            key = (
                tuple(sorted(tmpl.get("excluded_categories", []) or [])),
                tmpl.get("min_reward_usd", 0),
            )
            seen[key] = tmpl
        if seen:
            return list(seen.values())
        return [self.db.get_template(self.db.get_default_template_id())]

    def _scan_with_status(self) -> list:
        """Run one scan, reporting scan_status/progress; shared by manual and
        auto paths. On success sets eligible_markets/last_scan_time and
        scan_status='done' and returns the eligible list. On failure resets
        scan_status to 'done' (never left 'scanning') WITHOUT touching
        last_scan_time (a failed round did not complete), then re-raises."""
        import time as _time

        prev_eligible = self.eligible_markets
        self.scan_status = "scanning"
        self.scan_progress = "Starting..."
        self.scan_checked = 0
        self.scan_total = 0
        self.eligible_markets = []

        def on_progress(checked, total, message):
            self.scan_checked = checked
            self.scan_total = total
            self.scan_progress = message

        def on_found(entry):
            self.eligible_markets.append(entry)

        try:
            scanner = MarketScanner(self._scanner_api, self.db, "")
            templates = self._active_templates()
            candidate_pool = scanner.fetch_candidates(
                templates, on_progress=on_progress, on_found=on_found
            )
        except Exception:
            self.eligible_markets = prev_eligible  # don't blank on failure
            self.scan_status = "done"  # not 'scanning': progress bar won't stick
            raise
        self.eligible_markets = candidate_pool
        self.last_scan_time = _time.time()
        self.scan_status = "done"
        self.scan_progress = f"Done: {len(candidate_pool)} candidates"
        logger.info("Scanner found %d candidates", len(candidate_pool))
        return candidate_pool

    def _do_scan(self):
        """采集一次候选池,每钱包按自己模板精筛+下单。"""
        candidate_pool = self._scan_with_status()
        for address, worker in self.engines.items():
            if not worker.running:
                continue
            try:
                tmpl = self.db.get_template_for(address)
                scanner = MarketScanner(self._scanner_api, self.db, "")
                eligible = scanner.filter_for_template(candidate_pool, tmpl, address)
                eligible.sort(
                    key=lambda m: float(m.get("market_competitiveness", 0) or 0)
                )
                worker.place_orders(eligible, cancel_dropouts=True)
            except Exception as e:
                logger.error("Error distributing to wallet %s: %s", address, e)

    def startup_recovery(self):
        """API-driven recovery: seed each monitor's trade watermark from DB history.

        Offline fills are caught next tick by get_trades(after=watermark) +
        id-dedup. Stale resting orders are reconciled by the monitor's
        compliance step. No DB orders/positions reconciliation.
        """
        for worker in self.engines.values():
            try:
                worker.monitor.init_watermark()
            except Exception as e:
                logger.error(
                    "Watermark init failed for %s: %s", worker.wallet_address, e
                )

    def get_status(self) -> dict:
        """Get status of all engines."""
        return {
            "engines": {
                addr: {"running": eng.running} for addr, eng in self.engines.items()
            }
        }
