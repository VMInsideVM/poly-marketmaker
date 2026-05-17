"""engine/scanner.py — Market scanner that filters eligible markets."""

import time
import logging
from engine.strategy import determine_order_price

logger = logging.getLogger(__name__)


class MarketScanner:
    def __init__(self, api, db, wallet_address: str):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address

    def scan(self) -> list[dict]:
        """Scan all reward markets and return eligible ones with order prices."""
        settings = self.db.get_settings()
        balance = self.api.get_balance()
        markets = self.api.get_rewards_markets()
        eligible = []

        for market in markets:
            result = self._evaluate_market(market, settings, balance)
            if result is not None:
                eligible.append(result)

        return eligible

    def _evaluate_market(
        self, market: dict, settings: dict, balance: float
    ) -> dict | None:
        """Evaluate a single market. Return enriched dict if eligible, else None."""
        # Filter: reward amount
        if market.get("reward_usd", 0) < settings["min_reward_usd"]:
            return None

        # Filter: settlement date
        end_date = market.get("end_date", 0)
        days_remaining = (end_date - time.time()) / 86400
        if days_remaining < settings["min_settlement_days"]:
            return None

        # Filter: cooldown
        if self.db.is_in_cooldown(self.wallet_address, market["market_id"]):
            return None

        # Get orderbook
        try:
            orderbook = self.api.get_orderbook(market["token_id"])
        except Exception as e:
            logger.warning("Failed to get orderbook for %s: %s", market["market_id"], e)
            return None

        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if not bids or not asks:
            return None

        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])

        # Filter: bid-ask spread
        spread_cents = (best_ask - best_bid) * 100
        if spread_cents >= settings["max_spread_cents"]:
            return None

        # Filter: price range
        if (
            best_bid * 100 < settings["min_price_cents"]
            or best_bid * 100 > settings["max_price_cents"]
        ):
            return None

        # Determine order price using strategy
        order_price = determine_order_price(
            bids=bids,
            max_spread=market.get("max_spread", 2),
            tick_size=market.get("tick_size", 0.01),
            reward_range_min=market.get("reward_range_min", 0),
            reward_range_max=market.get("reward_range_max", 1),
        )
        if order_price is None:
            return None

        # Filter: balance sufficient for min_size
        min_size = market.get("min_size", 0)
        required = min_size * order_price
        if required > balance:
            return None

        return {
            **market,
            "order_price": order_price,
            "order_size": min_size,
        }
