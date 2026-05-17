"""api/polymarket_api.py — Polymarket CLOB + Rewards API wrapper."""

import logging
import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

logger = logging.getLogger(__name__)

POLYMARKET_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

# Rewards API base — public endpoint
REWARDS_API = "https://data-api.polymarket.com"


class PolymarketAPI:
    """Wrapper for one wallet's Polymarket connection."""

    def __init__(self, private_key: str):
        self.private_key = private_key
        self.client = ClobClient(
            host=POLYMARKET_HOST,
            chain_id=CHAIN_ID,
            key=private_key,
        )
        # Derive or create API credentials (L2 auth)
        self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def get_address(self) -> str:
        """Return wallet address derived from private key."""
        return self.client.get_address()

    # --- Market Data ---

    def get_orderbook(self, token_id: str) -> dict:
        """Get orderbook for a token. Returns {bids: [...], asks: [...]}."""
        return self.client.get_order_book(token_id)

    def get_last_trade_price(self, token_id: str) -> float:
        """Get last trade price for a token."""
        resp = self.client.get_last_trade_price(token_id)
        return float(resp.get("price", 0))

    # --- Balance ---

    def get_balance(self) -> float:
        """Get USDC balance for this wallet."""
        bal = self.client.get_balance_allowance()
        return float(bal.get("balance", 0))

    # --- Order Placement ---

    def place_limit_buy(self, token_id: str, price: float, size: int) -> dict:
        """Place a limit buy order. Returns order response with order_id."""
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side="BUY",
        )
        return self.client.create_and_post_order(order_args, OrderType.GTC)

    def place_limit_sell(self, token_id: str, price: float, size: int) -> dict:
        """Place a limit sell order at specified price."""
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side="SELL",
        )
        return self.client.create_and_post_order(order_args, OrderType.GTC)

    def place_market_sell(self, token_id: str, size: int) -> dict:
        """Place a market sell order (FOK)."""
        order_args = OrderArgs(
            token_id=token_id,
            price=0.001,  # Very low price for market sell
            size=size,
            side="SELL",
        )
        return self.client.create_and_post_order(order_args, OrderType.FOK)

    # --- Order Management ---

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a single order."""
        return self.client.cancel(order_id)

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders for this wallet."""
        return self.client.cancel_all()

    def get_order(self, order_id: str) -> dict:
        """Get order details by ID."""
        return self.client.get_order(order_id)

    def get_open_orders(self) -> list:
        """Get all open orders for this wallet."""
        return self.client.get_orders(open_only=True)

    def get_trades(self) -> list:
        """Get trade history for this wallet."""
        return self.client.get_trades()

    # --- Rewards (public API) ---

    @staticmethod
    def get_rewards_markets() -> list[dict]:
        """Fetch all markets with active rewards from Polymarket data API."""
        try:
            resp = requests.get(f"{REWARDS_API}/rewards/markets", timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Failed to fetch rewards markets: %s", e)
            return []

    @staticmethod
    def get_market_info(market_id: str) -> dict:
        """Get detailed market info including settlement date."""
        try:
            resp = requests.get(f"{REWARDS_API}/markets/{market_id}", timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Failed to fetch market info for %s: %s", market_id, e)
            return {}
