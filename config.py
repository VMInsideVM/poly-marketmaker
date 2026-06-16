"""Default configuration constants."""

import os
import sys

# 引擎级参数:全局单值,所有钱包共用,存 settings 表。
ENGINE_DEFAULTS = {
    "scan_interval_sec": 30,
    "fill_check_interval_sec": 5,
    "cooldown_minutes": 20,
    "rewards_cache_ttl_sec": 600,
}

# 策略级参数:每钱包/每模板取值,存 template_settings 表。
TEMPLATE_DEFAULTS = {
    "min_reward_usd": 100.0,
    "max_spread_cents": 3.0,
    "min_price_cents": 10.0,
    "max_price_cents": 50.0,
    "min_settlement_days": 4,
    "stop_loss_pct": 15.0,
    "excluded_categories": ["sports", "esports", "weather"],
    # 多档挂单(SP2)
    "tiers_k": 6,
    "tier_rules": [[{"upper": None, "action": {"type": "min_size"}}] for _ in range(6)],
    "max_exposure_usd": 250,
    "max_exposure_shares": 500,
    "max_concurrent_markets": 10,
    "min_price_double_cents": 10,
}

# 向后兼容:仍暴露合并后的 DEFAULTS。get_settings() 在最后一个任务前仍以此为基准。
DEFAULTS = {**ENGINE_DEFAULTS, **TEMPLATE_DEFAULTS}


def _data_dir() -> str:
    """Directory for runtime data (database, log).

    When packaged (PyInstaller `frozen`), the app is installed into a
    location a normal user cannot write to (Program Files on Windows, the
    read-only .app bundle in /Applications on macOS) — so runtime data goes
    to a per-user writable folder: %LOCALAPPDATA% on Windows,
    ~/Library/Application Support on macOS. In development we keep the
    project directory so tests and the manual scripts behave as before.
    """
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            base = os.path.expanduser("~/Library/Application Support")
        else:
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "PolymarketMarketMaker")
        os.makedirs(path, exist_ok=True)
        return path
    return os.path.dirname(os.path.abspath(__file__))


DATA_DIR = _data_dir()
DB_PATH = os.path.join(DATA_DIR, "market_maker.db")
LOG_PATH = os.path.join(DATA_DIR, "market_maker.log")
HOST = "127.0.0.1"
PORT = 8765
SECRET_KEY = None  # Set at runtime from user password
