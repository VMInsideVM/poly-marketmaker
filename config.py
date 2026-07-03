"""Default configuration constants."""

import os
import sys

# curated 品类名单(白名单勾选来源)。slug 须是 CLOB /rewards/markets/multi 的 tag_slug
# 可识别值(已实测 politics/geopolitics/world/elections/economy/finance/crypto/sports/
# esports/games/weather/ai/tech/culture 均返回合理子集)。顺序即配置页展示顺序。
CATEGORY_CATALOG = [
    {"slug": "politics", "label": "政治"},
    {"slug": "geopolitics", "label": "地缘政治"},
    {"slug": "world", "label": "国际"},
    {"slug": "elections", "label": "选举"},
    {"slug": "economy", "label": "经济"},
    {"slug": "finance", "label": "金融"},
    {"slug": "crypto", "label": "加密货币"},
    {"slug": "sports", "label": "体育"},
    {"slug": "esports", "label": "电竞"},
    {"slug": "games", "label": "游戏"},
    {"slug": "weather", "label": "天气"},
    {"slug": "ai", "label": "人工智能"},
    {"slug": "tech", "label": "科技"},
    {"slug": "culture", "label": "文化"},
]
CATALOG_SLUGS = frozenset(c["slug"] for c in CATEGORY_CATALOG)
# 默认延续升级前行为:体育/电竞/天气不做,其余(含未分类)都做。
_DEFAULT_EXCLUDED = {"sports", "esports", "weather"}
DEFAULT_INCLUDED_CATEGORIES = [
    c["slug"] for c in CATEGORY_CATALOG if c["slug"] not in _DEFAULT_EXCLUDED
]

# 引擎级参数:全局单值,所有钱包共用,存 settings 表。
ENGINE_DEFAULTS = {
    "scan_interval_sec": 30,
    "fill_check_interval_sec": 5,
    "cooldown_minutes": 20,
    "rewards_cache_ttl_sec": 600,
    "discovery_interval_sec": 14400,
}

# 策略级参数:每钱包/每模板取值,存 template_settings 表。
TEMPLATE_DEFAULTS = {
    "min_reward_usd": 100.0,
    "max_spread_cents": 3.0,
    "min_price_cents": 10.0,
    "max_price_cents": 50.0,
    "min_settlement_days": 4,
    "theta_loss_cents": 2,
    # 强平止损阈值:可按比例(占成本%)或按固定金额(美分)。默认按比例 20%。
    "stop_loss_mode": "percent",  # "percent" | "fixed"
    "stop_loss_percent": 20,  # 按比例时:成本的百分比(最大回撤)
    "theta_stop_cents": 5,  # 按固定金额时:亏损达到这么多美分即强平
    "case_a_mode": "ask",
    # 挂单模式(v4 用户策略):laddering=多档做市(默认);gap_single=断层分级单档。
    "placement_mode": "laddering",
    "gap_wide_cents": 10,
    "gap_mid_cents": 5,
    "gap_high_coeff_sum_min": 20,
    # 三级各自的选档系数门槛(>门槛才挂):规则1=宽断层、规则2=中断层、规则3=密盘。
    "rule1_min_coeff": 0,
    "rule2_min_coeff": 0,
    "rule3_min_coeff": 0,
    # 止盈方式:maker=挂卖一吃价差(默认);market=浮盈(成本<买一)立即市价清仓。
    "take_profit_mode": "maker",
    "included_categories": DEFAULT_INCLUDED_CATEGORIES,
    "include_other": True,
    # 多档挂单(SP2)
    "tier_rules": [[{"upper": None, "action": {"type": "min_size"}}] for _ in range(6)],
    "max_exposure_usd": 250,
    "max_exposure_shares": 500,
    "max_concurrent_markets": 10,
    "min_price_double_cents": 10,
    # 单份奖励阈值筛选总开关(每模板);False=整段跳过,其余筛选不受影响。
    "per_share_reward_enabled": True,
    # 单份奖励阈值档位(SP4)
    "per_share_reward_thresholds": {
        "20": 0.30,
        "50": 0.30,
        "100": 0.30,
        "200": 0.30,
        "250": 0.30,
    },
    # 奖励最低份额范围筛选(向上取档硬顶 250)
    "rewards_min_size_min": 1,
    "rewards_min_size_max": 250,
    # 每档区间匹配变量 + 金额数值表(风险系数模式用)
    "tier_match_var": "cumulative_thickness",  # "cumulative_thickness" | "risk_coefficient"
    "amount_value_table": [
        {"upper": 0.20, "value": 1},
        {"upper": 0.25, "value": 1.5},
        {"upper": 0.31, "value": 2},
    ],
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
