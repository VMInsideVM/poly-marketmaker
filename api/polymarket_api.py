"""api/polymarket_api.py — Polymarket CLOB + Rewards API wrapper."""

import logging
import time
import requests
from datetime import datetime, timezone
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
    OrderPayload,
    MarketOrderArgsV2,
    PartialCreateOrderOptions,
    TradeParams,
    OrdersScoringParams,
)
from py_builder_relayer_client.builder.derive import derive as _derive_safe
from py_builder_relayer_client.config import get_contract_config

logger = logging.getLogger(__name__)

POLYMARKET_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet
SIG_GNOSIS_SAFE = 2  # Browser-wallet proxy signature type

# Rewards API is part of the CLOB API
REWARDS_API = POLYMARKET_HOST
DATA_API_HOST = "https://data-api.polymarket.com"


def _end_ts_from_market(info) -> "float | None":
    """Parse a CLOB market's settlement date into a Unix timestamp.

    Reads ``end_date_iso`` (CLOB market object) and falls back to ``end_date``
    (the field name couldn't be verified offline, so both are tried). Returns
    None when info isn't a dict, both fields are absent/empty, or the value
    can't be parsed. Callers treat None as 'settlement date unknown'.
    """
    if not isinstance(info, dict):
        return None
    s = (info.get("end_date_iso") or info.get("end_date") or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    # A date-only or offset-less value parses to a naive datetime; treat it as
    # UTC so .timestamp() doesn't silently use the host's local timezone.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def derive_deposit_address(eoa_address: str) -> str:
    """Polymarket deposit-wallet (funder) address for a given signer EOA.

    Polymarket deploys a deterministic 1-of-1 Gnosis Safe per EOA via CREATE2;
    that Safe — not the EOA — holds the user's USDC/positions and is what the
    site shows as the deposit address. We derive it with the official relayer
    client so the user only ever needs to enter their private key. Matches
    signature_type=2 (POLY_GNOSIS_SAFE).
    """
    cfg = get_contract_config(CHAIN_ID)
    return _derive_safe(eoa_address, cfg.safe_factory)


class PolymarketAPI:
    """Wrapper for one wallet's Polymarket connection.

    Orders are placed from the GNOSIS_SAFE browser-wallet proxy (signature_type=2).
    """

    def __init__(
        self,
        private_key: str,
        signature_type: int = SIG_GNOSIS_SAFE,
        funder: str = None,
    ):
        """Initialize with private key.

        Args:
            private_key: Hex private key string.
            signature_type: 2=GNOSIS_SAFE (default, browser-wallet proxy).
            funder: Deposit-wallet (Gnosis Safe) address. If None, it is
                derived from the signer EOA via derive_deposit_address so the
                user only needs to provide a private key.
        """
        self.private_key = private_key
        # Step 1: Create temp client to derive API creds + the signer EOA.
        temp_client = ClobClient(
            host=POLYMARKET_HOST,
            key=private_key,
            chain_id=CHAIN_ID,
        )
        api_creds = temp_client.derive_api_key()
        # The deposit wallet (where funds live) is the Polymarket Gnosis Safe
        # deterministically derived from the EOA — NOT the EOA itself. Derive
        # it when no funder is supplied.
        eoa_address = temp_client.get_address()
        # Step 2: Create full client with L2 auth
        self.client = ClobClient(
            host=POLYMARKET_HOST,
            key=private_key,
            chain_id=CHAIN_ID,
            creds=api_creds,
            signature_type=signature_type,
            funder=funder or derive_deposit_address(eoa_address),
        )

    def get_address(self) -> str:
        """Return wallet address derived from private key."""
        return self.client.get_address()

    def get_funder(self) -> str:
        """Proxy/funder address (where funds live) used for orders/data API."""
        return self.client.builder.funder

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
        """Get pUSD (collateral) balance of the GNOSIS_SAFE proxy, in human-readable units."""
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=SIG_GNOSIS_SAFE,
        )
        bal = self.client.get_balance_allowance(params)
        raw = float(bal.get("balance", 0)) if isinstance(bal, dict) else 0.0
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

    def cancel_orders(self, order_ids: list) -> dict:
        """Batch-cancel multiple orders by ID in a single request."""
        return self.client.cancel_orders(order_ids)

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

    def get_market(self, condition_id: str) -> dict:
        """CLOB market info by condition_id (includes end_date_iso)."""
        return self.client.get_market(condition_id)

    def get_market_end_ts(self, condition_id: str) -> "float | None":
        """Settlement time (Unix seconds) for a market; None if unavailable.

        Reads end_date_iso via CLOB get_market(condition_id). Any failure /
        missing field returns None (caller treats as 'settlement unknown ->
        skip that dimension').
        """
        try:
            return _end_ts_from_market(self.get_market(condition_id))
        except Exception as e:
            logger.warning("get_market_end_ts(%s) failed: %s", condition_id, e)
            return None

    def get_trades(self, params: TradeParams = None) -> list:
        """Get trade history for this wallet (auto-paginated)."""
        return self.client.get_trades(params)

    def are_orders_scoring(self, order_ids: list) -> dict:
        """Batch-query whether orders are scoring (in reward band).

        Returns dict keyed by order id -> bool. Empty dict for empty input.
        """
        if not order_ids:
            return {}
        return self.client.are_orders_scoring(OrdersScoringParams(orderIds=order_ids))

    def get_user_positions(self, user_address: str) -> list:
        """Polymarket Data API: current positions for a user.

        Returns a list of position dicts. Confirmed fields (documented
        sample): asset (=asset_id), size, avgPrice, curPrice, conditionId
        (=market), outcome, title. user_address is the proxy/funder.
        """
        resp = requests.get(
            f"{DATA_API_HOST}/positions",
            params={"user": user_address},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

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
            # One page of 100 (sorted by rate_per_day DESC) already covers every
            # reward-qualifying market; next_cursor pagination covers the rest.
            # Do NOT combine a large page_size with min_price/max_price + order_by
            # — that combination makes this endpoint hang / 500 (see scanner.py).
            params = {"page_size": 100}
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
                        timeout=30,
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
