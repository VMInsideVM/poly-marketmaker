"""engine/scanner.py — Market scanner: shared candidate collection + per-wallet filtering.

Two-phase design:
- fetch_candidates(templates): wallet-independent network work. Fetches all reward
  markets, tags each with the curated categories it matches (queried per tag_slug over
  the full CATALOG_SLUGS), pre-filters to markets wanted by at least one template
  (whitelist union of included_categories, plus the "other" bucket when some template
  sets include_other) and by the loosest reward floor, fetches precise per-market
  reward, and caches each token's orderbook. Does NOT compute order prices.
- filter_for_template(pool, template, wallet): per-wallet CPU work. Applies the
  template's thresholds (reward floor, settlement, price band, spread), keeps only
  markets matching the template's included_categories (or untagged when include_other),
  checks cooldown. Does NOT compute prices or costs —
  pricing and sizing happen at placement time (place_orders).

Optional on_progress(checked, total, msg) / on_found(market) callbacks fire per
candidate during fetch_candidates.
"""

import re
import time
import logging
from datetime import datetime
from engine.categories import (
    included_union,
    any_include_other,
    tag_pool,
    market_wanted,
    count_by_category,
)
from config import CATALOG_SLUGS
from engine.strategy import reward_price_range

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


def reward_bracket(min_size):
    """向上取档(更保守):返回 20/50/100/200/250;超 250 或 <=0 返回 None。

    v4 §2:仅服务市场筛选,不参与挂单/离场。
    """
    if min_size <= 0:
        return None
    for b in (20, 50, 100, 200, 250):
        if min_size <= b:
            return b
    return None


class MarketScanner:
    def __init__(self, api, db, wallet_address: str):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address

    def discover_candidates(
        self, templates, on_progress=None, on_found=None
    ) -> list[dict]:
        """共享发现:抓全量奖励市场,按品类交集采集阶段排除,打 tags,补精确奖励。
        钱包无关、网络密集(rewards 端点)、不抓订单簿、不算价。"""
        union = included_union(templates)
        inc_other = any_include_other(templates)
        floors = [t.get("min_reward_usd", 0) for t in templates]
        min_floor = min(floors) if floors else 0

        full = self.api.get_rewards_markets()
        category_ids = {}
        for slug in CATALOG_SLUGS:  # 对整份 catalog 打标签(否则"其他"判定不准)
            try:
                rows = self.api.get_rewards_markets(tag_slug=slug)
                category_ids[slug] = {m.get("condition_id", "") for m in rows}
            except Exception as e:
                # 单个品类查询失败不拖垮整轮发现(奖励端点偶发 500):该 slug 记空集,
                # 其命中市场退化为"其他"(include_other 时仍会被采集)。尤其冷启动无缓存池
                # 时,避免一次抖动导致整池空、全不下单。与 category_counts 的容错口径一致。
                logger.warning(
                    "Discovery tag_slug %s failed (treated as empty): %s", slug, e
                )
                category_ids[slug] = set()

        pool = tag_pool(full, category_ids, CATALOG_SLUGS)
        blacklist = self.db.get_blacklist_ids()

        out = []
        checked = 0
        for market in pool:
            cid = market.get("condition_id", "")
            if cid in blacklist:
                continue
            if not market_wanted(market.get("tags", []), union, inc_other):
                continue  # 没被任何模板 include(且非其他)-> 不做昂贵的精确奖励拉取
            total_rate = sum(
                rc.get("rate_per_day", 0) for rc in market.get("rewards_config", [])
            )
            if total_rate < min_floor:
                continue  # 比最宽松模板还低,任何模板都不会要
            self.db.upsert_market_meta(
                cid,
                market.get("question", ""),
                market.get("market_slug", ""),
                market.get("event_slug", ""),
            )
            # 精确每市场奖励(与旧 scan 一致:/rewards/markets/{cid})
            market_reward = total_rate
            try:
                raw = self.api.get_rewards_for_market(cid)
                if raw:
                    market_reward = sum(
                        rc.get("rate_per_day", 0)
                        for rd in raw
                        for rc in rd.get("rewards_config", [])
                    )
            except Exception as e:
                logger.warning("Precise reward fetch failed for %s: %s", cid, e)
            market["market_reward"] = market_reward
            # 候选池展示就绪键(供 /api/eligible 与持久化显示市场名/奖励)。
            # 不含 order_price/outcome:候选池是按市场的,单价改为每钱包下单时
            # 实时计算(见 filter_for_template),前端对缺失价格以「—」兜底。
            market["market_id"] = cid
            market["market_name"] = market.get("question", "")
            market["daily_reward"] = market_reward
            checked += 1
            if on_progress:
                on_progress(
                    checked, len(pool), f"Checking: {market.get('question','')}"
                )
            if on_found:
                on_found(market)
            out.append(market)
        logger.info(
            "discover_candidates: %d candidates (included union %d categories)",
            len(out),
            len(union),
        )
        return out

    def refresh_orderbooks(self, pool):
        """给候选池每个市场刷新订单簿快照(覆盖写)。钱包无关、可重复调。
        某 token 抓不到则不入该市场的 _orderbooks(filter 现有逻辑会跳过该 token),
        覆盖写保证不留上一轮的陈旧簿。"""
        for market in pool:
            market["_orderbooks"] = self._fetch_orderbooks(market)

    def fetch_candidates(
        self, templates, on_progress=None, on_found=None, skip_orderbook=False
    ) -> list[dict]:
        """共享采集 = 发现 + (除非 skip_orderbook)刷新订单簿。手动扫描/单测用。"""
        pool = self.discover_candidates(
            templates, on_progress=on_progress, on_found=on_found
        )
        if not skip_orderbook:
            self.refresh_orderbooks(pool)
        return pool

    def category_counts(self, catalog) -> dict:
        """catalog: [{'slug','label'}]. 返回各 curated 品类在当前奖励市场的市场数 +
        「其他」数。钱包无关;逐 slug 查 CLOB 奖励端点,与全量取交集计数。"""
        full = self.api.get_rewards_markets()
        full_ids = {m.get("condition_id", "") for m in full if m.get("condition_id")}
        category_ids = {}
        for c in catalog:
            slug = c["slug"]
            try:
                rows = self.api.get_rewards_markets(tag_slug=slug)
                category_ids[slug] = {
                    m.get("condition_id", "") for m in rows
                } & full_ids
            except Exception as e:
                logger.warning("category_counts slug %s failed: %s", slug, e)
                category_ids[slug] = set()
        slugs = [c["slug"] for c in catalog]
        counts, other = count_by_category(full_ids, category_ids, slugs)
        cats = [
            {"slug": c["slug"], "label": c["label"], "count": counts.get(c["slug"], 0)}
            for c in catalog
        ]
        return {"categories": cats, "other_count": other}

    def _fetch_orderbooks(self, market: dict) -> dict:
        """抓该市场每 token 的订单簿快照(钱包无关)。抓不到的略过。"""
        books = {}
        for token in market.get("tokens", []):
            token_id = token.get("token_id", "")
            if not token_id:
                continue
            try:
                spread_val = self.api.get_spread(token_id)
                ob = self.api.get_orderbook(token_id)
            except Exception as e:
                logger.warning("Orderbook fetch failed for %s: %s", token_id, e)
                continue
            books[token_id] = {
                "bids": ob.get("bids", []),
                "asks": ob.get("asks", []),
                "tick_size": ob.get("tick_size", "0.01"),
                "spread": spread_val,
            }
        return books

    def filter_for_template(
        self, candidate_pool, template, wallet_address
    ) -> list[dict]:
        """从候选池产出某模板的 eligible(门槛过滤 + 品类 narrow + 老算法定价)。"""
        min_reward = template["min_reward_usd"]
        min_price_cents = template["min_price_cents"]
        max_price_cents = template["max_price_cents"]
        max_spread_cents = template["max_spread_cents"]
        min_days = template["min_settlement_days"]
        included = set(template.get("included_categories", []) or [])
        include_other = bool(template.get("include_other", False))

        eligible = []
        for market in candidate_pool:
            if not market_wanted(market.get("tags", []), included, include_other):
                continue
            total_rate = sum(
                rc.get("rate_per_day", 0) for rc in market.get("rewards_config", [])
            )
            market_reward = market.get("market_reward", total_rate)
            if total_rate < min_reward or market_reward < min_reward:
                continue
            end_date_str = market.get("end_date", "")
            end_ts = _parse_end_date(end_date_str)
            days_left = (end_ts - time.time()) / 86400 if end_ts else -1
            if 0 <= days_left < min_days:
                continue

            condition_id = market.get("condition_id", "")
            if self.db.is_in_cooldown(wallet_address, condition_id):
                continue  # 该钱包对此市场仍在冷却(与旧 scan 口径一致)
            max_spread_reward = float(market.get("rewards_max_spread", 2))
            min_size = int(market.get("rewards_min_size", 0) or 0)
            # 最低份额范围筛选(可配);硬顶 250(超档无取档)。默认 1/250 = 放行全部合法档。
            size_lo = max(1, int(template.get("rewards_min_size_min", 1) or 1))
            size_hi = min(250, int(template.get("rewards_min_size_max", 250) or 250))
            if not (size_lo <= min_size <= size_hi):
                continue
            # v4 §3:单份奖励(每日LP奖励÷最低份数) >= 该取档阈值(向上取档) -> 通过
            bracket = reward_bracket(min_size)
            per_share = market_reward / min_size
            thresholds = template.get("per_share_reward_thresholds", {})
            if per_share < float(thresholds.get(str(bracket), 0.30)):
                continue
            neg_risk = market.get("neg_risk", False)
            books = market.get("_orderbooks", {})
            valid_tokens = [
                t
                for t in market.get("tokens", [])
                if min_price_cents <= float(t.get("price", 0)) * 100 <= max_price_cents
            ]
            for token in valid_tokens:
                token_id = token.get("token_id", "")
                book = books.get(token_id)
                if not book:
                    continue
                spread_val = book.get("spread", -1)
                if spread_val < 0 or spread_val * 100 >= max_spread_cents:
                    continue
                bids = sorted(
                    book.get("bids", []), key=lambda x: float(x["price"]), reverse=True
                )
                asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
                if not bids or not asks:
                    continue
                best_bid = float(bids[0]["price"])
                best_ask = float(asks[0]["price"])
                if best_bid * 100 < min_price_cents or best_bid * 100 > max_price_cents:
                    continue
                tick_size_str = book.get("tick_size", "0.01")
                midpoint = (best_bid + best_ask) / 2
                rmin, rmax = reward_price_range(midpoint, max_spread_reward)
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
                        "tick_size_str": tick_size_str,
                        "neg_risk": neg_risk,
                        "tags": market.get("tags", []),
                        "reward_range_min": rmin,
                        "reward_range_max": rmax,
                        "spread_cents": spread_val * 100,
                    }
                )
        logger.info(
            "filter_for_template(%s): %d eligible from %d candidates",
            wallet_address,
            len(eligible),
            len(candidate_pool),
        )
        return eligible
