"""engine/manager.py — Multi-wallet engine manager."""

import logging
import threading
import time
from api.polymarket_api import PolymarketAPI
from engine.scanner import MarketScanner
from engine.monitor import OrderMonitor
from utils.crypto import decrypt

logger = logging.getLogger(__name__)


class WalletEngine:
    """Engine for a single wallet — runs scan, order, monitor loops."""

    def __init__(self, api: PolymarketAPI, db, wallet_address: str, settings: dict):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address
        self.settings = settings
        self.scanner = MarketScanner(api, db, wallet_address)
        self.monitor = OrderMonitor(api, db, wallet_address)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.running = True
        logger.info("Engine started for wallet %s", self.wallet_address)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=30)
        self.running = False
        self._cancel_buy_orders()
        logger.info("Engine stopped for wallet %s", self.wallet_address)

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
        scan_interval = self.settings["scan_interval_sec"]
        check_interval = self.settings["fill_check_interval_sec"]
        last_scan = 0

        while not self._stop_event.is_set():
            now = time.time()

            # Scan and place orders at scan_interval
            if now - last_scan >= scan_interval:
                self._scan_and_place()
                self._check_existing_orders()
                last_scan = now

            # Check fills and stop-loss at check_interval
            self.monitor.check_buy_orders()
            self.monitor.check_stop_loss()
            self.monitor.check_sell_orders()

            self._stop_event.wait(timeout=check_interval)

    def _scan_and_place(self):
        """Scan markets and place buy orders."""
        try:
            eligible = self.scanner.scan()
            for market in eligible:
                # Check if we already have an open order in this market
                open_orders = self.db.get_open_buy_orders(self.wallet_address)
                already_ordered = any(
                    o["market_id"] == market["market_id"] for o in open_orders
                )
                if already_ordered:
                    continue

                resp = self.api.place_limit_buy(
                    market["token_id"],
                    market["order_price"],
                    market["order_size"],
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
                    "Placed buy order %s: %s @ %.4f x %d",
                    order_id,
                    market["market_name"],
                    market["order_price"],
                    market["order_size"],
                )
        except Exception as e:
            logger.error("Error in scan_and_place for %s: %s", self.wallet_address, e)

    def _check_existing_orders(self):
        """Check if existing orders are still in reward range, cancel if not."""
        try:
            open_orders = self.db.get_open_buy_orders(self.wallet_address)
            markets = self.api.get_rewards_markets()
            for order in open_orders:
                market_data = next(
                    (m for m in markets if m["market_id"] == order["market_id"]), None
                )
                if market_data is None:
                    self.api.cancel_order(order["order_id"])
                    self.db.update_order_status(order["order_id"], "cancelled")
                    continue

                reward_min = market_data.get("reward_range_min", 0)
                reward_max = market_data.get("reward_range_max", 1)
                if not (reward_min <= order["price"] <= reward_max):
                    self.api.cancel_order(order["order_id"])
                    self.db.update_order_status(order["order_id"], "cancelled")
                    logger.info(
                        "Cancelled order %s: price %.4f outside reward range [%.4f, %.4f]",
                        order["order_id"],
                        order["price"],
                        reward_min,
                        reward_max,
                    )
        except Exception as e:
            logger.error("Error checking existing orders: %s", e)


class EngineManager:
    """Manages engines for all wallets."""

    def __init__(self, db, encryption_key: bytes):
        self.db = db
        self.encryption_key = encryption_key
        self.engines: dict[str, WalletEngine] = {}

    def start_all(self):
        """Start engines for all enabled wallets."""
        wallets = self.db.list_wallets()
        for w in wallets:
            if w["enabled"]:
                self.start_wallet(w["address"], w["encrypted_key"])

    def stop_all(self):
        """Stop all running engines (cancels buy orders)."""
        for address in list(self.engines.keys()):
            self.stop_wallet(address)

    def restart_all(self):
        """Restart all engines with fresh settings."""
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
        engine = WalletEngine(api, self.db, address, settings)
        self.engines[address] = engine
        engine.start()

    def stop_wallet(self, address: str):
        engine = self.engines.pop(address, None)
        if engine:
            engine.stop()

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
                    size_matched = int(remote.get("size_matched", 0))
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
