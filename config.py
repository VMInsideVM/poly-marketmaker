"""Default configuration constants."""

import os
import sys

DEFAULTS = {
    "min_reward_usd": 100.0,
    "max_spread_cents": 3.0,
    "min_price_cents": 10.0,
    "max_price_cents": 50.0,
    "min_settlement_days": 4,
    "stop_loss_pct": 15.0,
    "scan_interval_sec": 30,
    "fill_check_interval_sec": 5,
    "cooldown_minutes": 20,
    "rewards_cache_ttl_sec": 600,
    "max_buy_orders_per_wallet": 5,
    "order_size_mode": "min",
    "order_size_custom_usd": 0.0,
}


def _data_dir() -> str:
    """Directory for runtime data (database, log).

    When packaged (PyInstaller `frozen`), the app is installed into
    Program Files, which a normal user cannot write to — so runtime data
    goes to a per-user writable folder under %LOCALAPPDATA%. In development
    we keep the project directory so tests and the manual scripts behave as
    before.
    """
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "PolymarketMarketMaker")
        os.makedirs(path, exist_ok=True)
        return path
    return os.path.dirname(os.path.abspath(__file__))


DATA_DIR = _data_dir()
DB_PATH = os.path.join(DATA_DIR, "market_maker.db")
LOG_PATH = os.path.join(DATA_DIR, "market_maker.log")
HOST = "127.0.0.1"
PORT = 8000
SECRET_KEY = None  # Set at runtime from user password
