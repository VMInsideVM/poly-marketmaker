"""engine/scanner.py — Market scanner that filters eligible markets.

Polymarket /rewards/markets/multi API response fields:
  - condition_id: market identifier
  - market_id: numeric market ID
  - question: market question text
  - end_date: settlement date string like "2024-08-10 00:00:00"
  - tokens: [{token_id, outcome, price}, ...]
  - rewards_max_spread: max spread in ticks for reward eligibility
  - rewards_min_size: min order size for reward eligibility
  - rewards_config: [{rate_per_day, total_rewards, end_date, ...}, ...]
  - spread: current market spread
"""

import time
import logging
from datetime import datetime
from engine.strategy import determine_order_price

logger = logging.getLogger(__name__)


def _parse_end_date(end_date_str: str) -> float:
    """Parse end_date string to Unix timestamp."""
    if not end_date_str:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(end_date_str, fmt).timestamp()
        except ValueError:
            continue
    return 0


def _calc_total_rewards(rewards_config: list[dict]) -> float:
    """Calculate total daily reward value from rewards_config array."""
    return sum(rc.get("rate_per_day", 0) for rc in rewards_config)


class MarketScanner:
    def __init__(self, api, db, wallet_address: str):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address

    def scan(self) -> list[dict]:
        """Scan all reward markets and return eligible ones with order prices.

        Each token in a market is evaluated independently (YES and NO are
        separate orderbooks on Polymarket).
        """
        settings = self.db.get_settings()
        balance = self.api.get_balance()
        markets = self.api.get_rewards_markets()
        eligible = []

        for market in markets:
            # Each market has multiple tokens (YES/NO), evaluate each
            tokens = market.get("tokens", [])
            for token in tokens:
                result = self._evaluate_token(market, token, settings, balance)
                if result is not None:
                    eligible.append(result)

        return eligible

    def _evaluate_token(
        self, market: dict, token: dict, settings: dict, balance: float
    ) -> dict | None:
        """Evaluate a single token in a market. Return enriched dict if eligible."""
        market_id = market.get("condition_id", market.get("market_id", ""))
        token_id = token.get("token_id", "")
        token_price = float(token.get("price", 0))

        # Filter: reward amount (total daily rate in USD)
        rewards_config = market.get("rewards_config", [])
        daily_reward = _calc_total_rewards(rewards_config)
        if daily_reward < settings["min_reward_usd"]:
            return None

        # Filter: settlement date
        end_date_str = market.get("end_date", "")
        end_date_ts = _parse_end_date(end_date_str)
        days_remaining = (end_date_ts - time.time()) / 86400
        if days_remaining < settings["min_settlement_days"]:
            return None

        # Filter: cooldown
        if self.db.is_in_cooldown(self.wallet_address, market_id):
            return None

        # Filter: token price range (quick pre-filter before fetching orderbook)
        if (
            token_price * 100 < settings["min_price_cents"]
            or token_price * 100 > settings["max_price_cents"]
        ):
            return None

        # Get orderbook for this specific token
        try:
            orderbook = self.api.get_orderbook(token_id)
        except Exception as e:
            logger.warning("Failed to get orderbook for %s: %s", token_id, e)
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

        # Filter: price range (from actual orderbook)
        if (
            best_bid * 100 < settings["min_price_cents"]
            or best_bid * 100 > settings["max_price_cents"]
        ):
            return None

        # Reward parameters
        max_spread = int(market.get("rewards_max_spread", 2))
        min_size = int(market.get("rewards_min_size", 0))

        # Calculate reward range: midpoint +/- max_spread ticks
        # The tick_size determines price granularity
        midpoint = (best_bid + best_ask) / 2
        tick_size = 0.01  # default 1 cent
        # Polymarket markets use either 0.01 or 0.001 tick sizes
        # Check if prices suggest fine-grained ticks
        if any(
            "." in str(b["price"]) and len(str(b["price"]).split(".")[-1]) >= 3
            for b in bids[:3]
        ):
            tick_size = 0.001

        reward_range_min = midpoint - max_spread * tick_size
        reward_range_max = midpoint + max_spread * tick_size

        # Determine order price using strategy
        order_price = determine_order_price(
            bids=bids,
            max_spread=max_spread,
            tick_size=tick_size,
            reward_range_min=reward_range_min,
            reward_range_max=reward_range_max,
        )
        if order_price is None:
            return None

        # Filter: balance sufficient for min_size
        required = min_size * order_price
        if required > balance:
            return None

        return {
            "market_id": market_id,
            "token_id": token_id,
            "market_name": market.get("question", ""),
            "outcome": token.get("outcome", ""),
            "end_date": end_date_str,
            "daily_reward": daily_reward,
            "rewards_max_spread": max_spread,
            "rewards_min_size": min_size,
            "tick_size": tick_size,
            "reward_range_min": reward_range_min,
            "reward_range_max": reward_range_max,
            "order_price": order_price,
            "order_size": min_size,
        }
