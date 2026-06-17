"""engine/scanner.py — Market scanner: shared candidate collection + per-wallet filtering.

Two-phase design:
- fetch_candidates(templates): wallet-independent network work. Fetches all reward
  markets, excludes blacklisted-category markets at collection time (the category
  intersection across templates), tags survivors, pre-filters by the loosest reward
  floor, fetches precise per-market reward, and caches each token's orderbook. Does
  NOT compute order prices.
- filter_for_template(pool, template, wallet): per-wallet CPU work. Applies the
  template's thresholds (reward floor, settlement, price band, spread), narrows by the
  template's excluded_categories, checks cooldown. Does NOT compute prices or costs —
  pricing and sizing happen at placement time (place_orders).

Optional on_progress(checked, total, msg) / on_found(market) callbacks fire per
candidate during fetch_candidates.
"""

import re
import time
import logging
from datetime import datetime
from engine.categories import (
    excluded_intersection,
    queried_categories,
    partition_candidates,
)
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
        inter = excluded_intersection(templates)
        queried = queried_categories(templates)
        floors = [t.get("min_reward_usd", 0) for t in templates]
        min_floor = min(floors) if floors else 0

        full = self.api.get_rewards_markets()
        category_ids = {}
        for slug in queried:
            rows = self.api.get_rewards_markets(tag_slug=slug)
            category_ids[slug] = {m.get("condition_id", "") for m in rows}

        pool = partition_candidates(full, category_ids, inter)
        blacklist = self.db.get_blacklist_ids()

        out = []
        checked = 0
        for market in pool:
            cid = market.get("condition_id", "")
            if cid in blacklist:
                continue
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
            "discover_candidates: %d candidates (queried %d categories)",
            len(out),
            len(queried),
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
        excluded = set(template.get("excluded_categories", []) or [])

        eligible = []
        for market in candidate_pool:
            if excluded & set(market.get("tags", [])):
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
            min_size = int(market.get("rewards_min_size", 0))
            # v4 §3:最低份额 0 < ≤ 250;超档无取档 -> 不做该市场
            if not (0 < min_size <= 250):
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
