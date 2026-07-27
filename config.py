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
    # 0=每次实时取(在挂单市场的奖励复查要求实时);代理吃紧时可调回 600 恢复缓存。
    "rewards_cache_ttl_sec": 0,
    "discovery_interval_sec": 14400,
    # 每品类奖励抓取页数上限(每页 100)。默认 20=2000/品类,覆盖天气等大品类;
    # 扫描级全局,由 manager 透传给 scanner。
    "reward_scan_max_pages": 20,
}

# 周报中继(Cloudflare Worker)。Telegram token/chat 只存在 Worker 的环境变量里,客户端
# 一概不知道。写死在这里的两个值会随源码上 GitHub,**都不是秘密**:
#   REPORT_URL  Worker 地址,公开可见
#   REPORT_KEY  只用来把随机扫描 workers.dev 的爬虫挡在门外,不是鉴权凭证
# 真正的防线是 Worker 侧的 ENABLED 止损开关(设成 "0" 三十秒内全停、不用发版),以及「改
# Worker 不用发版」这件事本身。
# 历史:上一版把 bot token 写死在这里,被人从公开仓库扒走盗用(2026-07-27),已 revoke。
REPORT_URL = "https://polymarket-profit.lgldppst.workers.dev"
REPORT_KEY = "316e1800a8e77780d39523fc77cff12b"
PUSH_HOUR = 9  # 北京时间几点后推(8点奖励到账之后)

# 策略级参数:每钱包/每模板取值,存 template_settings 表。
TEMPLATE_DEFAULTS = {
    "min_reward_usd": 100.0,
    "max_spread_cents": 3.0,
    "min_price_cents": 10.0,
    "max_price_cents": 50.0,
    "min_settlement_days": 4,
    # 结算窗口上限(整天,0=今天/1=明天…):None=不限。与 min_settlement_days 合成
    # 窗口 [min, max];留空即沿用旧的「只有下限、无上限」行为。
    "max_settlement_days": None,
    # 跳过新建市场:创建不足 new_market_hours 小时的市场不做。默认关(升级零行为变化);
    # new_market_hours=0 视同不筛。判定在 scanner 的发现阶段与 prefilter 各做一次。
    "skip_new_markets": False,
    "new_market_hours": 24.0,
    "theta_loss_cents": 2,
    # 强平止损阈值:可按比例(占成本%)或按固定金额(美分)。默认按比例 20%。
    "stop_loss_mode": "percent",  # "percent" | "fixed"
    "stop_loss_percent": 20,  # 按比例时:成本的百分比(最大回撤)
    "theta_stop_cents": 5,  # 按固定金额时:亏损达到这么多美分即强平
    "case_a_mode": "ask",
    "gap_wide_cents": 10,
    "gap_mid_cents": 5,
    # 悬崖否决:奖励区间下沿往下这么多美分内无买档 → 该侧不挂。0=关闭。
    "cliff_probe_cents": 2,
    # 止盈方式:maker=挂卖一吃价差(默认);market=浮盈(成本<买一)立即市价清仓。
    "take_profit_mode": "maker",
    "included_categories": DEFAULT_INCLUDED_CATEGORIES,
    "include_other": True,
    "max_exposure_usd": 250,
    "max_exposure_shares": 500,
    "max_concurrent_markets": 10,
    # 低余额清仓:余额 < low_balance_threshold_usd(0=关)时按优先级逐笔市价卖持仓腾现金。
    # 档1=市场奖励 < low_reward_threshold_usd、档2=份额 < small_position_shares、档3=按亏损从小到大。
    "low_balance_threshold_usd": 4.0,
    "low_reward_threshold_usd": 30.0,
    "small_position_shares": 20.0,
    "liquidate_target_mode": "balance",  # "balance"=卖到余额线 | "next_order"=卖到够下一单
    "liquidate_target_usd": 4.0,
    # 档位模块:按市场最低奖励份额(rewards_min_size)精确匹配的挂单参数组,取代了
    # 原先的奖励最低份额范围筛选、选档系数门槛与金额数值表(均已下沉到每个档位内)。
    # 每项 {size, enabled, shares, rule1/2/3_min_coeff, gap_high_coeff_sum_min,
    # amount_value_table}。空列表 = 无档可匹配 = 不挂单。
    "size_tiers": [],
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
