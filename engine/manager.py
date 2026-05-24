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
from engine.positions import held_condition_ids
from engine.order_sizing import compute_order_size
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
        """Monitor loop: run one tick at each check_interval."""
        check_interval = self.settings["fill_check_interval_sec"]
        while not self._stop_event.is_set():
            self._tick()
            self._stop_event.wait(timeout=check_interval)

    def _tick(self):
        """One monitor pass: detect fills, maintain take-profit sells, stop-loss,
        strategy compliance. check_take_profit runs right after fill detection so
        the position-driven sell reflects the latest fills."""
        self.monitor.begin_status_tick()
        self.monitor.check_buy_orders()
        self.monitor.check_take_profit()
        self.monitor.check_stop_loss()
        self.monitor.check_sell_orders()
        self.monitor.publish_status()

    def place_orders(self, eligible_markets: list[dict], limit: int | None = None):
        """Place orders on eligible markets; price recomputed at placement time.

        If ``limit`` is set, stop after that many *successful* placements
        (a placement counts only when ``place_limit_buy`` succeeds).
        """
        from engine.strategy import determine_order_price

        placed = 0
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
        held = held_condition_ids(positions)
        buy_orders = [o for o in open_orders if o.get("side") == "BUY"]

        # Hard per-wallet cap on the TOTAL open buy orders. Read live from
        # settings (not the constructor snapshot) so a config change takes
        # effect on the next placement without restarting the engine.
        settings = self.db.get_settings()
        max_buys = int(settings.get("max_buy_orders_per_wallet", 5))
        order_size_mode = settings.get("order_size_mode", "min")
        order_size_custom_usd = float(settings.get("order_size_custom_usd", 0) or 0)

        # If existing buys already exceed the cap (e.g. the user just lowered
        # it), cancel the excess down to the cap. Keep the oldest orders
        # (smallest created_at = longest queue priority / most rewards) and
        # cancel the newest excess.
        if len(buy_orders) > max_buys:
            ordered = sorted(
                buy_orders, key=lambda o: float(o.get("created_at", 0) or 0)
            )
            keep, excess = ordered[:max_buys], ordered[max_buys:]
            excess_ids = [o["id"] for o in excess if o.get("id")]
            if excess_ids:
                try:
                    self.api.cancel_orders(excess_ids)
                    logger.info(
                        "Cancelled %d excess buy orders for %s (cap %d)",
                        len(excess_ids),
                        self.wallet_address,
                        max_buys,
                    )
                    self._record_cap_cancel(excess)
                    buy_orders = keep
                except Exception as e:
                    logger.error(
                        "Cancel excess buys for %s failed: %s", self.wallet_address, e
                    )

        open_buy_assets = {o.get("asset_id") for o in buy_orders}
        # available slots = cap - existing buys. Recomputed each distribution
        # from the live order count, so the total never exceeds the cap.
        slots = max_buys - len(buy_orders)
        if slots <= 0:
            logger.info(
                "Buy-order cap (%d) reached for %s, skipping placement",
                max_buys,
                self.wallet_address,
            )
            return
        effective_limit = slots if limit is None else min(limit, slots)

        # 全局黑名单:任何钱包都不再挂这些 condition_id 的买单(一次性加载)。
        blacklist = self.db.get_blacklist_ids()

        for market in eligible_markets:
            if market["market_id"] in blacklist:
                continue
            if market["market_id"] in held:
                continue
            if self.db.is_in_cooldown(self.wallet_address, market["market_id"]):
                continue
            if market["token_id"] in open_buy_assets:
                continue

            # Per-wallet balance gate: skip markets this wallet can't even
            # afford the minimum reward-qualifying order on, before doing any
            # detailed work (orderbook/strategy). min_cost is recorded at scan.
            min_cost = float(market.get("min_cost", 0) or 0)
            if self.api.get_balance() < min_cost:
                logger.info(
                    "Skip %s: balance below min_cost %.2f",
                    market["market_name"],
                    min_cost,
                )
                continue

            try:
                ob = self.api.get_orderbook(market["token_id"])
            except Exception as e:
                logger.warning("Orderbook failed for %s: %s", market["market_name"], e)
                continue
            bids = sorted(
                ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True
            )
            asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
            if not bids or not asks:
                continue
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            midpoint = (best_bid + best_ask) / 2
            tick = float(ob.get("tick_size", "0.01"))
            tick_str = ob.get("tick_size", "0.01")
            max_spread = int(market.get("rewards_max_spread", 2))
            rmin = midpoint - max_spread * tick
            rmax = midpoint + max_spread * tick
            try:
                order_price = determine_order_price(
                    bids=bids,
                    max_spread=max_spread,
                    tick_size=tick,
                    reward_range_min=rmin,
                    reward_range_max=rmax,
                )
            except Exception as e:
                logger.warning("Strategy failed for %s: %s", market["market_name"], e)
                continue
            if order_price is None:
                continue

            balance = self.api.get_balance()
            # 下单份额按设置的模式决定(min=最小合格份额 / custom=美元上限 /
            # balance=全额)。maker 买单不锁仓,同一笔余额垫付所有挂着的买单,
            # 所以跨市场循环时刻意 *不* 递减余额。
            min_size = int(
                market.get("rewards_min_size", market.get("order_size", 0)) or 0
            )
            order_size = compute_order_size(
                order_size_mode,
                order_price,
                balance,
                min_size,
                order_size_custom_usd,
            )
            if order_size is None:
                logger.info(
                    "Skip %s: order_size=None "
                    "(mode=%s balance=%.2f price=%.4f min_size=%d cap=%.2f)",
                    market["market_name"],
                    order_size_mode,
                    balance,
                    order_price,
                    min_size,
                    order_size_custom_usd,
                )
                continue
            try:
                self.api.place_limit_buy(
                    market["token_id"],
                    order_price,
                    order_size,
                    tick_size=tick_str,
                    neg_risk=market.get("neg_risk", False),
                )
                logger.info(
                    "Placed buy %s [%s] @ %.4f x %d",
                    market["market_name"],
                    market["outcome"],
                    order_price,
                    order_size,
                )
                placed += 1
                self._record_place_buy(
                    market, order_price, order_size, max_spread, rmin, rmax
                )
                if placed >= effective_limit:
                    break
            except Exception as e:
                logger.error("Error placing order for %s: %s", market["market_name"], e)

    def _record_cap_cancel(self, excess: list):
        """Log each buy order cancelled for exceeding the per-wallet cap."""
        for o in excess:
            try:
                self.db.record_action(
                    wallet=self.wallet_address,
                    market_id=o.get("market", ""),
                    action_type="cap_cancel_excess",
                    side="-",
                    price=-1,
                    size=int(float(o.get("original_size", 0) or 0)),
                    reason=(
                        "超过每钱包挂买单上限，撤销多余买单"
                        "（保留挂得最久的，丢失队列优先级最小）"
                    ),
                    price_basis=(
                        f"撤 order_id={o.get('id')}；上限=max_buy_orders_per_wallet；"
                        f"来源：CLOB get_open_orders"
                    ),
                )
            except Exception as e:
                logger.warning("record_action(cap_cancel_excess) failed: %s", e)

    def _record_place_buy(
        self, market, order_price, order_size, max_spread, rmin, rmax
    ):
        """Log a successful buy placement to the actions table (never raises)."""
        try:
            self.db.record_action(
                wallet=self.wallet_address,
                market_id=market["market_id"],
                action_type="place_buy",
                side="买入",
                price=order_price,
                size=order_size,
                reason="按策略在奖励区间内挂买单（贴最优买价深度，最大化奖励占比）",
                price_basis=(
                    f"应挂价 {order_price:.4f}=determine_order_price(bids, "
                    f"ms{max_spread}, 区间[{rmin:.4f},{rmax:.4f}])；"
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
                    pk, funder=enabled[0].get("funder") or None
                )
                logger.info("Created scanner API from wallet %s", enabled[0]["address"])

        eligible = self._scan_with_status()

        # Persist to database (replace old data)
        self.db.save_eligible_markets(eligible)
        logger.info("Saved %d eligible markets to database", len(eligible))

    def place_all_orders(self):
        """Distribute eligible markets to all wallets for order placement.

        Orders are placed from lowest competitiveness first (less competition = more reward share).
        """
        if not self.eligible_markets:
            logger.warning("No eligible markets to place orders on")
            return

        # Sort: lowest competitiveness first
        sorted_markets = sorted(
            self.eligible_markets,
            key=lambda m: float(m.get("market_competitiveness", 0) or 0),
        )

        for address, worker in self.engines.items():
            if worker.running:
                try:
                    worker.place_orders(sorted_markets)
                except Exception as e:
                    logger.error("Error placing orders for %s: %s", address, e)
        logger.info(
            "Distributed %d eligible markets to %d wallets",
            len(self.eligible_markets),
            len(self.engines),
        )

    def test_place_orders(self) -> dict:
        """Place up to 3 strategy-compliant test buys on the first enabled
        wallet, iterating eligible markets until 3 succeed.

        Does not require the monitor to be running: if the first enabled
        wallet already has a running worker it is reused (so fills get
        monitored and written to history); otherwise a transient API/worker
        is constructed just to place the orders (no monitor thread)."""
        if not self.eligible_markets:
            return {"ok": False, "message": "请先扫描市场"}

        sorted_markets = sorted(
            self.eligible_markets,
            key=lambda m: float(m.get("market_competitiveness", 0) or 0),
        )

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
                api = PolymarketAPI(private_key, funder=funder or None)
                settings = self.db.get_settings()
                worker = WalletWorker(api, self.db, address, settings)
            except Exception as e:
                logger.error("Error building API for test orders: %s", e)
                return {"ok": False, "message": f"测试挂单失败：{e}"}

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
        api = PolymarketAPI(private_key, funder=funder or None)
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
            eligible = scanner.scan(on_progress=on_progress, on_found=on_found)
        except Exception:
            self.eligible_markets = prev_eligible  # don't blank on failure
            self.scan_status = "done"  # not 'scanning': progress bar won't stick
            raise
        self.eligible_markets = eligible
        self.last_scan_time = _time.time()
        self.scan_status = "done"
        self.scan_progress = f"Done: {len(eligible)} eligible"
        logger.info("Scanner found %d eligible markets", len(eligible))
        return eligible

    def _do_scan(self):
        """Run one scan cycle: find eligible markets, distribute to wallets."""
        eligible = self._scan_with_status()

        # Distribute lowest-competitiveness first (less competition = larger
        # reward share), matching the manual place_all_orders path. The scan
        # returns markets in rate_per_day DESC order, so without this sort auto
        # mode would diverge from the documented "lowest competitiveness first".
        sorted_markets = sorted(
            eligible, key=lambda m: float(m.get("market_competitiveness", 0) or 0)
        )

        # Distribute to each running wallet
        for address, worker in self.engines.items():
            if worker.running:
                try:
                    worker.place_orders(sorted_markets)
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
