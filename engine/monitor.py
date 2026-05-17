"""engine/monitor.py — Order fill monitoring and stop-loss."""

import logging

logger = logging.getLogger(__name__)


class OrderMonitor:
    def __init__(self, api, db, wallet_address: str):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address
        # Track partially filled amounts to detect new fills
        self._last_matched: dict[str, int] = {}

    def check_buy_orders(self):
        """Check all open buy orders for fills. Handle full and partial fills."""
        settings = self.db.get_settings()
        orders = self.db.get_open_buy_orders(self.wallet_address)

        for order in orders:
            try:
                self._process_order(order, settings)
            except Exception as e:
                logger.error("Error checking order %s: %s", order["order_id"], e)

    def _process_order(self, order: dict, settings: dict):
        order_id = order["order_id"]
        remote = self.api.get_order(order_id)
        size_matched = int(remote.get("size_matched", 0))
        prev_matched = self._last_matched.get(order_id, 0)
        new_fill = size_matched - prev_matched

        if new_fill <= 0:
            return

        self._last_matched[order_id] = size_matched

        # Place sell order for newly filled portion
        sell_resp = self.api.place_limit_sell(
            order["token_id"], order["price"], new_fill
        )
        sell_order_id = sell_resp.get("orderID", "")

        # Record position
        self.db.record_position(
            wallet=self.wallet_address,
            market_id=order["market_id"],
            token_id=order["token_id"],
            market_name=order["market_name"],
            buy_price=order["price"],
            size=new_fill,
            sell_order_id=sell_order_id,
        )

        # Record trade history
        self.db.record_trade(
            wallet=self.wallet_address,
            market_id=order["market_id"],
            market_name=order["market_name"],
            side="buy",
            price=order["price"],
            size=new_fill,
        )

        is_fully_filled = remote.get("status") == "MATCHED"
        if is_fully_filled:
            self.db.update_order_status(order_id, "filled")
            del self._last_matched[order_id]

        # Set cooldown
        self.db.set_cooldown(
            self.wallet_address,
            order["market_id"],
            settings["cooldown_minutes"],
        )
        logger.info(
            "Buy order %s filled %d shares at %.4f", order_id, new_fill, order["price"]
        )

    def check_stop_loss(self):
        """Check positions for stop-loss trigger."""
        settings = self.db.get_settings()
        stop_loss_pct = settings["stop_loss_pct"] / 100.0
        positions = self.db.get_positions(self.wallet_address)

        for pos in positions:
            try:
                self._check_position_stop_loss(pos, stop_loss_pct)
            except Exception as e:
                logger.error(
                    "Error checking stop loss for position %s: %s", pos["id"], e
                )

    def _check_position_stop_loss(self, pos: dict, stop_loss_pct: float):
        current_price = self.api.get_last_trade_price(pos["token_id"])
        threshold = pos["buy_price"] * (1 - stop_loss_pct)

        if current_price <= threshold:
            logger.warning(
                "Stop loss triggered for position %d: price %.4f <= threshold %.4f",
                pos["id"],
                current_price,
                threshold,
            )
            # Cancel existing sell order
            if pos.get("sell_order_id"):
                self.api.cancel_order(pos["sell_order_id"])

            # Market sell
            self.api.place_market_sell(pos["token_id"], pos["size"])

            # Record trade
            pnl = (current_price - pos["buy_price"]) * pos["size"]
            self.db.record_trade(
                wallet=self.wallet_address,
                market_id=pos["market_id"],
                market_name=pos.get("market_name", ""),
                side="stop_loss",
                price=current_price,
                size=pos["size"],
                pnl=pnl,
            )
            self.db.close_position(pos["id"])

    def check_sell_orders(self):
        """Check if any sell orders have been filled."""
        positions = self.db.get_positions(self.wallet_address)
        for pos in positions:
            if not pos.get("sell_order_id"):
                continue
            try:
                remote = self.api.get_order(pos["sell_order_id"])
                if remote.get("status") == "MATCHED":
                    self.db.record_trade(
                        wallet=self.wallet_address,
                        market_id=pos["market_id"],
                        market_name=pos.get("market_name", ""),
                        side="sell",
                        price=pos["buy_price"],
                        size=pos["size"],
                        pnl=0.0,
                    )
                    self.db.close_position(pos["id"])
                    logger.info("Sell order filled for position %d", pos["id"])
            except Exception as e:
                logger.error(
                    "Error checking sell order for position %s: %s", pos["id"], e
                )
