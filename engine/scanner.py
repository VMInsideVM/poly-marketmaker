"""engine/scanner.py — Market scanner that filters eligible markets.

Filtering flow (matches test_live.py logic):
1. /rewards/markets/multi — fetch markets sorted by rate_per_day DESC
2. Filter: total rate_per_day >= min_reward (from rewards_config sum)
3. Filter: settlement date (only exclude 0~4 days, negative = pass)
4. /rewards/markets/{condition_id} — get precise per-market reward
5. Filter: at least one token price in [min_price, max_price]
6. GET /spread — fast spread check per token
7. GET /book — full orderbook for strategy calculation
"""

import re
import time
import logging
from datetime import datetime
from engine.strategy import determine_order_price

logger = logging.getLogger(__name__)


def _parse_end_date(end_date_str: str) -> float:
    """Parse end_date string to Unix timestamp."""
    if not end_date_str:
        return 0
    s = end_date_str.strip()
    if s.endswith("Z"):
        s = s[:-1]
    else:
        m = re.search(r"(\d{2}:\d{2}:\d{2})[+-]\d{2}:?\d{0,2}$", s)
        if m:
            s = s[: m.end(1)]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return 0


class MarketScanner:
    def __init__(self, api, db, wallet_address: str):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address

    def scan(self) -> list[dict]:
        """Scan reward markets and return eligible ones with order prices.

        Returns list of dicts, each representing one token to place an order on.
        """
        settings = self.db.get_settings()
        balance = self.api.get_balance()
        min_reward = settings["min_reward_usd"]
        min_price_cents = settings["min_price_cents"]
        max_price_cents = settings["max_price_cents"]
        max_spread_cents = settings["max_spread_cents"]
        min_days = settings["min_settlement_days"]

        # Step 1: Fetch markets (server-side filter: price 0.10~0.90, sorted by rate)
        markets = self.api.get_rewards_markets(
            min_price=0.10,
            max_price=0.90,
        )

        eligible = []

        for market in markets:
            tokens = market.get("tokens", [])
            if not tokens:
                continue

            condition_id = market.get("condition_id", "")
            rewards_config = market.get("rewards_config", [])
            total_rate = sum(rc.get("rate_per_day", 0) for rc in rewards_config)

            # Step 2: Coarse reward filter
            if total_rate < min_reward:
                continue

            # Step 3: Settlement date (only exclude 0~min_days, negative = pass)
            end_date_str = market.get("end_date", "")
            end_ts = _parse_end_date(end_date_str)
            days_left = (end_ts - time.time()) / 86400 if end_ts else -1
            if 0 <= days_left < min_days:
                continue

            # Cooldown check
            if self.db.is_in_cooldown(self.wallet_address, condition_id):
                continue

            # Step 4: Get precise reward via /rewards/markets/{condition_id}
            raw_rewards = self.api.get_rewards_for_market(condition_id)
            market_reward = 0
            for rd in raw_rewards:
                for rc in rd.get("rewards_config", []):
                    market_reward += rc.get("rate_per_day", 0)
            if not raw_rewards:
                market_reward = total_rate
            if market_reward < min_reward:
                continue

            # Step 5: Find tokens with price in range
            valid_tokens = [
                t
                for t in tokens
                if min_price_cents <= float(t.get("price", 0)) * 100 <= max_price_cents
            ]
            if not valid_tokens:
                continue

            # Step 6 & 7: Check each valid token
            max_spread_reward = int(market.get("rewards_max_spread", 2))
            min_size = int(market.get("rewards_min_size", 0))
            neg_risk = market.get("neg_risk", False)

            for token in valid_tokens:
                token_id = token.get("token_id", "")
                token_price = float(token.get("price", 0))

                # Step 6: Fast spread check
                spread_val = self.api.get_spread(token_id)
                if spread_val < 0:
                    continue
                if spread_val * 100 >= max_spread_cents:
                    continue

                # Step 7: Full orderbook
                try:
                    orderbook = self.api.get_orderbook(token_id)
                except Exception as e:
                    logger.warning("Failed to get orderbook for %s: %s", token_id, e)
                    continue

                bids = sorted(
                    orderbook.get("bids", []),
                    key=lambda x: float(x["price"]),
                    reverse=True,
                )
                asks = sorted(
                    orderbook.get("asks", []), key=lambda x: float(x["price"])
                )
                if not bids or not asks:
                    continue

                best_bid = float(bids[0]["price"])
                best_ask = float(asks[0]["price"])

                # Confirm price range with actual best_bid
                if best_bid * 100 < min_price_cents or best_bid * 100 > max_price_cents:
                    continue

                # Balance check
                if min_size * best_bid > balance:
                    continue

                # Calculate reward range and determine order price
                tick_size_str = orderbook.get("tick_size", "0.01")
                tick_size = float(tick_size_str)
                midpoint = (best_bid + best_ask) / 2
                reward_range_min = midpoint - max_spread_reward * tick_size
                reward_range_max = midpoint + max_spread_reward * tick_size

                order_price = determine_order_price(
                    bids=bids,
                    max_spread=max_spread_reward,
                    tick_size=tick_size,
                    reward_range_min=reward_range_min,
                    reward_range_max=reward_range_max,
                )
                if order_price is None:
                    continue

                eligible.append(
                    {
                        "market_id": condition_id,
                        "token_id": token_id,
                        "market_name": market.get("question", ""),
                        "outcome": token.get("outcome", ""),
                        "market_competitiveness": market.get(
                            "market_competitiveness", 0
                        ),
                        "end_date": end_date_str,
                        "daily_reward": market_reward,
                        "rewards_max_spread": max_spread_reward,
                        "rewards_min_size": min_size,
                        "tick_size": tick_size,
                        "tick_size_str": tick_size_str,
                        "neg_risk": neg_risk,
                        "reward_range_min": reward_range_min,
                        "reward_range_max": reward_range_max,
                        "order_price": order_price,
                        "order_size": min_size,
                    }
                )
                # Both tokens can be eligible independently

        return eligible
