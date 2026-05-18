"""api/polymarket_api.py — Polymarket CLOB + Rewards API wrapper."""

import logging
import time
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
RELAYER_URL = "https://relayer-v2.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet
SIG_POLY_1271 = 3  # Deposit wallet signature type (ERC-1271)

# Rewards API is part of the CLOB API
REWARDS_API = POLYMARKET_HOST


def _derive_deposit_wallet(private_key: str) -> str:
    """Derive the deterministic deposit wallet address from the EOA private key.

    Uses the relayer client's offline derivation (no network call).
    """
    from py_builder_relayer_client.client import RelayClient

    rc = RelayClient(RELAYER_URL, CHAIN_ID, private_key)
    return rc.get_expected_safe()


class PolymarketAPI:
    """Wrapper for one wallet's Polymarket connection.

    Orders are placed from the user's deposit wallet (signature_type=3, POLY_1271).
    """

    def __init__(
        self, private_key: str, signature_type: int = SIG_POLY_1271, funder: str = None
    ):
        """Initialize with private key.

        Args:
            private_key: Hex private key string (the deposit wallet owner / EOA).
            signature_type: 3=POLY_1271 deposit wallet (default).
            funder: Deposit wallet address. If None, derived from private key.
        """
        self.private_key = private_key
        # Derive the deterministic deposit wallet address
        self.deposit_wallet = funder or _derive_deposit_wallet(private_key)
        # Step 1: Create temp client to derive API creds
        temp_client = ClobClient(
            host=POLYMARKET_HOST,
            key=private_key,
            chain_id=CHAIN_ID,
        )
        api_creds = temp_client.derive_api_key()
        # Step 2: Create full client with L2 auth, funded by the deposit wallet
        self.client = ClobClient(
            host=POLYMARKET_HOST,
            key=private_key,
            chain_id=CHAIN_ID,
            creds=api_creds,
            signature_type=signature_type,
            funder=self.deposit_wallet,
        )
        logger.info("Deposit wallet: %s", self.deposit_wallet)

    def get_address(self) -> str:
        """Return wallet address derived from private key."""
        return self.client.get_address()

    # --- Market Data ---

    def get_orderbook(self, token_id: str) -> dict:
        """Get orderbook for a token. Returns {bids: [...], asks: [...]}."""
        return self.client.get_order_book(token_id)

    def get_spread(self, token_id: str) -> float:
        """Get spread for a token via GET /spread.

        Returns spread as float (e.g., 0.02 = 2 cents).
        Returns -1 if no orderbook exists.
        """
        try:
            resp = requests.get(
                f"{POLYMARKET_HOST}/spread",
                params={"token_id": token_id},
                timeout=10,
            )
            if resp.status_code == 404:
                return -1
            resp.raise_for_status()
            return float(resp.json().get("spread", "0"))
        except Exception as e:
            logger.warning("Failed to get spread for %s: %s", token_id, e)
            return -1

    def get_last_trade_price(self, token_id: str) -> float:
        """Get last trade price for a token."""
        resp = self.client.get_last_trade_price(token_id)
        return float(resp.get("price", 0))

    # --- Balance ---

    def get_balance(self) -> float:
        """Get pUSD (collateral) balance of the deposit wallet, in human-readable units.

        Uses update_balance_allowance which forces a fresh on-chain sync of the
        deposit wallet's balance into the CLOB cache and returns the latest value.
        Per Polymarket docs, the cached get endpoint can be stale for deposit
        wallets after funding/approving.
        """
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=SIG_POLY_1271,
        )
        bal = self.client.update_balance_allowance(params)
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
    def get_rewards_markets(
        min_price: float = None,
        max_price: float = None,
        max_spread: float = None,
        order_by: str = "rate_per_day",
        position: str = "DESC",
        max_pages: int = 5,
    ) -> list[dict]:
        """Fetch markets with active rewards using GET /rewards/markets/multi.

        Supports server-side filtering and sorting:
          - min_price/max_price: filter by first token price
          - max_spread: filter by current spread
          - order_by: sort field (rate_per_day, volume_24hr, spread, end_date, etc.)
          - position: ASC or DESC

        Returns list of dicts with keys:
          - condition_id, market_id, question, market_slug, end_date
          - tokens: [{token_id, outcome, price}, ...]
          - rewards_max_spread, rewards_min_size
          - rewards_config: [{rate_per_day, total_rewards, end_date, ...}, ...]
          - spread, volume_24hr
        """
        all_markets = []
        next_cursor = ""
        page = 0
        while True:
            page += 1
            params = {"page_size": 500}
            if min_price is not None:
                params["min_price"] = min_price
            if max_price is not None:
                params["max_price"] = max_price
            if max_spread is not None:
                params["max_spread"] = max_spread
            if order_by:
                params["order_by"] = order_by
            if position:
                params["position"] = position
            if next_cursor:
                params["next_cursor"] = next_cursor
            logger.info("Fetching rewards markets page %d...", page)

            # Retry per page
            resp_data = None
            for attempt in range(3):
                try:
                    resp = requests.get(
                        f"{REWARDS_API}/rewards/markets/multi",
                        params=params,
                        timeout=20,
                    )
                    resp.raise_for_status()
                    resp_data = resp.json()
                    break
                except Exception as e:
                    if attempt < 2:
                        logger.warning(
                            "Page %d attempt %d failed: %s, retrying...",
                            page,
                            attempt + 1,
                            e,
                        )
                        time.sleep(2)
                    else:
                        logger.error("Page %d failed after 3 attempts: %s", page, e)

            if resp_data is None:
                break

            markets = resp_data.get("data", [])
            all_markets.extend(markets)
            logger.info(
                "Page %d: got %d markets (total: %d)",
                page,
                len(markets),
                len(all_markets),
            )
            next_cursor = resp_data.get("next_cursor", "LTE=")
            if next_cursor == "LTE=" or not markets or page >= max_pages:
                break

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

    # --- Gamma API (market details) ---

    @staticmethod
    def get_market_by_id(market_id: str) -> dict:
        """Get market details from Gamma API including end_date, tokens, etc.

        Endpoint: GET https://gamma-api.polymarket.com/markets/{id}
        """
        try:
            resp = requests.get(
                f"https://gamma-api.polymarket.com/markets/{market_id}",
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Failed to get market %s from Gamma API: %s", market_id, e)
            return {}

    @staticmethod
    def list_markets(**filters) -> list[dict]:
        """List markets from Gamma API with filters.

        Endpoint: GET https://gamma-api.polymarket.com/markets
        Supports filters: active, closed, end_date_min, end_date_max, etc.
        """
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params=filters,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Failed to list markets from Gamma API: %s", e)
            return []

    # --- Per-market rewards ---

    @staticmethod
    def get_rewards_for_market(condition_id: str) -> list[dict]:
        """Get raw rewards for a specific market.

        Endpoint: GET /rewards/markets/{condition_id}
        Returns per-token reward configs with rate_per_day.
        """
        all_data = []
        next_cursor = ""
        try:
            while True:
                params = {}
                if next_cursor:
                    params["next_cursor"] = next_cursor
                resp = requests.get(
                    f"{REWARDS_API}/rewards/markets/{condition_id}",
                    params=params,
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get("data", [])
                all_data.extend(items)
                next_cursor = data.get("next_cursor", "LTE=")
                if next_cursor == "LTE=" or not items:
                    break
        except Exception as e:
            logger.error("Failed to get rewards for market %s: %s", condition_id, e)
        return all_data
