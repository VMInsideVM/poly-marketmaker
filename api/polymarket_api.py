"""api/polymarket_api.py — Polymarket CLOB + Rewards API wrapper."""

import logging
import requests
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
    OrderPayload,
    MarketOrderArgsV2,
    PartialCreateOrderOptions,
)

logger = logging.getLogger(__name__)

POLYMARKET_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

# Rewards API is part of the CLOB API
REWARDS_API = POLYMARKET_HOST


class PolymarketAPI:
    """Wrapper for one wallet's Polymarket connection."""

    def __init__(self, private_key: str, signature_type: int = 2, funder: str = None):
        """Initialize with private key.

        Args:
            private_key: Hex private key string.
            signature_type: 0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE (default, for browser wallets).
            funder: Proxy wallet address. If None, derived from private key.
        """
        self.private_key = private_key
        # Step 1: Create temp client to derive API creds
        temp_client = ClobClient(
            host=POLYMARKET_HOST,
            key=private_key,
            chain_id=CHAIN_ID,
        )
        api_creds = temp_client.derive_api_key()
        # Step 2: Create full client with L2 auth
        self.client = ClobClient(
            host=POLYMARKET_HOST,
            key=private_key,
            chain_id=CHAIN_ID,
            creds=api_creds,
            signature_type=signature_type,
            funder=funder or temp_client.get_address(),
        )

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
        """Get pUSD (collateral) balance for this wallet, in human-readable units."""
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        bal = self.client.get_balance_allowance(params)
        raw = float(bal.get("balance", 0))
        return raw / 1e6  # pUSD has 6 decimals

    # --- Order Placement ---

    def place_limit_buy(
        self,
        token_id: str,
        price: float,
        size: int,
        tick_size: str = "0.01",
        neg_risk: bool = False,
    ) -> dict:
        """Place a limit buy order.

        Returns dict with "orderID" and "status" keys.
        """
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=float(size),
            side="BUY",
        )
        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        return self.client.create_and_post_order(order_args, options, OrderType.GTC)

    def place_limit_sell(
        self,
        token_id: str,
        price: float,
        size: int,
        tick_size: str = "0.01",
        neg_risk: bool = False,
    ) -> dict:
        """Place a limit sell order at specified price.

        Returns dict with "orderID" and "status" keys.
        """
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=float(size),
            side="SELL",
        )
        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        return self.client.create_and_post_order(order_args, options, OrderType.GTC)

    def place_market_sell(
        self, token_id: str, size: int, tick_size: str = "0.01", neg_risk: bool = False
    ) -> dict:
        """Place a market sell order (FOK).

        Uses MarketOrderArgsV2 with amount (pUSD value to sell).
        """
        market_args = MarketOrderArgsV2(
            token_id=token_id,
            amount=float(size),
            side="SELL",
        )
        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        return self.client.create_and_post_market_order(
            market_args, options, OrderType.FOK
        )

    # --- Order Management ---

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a single order by ID."""
        return self.client.cancel_order(OrderPayload(orderID=order_id))

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders for this wallet."""
        return self.client.cancel_all()

    def get_order(self, order_id: str) -> dict:
        """Get order details by ID.

        Returns dict with keys: id, status, side, original_size,
        size_matched, price, asset_id, market, etc.
        """
        return self.client.get_order(order_id)

    def get_open_orders(self) -> list:
        """Get all open orders for this wallet.

        Returns list of order dicts with keys: id, status, side,
        original_size, size_matched, price, asset_id, market, etc.
        """
        return self.client.get_open_orders()

    def get_trades(self) -> list:
        """Get trade history for this wallet."""
        return self.client.get_trades()

    # --- Rewards (CLOB API - public endpoints) ---

    @staticmethod
    def get_rewards_markets() -> list[dict]:
        """Fetch all markets with active rewards using /rewards/markets/multi.

        Uses the CLOB endpoint GET /rewards/markets/multi which returns
        markets with reward configs, token info, spread, end_date, etc.

        Returns normalized list of dicts with keys:
          - market_id, condition_id, question, end_date
          - tokens: [{token_id, outcome, price}, ...]
          - rewards_max_spread, rewards_min_size
          - rewards_config: [{rate_per_day, total_rewards, end_date, ...}, ...]
          - spread, volume_24hr
        """
        all_markets = []
        next_cursor = ""
        try:
            while True:
                params = {"page_size": 500}
                if next_cursor:
                    params["next_cursor"] = next_cursor
                resp = requests.get(
                    f"{REWARDS_API}/rewards/markets/multi",
                    params=params,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                markets = data.get("data", [])
                all_markets.extend(markets)
                next_cursor = data.get("next_cursor", "LTE=")
                if next_cursor == "LTE=" or not markets:
                    break
        except Exception as e:
            logger.error("Failed to fetch rewards markets: %s", e)
        return all_markets

    @staticmethod
    def get_rewards_current() -> list[dict]:
        """Fetch current active rewards configurations using /rewards/markets/current.

        Returns list of dicts with keys:
          - condition_id, rewards_max_spread, rewards_min_size
          - rewards_config: [{rate_per_day, total_rewards, ...}, ...]
        """
        all_rewards = []
        next_cursor = ""
        try:
            while True:
                params = {}
                if next_cursor:
                    params["next_cursor"] = next_cursor
                resp = requests.get(
                    f"{REWARDS_API}/rewards/markets/current",
                    params=params,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                rewards = data.get("data", [])
                all_rewards.extend(rewards)
                next_cursor = data.get("next_cursor", "LTE=")
                if next_cursor == "LTE=" or not rewards:
                    break
        except Exception as e:
            logger.error("Failed to fetch current rewards: %s", e)
        return all_rewards
