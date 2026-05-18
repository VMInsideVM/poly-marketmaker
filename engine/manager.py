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
        """Cancel all open buy orders on the exchange."""
        try:
            open_orders = self.api.get_open_orders()
            for order in open_orders:
                if order.get("side") == "BUY":
                    self.api.cancel_order(order["id"])
                    logger.info("Cancelled buy order %s", order["id"])
        except Exception as e:
            logger.error(
                "Error cancelling buy orders for %s: %s", self.wallet_address, e
            )

    def _run(self):
        """Monitor loop: check fills and stop-loss at check_interval."""
        check_interval = self.settings["fill_check_interval_sec"]
        while not self._stop_event.is_set():
            self.monitor.check_buy_orders()
            self.monitor.check_stop_loss()
            self.monitor.check_sell_orders()
            self._stop_event.wait(timeout=check_interval)

    def place_orders(self, eligible_markets: list[dict]):
        """Place orders on eligible markets (called by the shared scanner)."""
        balance = self.api.get_balance()

        for market in eligible_markets:
            # Check cooldown
            if self.db.is_in_cooldown(self.wallet_address, market["market_id"]):
                continue

            # Check if already have an order in this market
            open_orders = self.db.get_open_buy_orders(self.wallet_address)
            already_ordered = any(
                o["market_id"] == market["market_id"]
                and o["token_id"] == market["token_id"]
                for o in open_orders
            )
            if already_ordered:
                continue

            # Balance check
            required = market["order_size"] * market["order_price"]
            if required > balance:
                continue

            # Place order
            try:
                resp = self.api.place_limit_buy(
                    market["token_id"],
                    market["order_price"],
                    market["order_size"],
                    tick_size=market.get("tick_size_str", "0.01"),
                    neg_risk=market.get("neg_risk", False),
                )
                order_id = resp.get("orderID", "")
                self.db.record_order(
                    wallet=self.wallet_address,
                    market_id=market["market_id"],
                    token_id=market["token_id"],
                    market_name=market["market_name"],
                    side="buy",
                    order_id=order_id,
                    price=market["order_price"],
                    size=market["order_size"],
                )
                logger.info(
                    "Placed buy order %s: %s [%s] @ %.4f x %d",
                    order_id,
                    market["market_name"],
                    market["outcome"],
                    market["order_price"],
                    market["order_size"],
                )
            except Exception as e:
                logger.error("Error placing order for %s: %s", market["market_name"], e)

    def check_existing_orders(self, rewards_markets: list[dict]):
        """Check if existing orders are still in reward range, cancel if not."""
        try:
            open_orders = self.db.get_open_buy_orders(self.wallet_address)
            market_by_id = {m.get("condition_id", ""): m for m in rewards_markets}

            for order in open_orders:
                market_data = market_by_id.get(order["market_id"])
                if market_data is None:
                    self.api.cancel_order(order["order_id"])
                    self.db.update_order_status(order["order_id"], "cancelled")
                    continue

                # Check reward range
                try:
                    ob = self.api.get_orderbook(order["token_id"])
                    bids = ob.get("bids", [])
                    asks = ob.get("asks", [])
                    if bids and asks:
                        best_bid = float(
                            sorted(bids, key=lambda x: float(x["price"]), reverse=True)[
                                0
                            ]["price"]
                        )
                        best_ask = float(
                            sorted(asks, key=lambda x: float(x["price"]))[0]["price"]
                        )
                        midpoint = (best_bid + best_ask) / 2
                        tick_size = float(ob.get("tick_size", "0.01"))
                        max_spread = int(market_data.get("rewards_max_spread", 2))
                        reward_min = midpoint - max_spread * tick_size
                        reward_max = midpoint + max_spread * tick_size

                        if not (reward_min <= order["price"] <= reward_max):
                            self.api.cancel_order(order["order_id"])
                            self.db.update_order_status(order["order_id"], "cancelled")
                            logger.info(
                                "Cancelled order %s: price %.4f outside reward range",
                                order["order_id"],
                                order["price"],
                            )
                except Exception as e:
                    logger.warning(
                        "Error checking reward range for %s: %s", order["order_id"], e
                    )
        except Exception as e:
            logger.error(
                "Error checking existing orders for %s: %s", self.wallet_address, e
            )


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

    def start_all(self):
        """Start all wallet workers + shared scanner.

        Runs startup_recovery first to clean up stale state.
        """
        # Recovery: cancel stale buy orders, handle offline fills
        self.startup_recovery()

        wallets = self.db.list_wallets()
        for w in wallets:
            if w["enabled"]:
                self.start_wallet(w["address"], w["encrypted_key"])

        # Start shared scanner thread
        self._stop_event.clear()
        self._scanner_thread = threading.Thread(target=self._scanner_loop, daemon=True)
        self._scanner_thread.start()
        logger.info("Engine manager started with %d wallets", len(self.engines))

    def stop_all(self):
        """Stop scanner + all wallet workers."""
        self._stop_event.set()
        if self._scanner_thread:
            self._scanner_thread.join(timeout=30)

        for address in list(self.engines.keys()):
            self.stop_wallet(address)
        logger.info("Engine manager stopped")

    def restart_all(self):
        """Restart with fresh settings."""
        self.stop_all()
        self.start_all()

    def start_wallet(self, address: str, encrypted_key: str = None):
        if address in self.engines and self.engines[address].running:
            return

        if encrypted_key is None:
            wallets = self.db.list_wallets()
            wallet = next((w for w in wallets if w["address"] == address), None)
            if not wallet:
                return
            encrypted_key = wallet["encrypted_key"]

        private_key = decrypt(encrypted_key, self.encryption_key)
        api = PolymarketAPI(private_key)
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

    def _do_scan(self):
        """Run one scan cycle: find eligible markets, distribute to wallets."""
        import time as _time

        # Use shared API for market scanning (no wallet-specific data needed)
        scanner = MarketScanner(self._scanner_api, self.db, "")
        eligible = scanner.scan()
        self.eligible_markets = eligible
        self.last_scan_time = _time.time()
        logger.info("Scanner found %d eligible markets", len(eligible))

        # Also get raw rewards markets for existing-order checks
        rewards_markets = self._scanner_api.get_rewards_markets(
            min_price=0.10, max_price=0.90
        )

        # Distribute to each running wallet
        for address, worker in self.engines.items():
            if worker.running:
                try:
                    worker.place_orders(eligible)
                    worker.check_existing_orders(rewards_markets)
                except Exception as e:
                    logger.error("Error distributing to wallet %s: %s", address, e)

    def startup_recovery(self):
        """Run on program start: cancel stale buy orders, handle offline fills."""
        wallets = self.db.list_wallets()
        for w in wallets:
            try:
                private_key = decrypt(w["encrypted_key"], self.encryption_key)
                api = PolymarketAPI(private_key)

                # Cancel all remaining buy orders
                open_orders = api.get_open_orders()
                for order in open_orders:
                    if order.get("side") == "BUY":
                        api.cancel_order(order["id"])
                        logger.info(
                            "Recovery: cancelled stale buy order %s", order["id"]
                        )

                # Check for offline fills
                db_orders = self.db.get_open_buy_orders(w["address"])
                for db_order in db_orders:
                    remote = api.get_order(db_order["order_id"])
                    size_matched = int(float(remote.get("size_matched", "0")))
                    if size_matched > 0:
                        sell_resp = api.place_limit_sell(
                            db_order["token_id"], db_order["price"], size_matched
                        )
                        self.db.record_position(
                            wallet=w["address"],
                            market_id=db_order["market_id"],
                            token_id=db_order["token_id"],
                            market_name=db_order["market_name"],
                            buy_price=db_order["price"],
                            size=size_matched,
                            sell_order_id=sell_resp.get("orderID", ""),
                        )
                        logger.info(
                            "Recovery: found offline fill for %s, placed sell order",
                            db_order["order_id"],
                        )
                    self.db.update_order_status(db_order["order_id"], "recovered")

            except Exception as e:
                logger.error("Recovery error for wallet %s: %s", w["address"], e)

    def get_status(self) -> dict:
        """Get status of all engines."""
        return {
            "engines": {
                addr: {"running": eng.running} for addr, eng in self.engines.items()
            }
        }
