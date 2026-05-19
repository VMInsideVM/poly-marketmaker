"""Default configuration constants."""

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
}

DB_PATH = "market_maker.db"
HOST = "127.0.0.1"
PORT = 5000
SECRET_KEY = None  # Set at runtime from user password
LOG_BUFFER_SIZE = 1000  # max in-memory log entries for the 运行日志 page
