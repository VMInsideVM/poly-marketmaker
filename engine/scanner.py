"""engine/scanner.py — Market scanner that filters eligible markets.

Filtering flow (matches test_live.py logic):
1. /rewards/markets/multi — fetch markets sorted by rate_per_day DESC
2. Filter: total rate_per_day >= min_reward (from rewards_config sum)
3. Filter: settlement date (only exclude 0~4 days, negative = pass)
4. /rewards/markets/{condition_id} — get precise per-market reward
5. Filter: at least one token price in [min_price, max_price]
6. GET /spread — fast spread check per token
7. GET /book — full orderbook for strategy calculation
"""

import re
import time
import logging
from datetime import datetime
from engine.strategy import determine_order_price, reward_price_range
from engine.take_profit import ceil_to_tick
from engine.categories import (
    excluded_intersection,
    queried_categories,
    partition_candidates,
)

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


class MarketScanner:
    def __init__(self, api, db, wallet_address: str):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address

    def fetch_candidates(
        self, templates, on_progress=None, on_found=None, skip_orderbook=False
    ) -> list[dict]:
        """共享采集:抓全量奖励市场,按品类交集采集阶段排除,打 tags,补精确奖励,
        缓存订单簿。钱包无关、网络密集、不算价。skip_orderbook 仅供单测。"""
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
            if not skip_orderbook:
                market["_orderbooks"] = self._fetch_orderbooks(market)
            checked += 1
            if on_progress:
                on_progress(
                    checked, len(pool), f"Checking: {market.get('question','')}"
                )
            if on_found:
                on_found(market)
            out.append(market)
        return out

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
                tick_size = float(tick_size_str)
                midpoint = (best_bid + best_ask) / 2
                reward_range_min, reward_range_max = reward_price_range(
                    midpoint, max_spread_reward
                )
                min_cost = min_size * ceil_to_tick(
                    max(reward_range_min, 0.0), tick_size
                )
                try:
                    order_price = determine_order_price(
                        bids=bids,
                        max_spread=int(max_spread_reward),
                        tick_size=tick_size,
                        reward_range_min=reward_range_min,
                        reward_range_max=reward_range_max,
                    )
                except Exception as e:
                    logger.warning("Strategy error for %s: %s", condition_id, e)
                    continue
                if order_price is None:
                    continue
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
                        "tick_size": tick_size,
                        "tick_size_str": tick_size_str,
                        "neg_risk": neg_risk,
                        "reward_range_min": reward_range_min,
                        "reward_range_max": reward_range_max,
                        "order_price": order_price,
                        "order_size": min_size,
                        "min_cost": min_cost,
                        "tags": market.get("tags", []),
                    }
                )
        return eligible

    def scan(self, on_progress=None, on_found=None) -> list[dict]:
        """兼容 shim:用默认模板采集 + 精筛(老入口,供单模板路径与既有测试)。"""
        tmpl = self.db.get_template(self.db.get_default_template_id())
        pool = self.fetch_candidates([tmpl], on_progress=on_progress, on_found=on_found)
        return self.filter_for_template(pool, tmpl, self.wallet_address)
