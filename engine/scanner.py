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
from concurrent.futures import ThreadPoolExecutor, as_completed
from engine.categories import (
    included_union,
    any_include_other,
    tag_pool,
    market_wanted,
    count_by_category,
)
from config import CATALOG_SLUGS, CATEGORY_CATALOG
from engine.strategy import reward_price_range
from api.proxy import use_proxy, current_proxy

logger = logging.getLogger(__name__)

_DISCOVERY_MAX_WORKERS = 4  # 发现阶段奖励端点并发上限(端点/代理娇气,降到 4 减轻挤压)


def _parallel_map(func, items, max_workers=_DISCOVERY_MAX_WORKERS):
    """并发对 items 跑 func,结果按输入序返回。

    关键:ThreadPoolExecutor 的 worker 线程默认 current_proxy=None(会直连、泄露真实
    IP,违反「绝不直连」铁律)。这里捕获**调用线程**的 current_proxy,在每个 worker 里
    设回,保住代理 IP 隔离。func 应自带异常处理(否则异常在收结果时抛出)。
    """
    items = list(items)
    if not items:
        return []
    proxy = current_proxy.get()

    def _task(item):
        token = current_proxy.set(proxy)
        try:
            return func(item)
        finally:
            current_proxy.reset(token)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as ex:
        return list(ex.map(_task, items))


class ScanSuperseded(Exception):
    """本轮扫描被更新一轮接管,应立即让位(合作式取消)。"""


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
        self, templates, on_progress=None, on_found=None, cancel=None
    ) -> list[dict]:
        """共享发现:抓全量奖励市场,按品类交集采集阶段排除,打 tags,补精确奖励。
        钱包无关、网络密集(rewards 端点)、不抓订单簿、不算价。"""
        union = included_union(templates)
        inc_other = any_include_other(templates)
        floors = [t.get("min_reward_usd", 0) for t in templates]
        min_floor = min(floors) if floors else 0

        full = self.api.get_rewards_markets()

        def _tag_slug(slug):  # 对整份 catalog 打标签(否则"其他"判定不准)
            try:
                rows = self.api.get_rewards_markets(tag_slug=slug)
                return slug, {m.get("condition_id", "") for m in rows}
            except Exception as e:
                # 单个品类查询失败不拖垮整轮发现(奖励端点偶发 500):该 slug 记空集,
                # 其命中市场退化为"其他"(include_other 时仍会被采集)。尤其冷启动无缓存池
                # 时,避免一次抖动导致整池空、全不下单。与 category_counts 的容错口径一致。
                logger.warning(
                    "Discovery tag_slug %s failed (treated as empty): %s", slug, e
                )
                return slug, set()

        # 14 个品类各查一次奖励端点:并发拉(原为串行,是"开始扫描到出结果"慢的一段)。
        category_ids = dict(_parallel_map(_tag_slug, CATALOG_SLUGS))

        # 顺带算一份品类计数快照(配置页勾选用),直接复用刚查到的 tag 数据,
        # 免得配置页再单独联网一遍。计数口径与 category_counts 一致(在全量集上交集)。
        self.last_catalog = self._catalog_payload(full, category_ids)

        pool = tag_pool(full, category_ids, CATALOG_SLUGS)
        blacklist = self.db.get_blacklist_ids()

        # 先无网络地筛出「想要且过粗筛」的市场(黑名单/品类/奖励地板),再**并发**拉每
        # 市场精确奖励——原来是每候选一次串行网络请求(≈一秒蹦一个),并发后成批产出。
        wanted = []
        for market in pool:
            if cancel and cancel():
                raise ScanSuperseded()
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
            wanted.append(market)

        def _precise_reward(market):
            # 精确每市场奖励(与旧 scan 一致:/rewards/markets/{cid});失败退回批量 total_rate。
            cid = market.get("condition_id", "")
            total_rate = sum(
                rc.get("rate_per_day", 0) for rc in market.get("rewards_config", [])
            )
            try:
                raw = self.api.get_rewards_for_market(cid)
                if raw:
                    return sum(
                        rc.get("rate_per_day", 0)
                        for rd in raw
                        for rc in rd.get("rewards_config", [])
                    )
            except Exception as e:
                logger.warning("Precise reward fetch failed for %s: %s", cid, e)
            return total_rate

        rewards = _parallel_map(_precise_reward, wanted)

        out = []
        for i, market in enumerate(wanted):
            if cancel and cancel():
                raise ScanSuperseded()
            cid = market.get("condition_id", "")
            self.db.upsert_market_meta(
                cid,
                market.get("question", ""),
                market.get("market_slug", ""),
                market.get("event_slug", ""),
            )
            market_reward = rewards[i]
            # 候选池展示就绪键(供 /api/eligible 与持久化显示市场名/奖励)。
            # 不含 order_price/outcome:候选池是按市场的,单价改为每钱包下单时
            # 实时计算(见 filter_for_template),前端对缺失价格以「—」兜底。
            market["market_reward"] = market_reward
            market["market_id"] = cid
            market["market_name"] = market.get("question", "")
            market["daily_reward"] = market_reward
            if on_progress:
                on_progress(
                    i + 1, len(wanted), f"Checking: {market.get('question','')}"
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

    def refresh_orderbooks(self, pool, cancel=None):
        """给候选池每个市场刷新订单簿快照(覆盖写)。钱包无关、可重复调。
        某 token 抓不到则不入该市场的 _orderbooks(filter 现有逻辑会跳过该 token),
        覆盖写保证不留上一轮的陈旧簿。

        **并发**拉:串行拉整池(~180 市场 × 每市场 4 次网络)过代理要几十分钟、下单轮
        基本跑不完,filter 拿不到簿 -> eligible 空 -> 不挂单(2026-07-04 实盘)。_fetch_
        orderbooks 自带 per-token 容错、不抛,_parallel_map 保序 + 带 current_proxy。"""
        if cancel and cancel():
            raise ScanSuperseded()
        results = _parallel_map(lambda m: self._fetch_orderbooks(m), pool)
        for market, books in zip(pool, results):
            market["_orderbooks"] = books

    def fetch_candidates(
        self,
        templates,
        on_progress=None,
        on_found=None,
        skip_orderbook=False,
        cancel=None,
    ) -> list[dict]:
        """共享采集 = 发现 + (除非 skip_orderbook)刷新订单簿。手动扫描/单测用。
        cancel(): 返回 True 时在最近的检查点抛 ScanSuperseded 让位(手动重扫接管)。"""
        pool = self.discover_candidates(
            templates, on_progress=on_progress, on_found=on_found, cancel=cancel
        )
        if not skip_orderbook:
            self.refresh_orderbooks(pool, cancel=cancel)
        return pool

    def _catalog_payload(self, full, category_ids) -> dict:
        """由「全量奖励市场 + 各 slug 命中集」组装配置页品类快照。计数在全量集上交集,
        与勾选无关;discover_candidates(扫描)与 category_counts(手动刷新)共用口径。"""
        full_ids = {m.get("condition_id", "") for m in full if m.get("condition_id")}
        narrowed = {s: category_ids.get(s, set()) & full_ids for s in CATALOG_SLUGS}
        counts, other = count_by_category(full_ids, narrowed, CATALOG_SLUGS)
        cats = [
            {"slug": c["slug"], "label": c["label"], "count": counts.get(c["slug"], 0)}
            for c in CATEGORY_CATALOG
        ]
        return {"categories": cats, "other_count": other, "ready": True}

    def category_counts(self, catalog) -> dict:
        """catalog: [{'slug','label'}]. 返回各 curated 品类在当前奖励市场的市场数 +
        「其他」数。钱包无关;各 slug 并发查 CLOB 奖励端点,与全量取交集计数。"""
        full = self.api.get_rewards_markets()
        full_ids = {m.get("condition_id", "") for m in full if m.get("condition_id")}

        # get_rewards_markets 是静态方法,靠环境 current_proxy 走代理,而 contextvar
        # 不会自动继承到线程池 worker —— 每个 worker 必须自己重设一次代理。
        proxy = getattr(self.api, "proxy_url", None)

        def _slug_ids(slug):
            with use_proxy(proxy):
                rows = self.api.get_rewards_markets(tag_slug=slug)
            return {m.get("condition_id", "") for m in rows} & full_ids

        slugs = [c["slug"] for c in catalog]
        category_ids = {}
        with ThreadPoolExecutor(max_workers=min(8, len(slugs) or 1)) as pool:
            futures = {pool.submit(_slug_ids, s): s for s in slugs}
            for fut in as_completed(futures):
                slug = futures[fut]
                try:
                    category_ids[slug] = fut.result()
                except Exception as e:
                    logger.warning("category_counts slug %s failed: %s", slug, e)
                    category_ids[slug] = set()

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
            # v4 §3:单份奖励(每日LP奖励÷最低份数) >= 该取档阈值(向上取档) -> 通过。
            # 可整体关闭(per_share_reward_enabled=False):跳过本门槛,其余筛选照常。
            if template.get("per_share_reward_enabled", True):
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
