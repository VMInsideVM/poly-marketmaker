"""engine/monitor.py — API-driven fill detection, stop-loss, strategy compliance."""

import logging
import time
from py_clob_client_v2.clob_types import TradeParams
from engine.fills import select_new_buy_fills, extract_buy_fills
from engine.take_profit import (
    plan_take_profit,
    cost_basis_from_buy_fills,
    take_profit_price,
)
from engine.strategy_check import needs_replace
from engine.eligibility import recheck_resting_buy
from engine.risk import stop_loss_triggered
from engine.strategy import determine_order_price
from engine.rewards import extract_max_spread
from engine import monitor_status

logger = logging.getLogger(__name__)


class OrderMonitor:
    def __init__(self, api, db, wallet_address: str):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address
        # Dedup processed buy fills by (trade_id, order_id).
        self._seen_fill_keys: set = set()
        # Watermark: lower bound for get_trades(after=) — bounds fetch size;
        # real idempotency is _seen_fill_keys.
        self._after_ts: float = 0.0
        # condition_id -> (max_spread, fetched_at) TTL cache for Step 3.
        self._max_spread_cache: dict = {}
        self._status_rows: list = []
        self._tick_ts: float = 0.0
        self._cost_cache: dict = {}  # asset_id -> 加权成本 or None(每 tick 重置)

    def begin_status_tick(self) -> None:
        self._status_rows = []
        self._tick_ts = time.time()
        self._cost_cache = {}

    def _status_add(self, **fields) -> None:
        try:
            row = {"ts": self._tick_ts, "wallet": self.wallet_address}
            row.update(fields)
            self._status_rows.append(row)
        except Exception as e:  # never break Step1/2/3
            logger.warning("status_add failed: %s", e)

    def publish_status(self) -> None:
        try:
            monitor_status.set_snapshot(
                self.wallet_address, self._status_rows, self._tick_ts
            )
        except Exception as e:
            logger.warning("publish_status failed: %s", e)

    def _record_action(
        self, market_id, action_type, side, price, size, reason, price_basis
    ) -> None:
        """Persist one order-mutating action. Never breaks Step1/2/3."""
        try:
            self.db.record_action(
                wallet=self.wallet_address,
                market_id=market_id,
                action_type=action_type,
                side=side,
                price=price,
                size=size,
                reason=reason,
                price_basis=price_basis,
            )
        except Exception as e:
            logger.warning("record_action failed: %s", e)

    def init_watermark(self):
        """Seed watermark from latest recorded trade/action ts for this wallet.

        DB created_at is local record time; trade match_time is exchange
        time. Both ~unix seconds; this is only a conservative lower bound,
        (trade_id, order_id) dedup guarantees no double-processing. Actions are
        included so that clearing the trades table does not reset the watermark
        to 0 (which would refetch all history and re-cooldown old markets).
        """
        rows = self.db.get_trade_history(self.wallet_address)
        actions = self.db.get_actions(self.wallet_address)
        times = [r.get("created_at", 0) or 0 for r in rows]
        times += [a.get("created_at", 0) or 0 for a in actions]
        self._after_ts = max(times) if times else 0.0

    def _funder(self) -> str:
        """Proxy/funder address — used for get_trades maker filter and Data API."""
        return self.api.get_funder()

    def _cost(self, asset_id: str, size: float):
        """该持仓的加权成本(本 tick 缓存)。来自 CLOB get_trades 的真实买入成交,
        替代 Data API avgPrice。取不到 -> None(调用方据此跳过,不在不确定成本上动手)。
        结果按 asset_id 缓存至本 tick 结束(size 在 tick 内视为稳定,止盈与止损作用于同一持仓快照)。"""
        if asset_id in self._cost_cache:
            return self._cost_cache[asset_id]
        funder = self._funder()
        try:
            # 不传 maker_address:服务端返回本钱包两种角色的成交,使我们当 taker 的
            # 买入也进入加权成本(extract_buy_fills 内部仍按 funder 过滤 maker_orders)。
            trades = self.api.get_trades(TradeParams(asset_id=asset_id))
        except Exception as e:
            logger.warning("get_trades(asset=%s) for cost failed: %s", asset_id, e)
            self._cost_cache[asset_id] = None
            return None
        fills = extract_buy_fills(trades, funder, asset_id)
        cost = cost_basis_from_buy_fills(fills, size)
        self._cost_cache[asset_id] = cost
        return cost

    def _cost_with_source(self, asset_id: str, size: float, avg_fallback: float):
        """成本 + 来源。优先 get_trades 加权成本;取不到(None/<=0)且 avg_fallback>0
        时回落 Data API avgPrice。返回 (cost_or_None, source_str)。
        门控:get_trades 有成本时永远不碰 avgPrice。
        注意:止盈调用方有穿价护栏兜底;止损调用方为市价平仓、无护栏,
        使用 avgPrice 兜底属已接受风险(avgPrice 读高可能误触发)。"""
        cost = self._cost(asset_id, size)  # get_trades 加权(本 tick 缓存)或 None
        if cost is not None and cost > 0:
            return cost, "get_trades加权"
        if avg_fallback > 0:
            return float(avg_fallback), "avgPrice兜底"
        return None, ""

    # --- Step 1: fills via get_trades (flatten maker_orders) ---
    def check_buy_orders(self):
        funder = self._funder()
        try:
            params = TradeParams(
                maker_address=funder,
                after=(str(int(self._after_ts)) if self._after_ts else None),
            )
            trades = self.api.get_trades(params)
        except Exception as e:
            logger.error("get_trades failed for %s: %s", self.wallet_address, e)
            return
        fills = select_new_buy_fills(trades, funder, self._seen_fill_keys)
        cancelled_orders: set = set()
        for ev in fills:
            try:
                self._handle_fill(ev, cancelled_orders)
            except Exception as e:
                logger.error(
                    "Error handling fill %s/%s: %s",
                    ev.get("trade_id"),
                    ev.get("order_id"),
                    e,
                )
            finally:
                self._seen_fill_keys.add((ev.get("trade_id"), ev.get("order_id")))
                self._after_ts = max(self._after_ts, float(ev.get("ts", 0) or 0))

    def _handle_fill(self, ev: dict, cancelled_orders: set):
        size = float(ev.get("size", 0) or 0)  # real data is fractional
        price = float(ev.get("price", 0) or 0)
        market_id = ev.get("market", "")
        order_id = ev.get("order_id")
        if size <= 0:
            return
        # No trades-table write: buy "history" is now the live Data API position
        # (avgPrice), because per-fill maker_orders prices were unreliable. The
        # take-profit SELL is maintained by check_take_profit() priced off the
        # get_trades weighted cost with a 穿价护栏 (max(cost, best_bid+tick)),
        # not avgPrice. Step 1 only sets the cooldown and cancels the buy's remainder.
        self.db.set_cooldown(
            self.wallet_address, market_id, self.db.get_settings()["cooldown_minutes"]
        )
        if order_id and order_id not in cancelled_orders:
            try:
                self.api.cancel_orders([order_id])
                cancelled_orders.add(order_id)
                self._record_action(
                    market_id=market_id,
                    action_type="cancel_remainder",
                    side="-",
                    price=-1,
                    size=size,  # fill size; remaining unfilled qty not known here
                    reason="该买单已成交，撤销同一买单剩余未成交量，避免超买",
                    price_basis=f"撤 order_id={order_id}；撤单操作无价格",
                )
            except Exception as e:
                logger.warning("Cancel remainder of %s failed: %s", order_id, e)
        self._status_add(
            market=market_id,
            side="买入",
            price=f"{price:.4f}",
            size=str(size),
            matched=str(size),
            stage="Step1",
            action="成交→撤余单",
            detail=f"成交{size}，止盈由持仓维护",
        )

    # --- Step 1b: position-driven take-profit (one resting sell at cost) ---
    def check_take_profit(self):
        """Maintain exactly one resting SELL per position at a cost-based price.

        Cost is the weighted average of our real CLOB get_trades buy fills (via
        _cost); the Data API position is used only for size. The sell price adds
        a 穿价护栏 (max(cost, best_bid+tick)) so it always rests as a maker and
        never crosses the book. Replaces both the old per-fill sells and the
        later Data-API-avgPrice approach (avgPrice glitched on fresh positions).
        """
        funder = self._funder()
        try:
            positions = self.api.get_user_positions(funder)
        except Exception as e:
            logger.warning(
                "Data API positions failed for %s (skip take-profit): %s",
                self.wallet_address,
                e,
            )
            return
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed for %s: %s", self.wallet_address, e)
            return
        for pos in positions:
            try:
                self._reconcile_take_profit(pos, open_orders)
            except Exception as e:
                logger.error("Take-profit error on %s: %s", pos.get("asset"), e)

    def _sell_book(self, asset_id: str):
        """(tick_float, tick_str, best_bid) for an asset;
        失败/盘口空时回 (0.01, "0.01", None)。"""
        try:
            ob = self.api.get_orderbook(asset_id)
            tick_str = ob.get("tick_size", "0.01")
            bids = sorted(
                ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True
            )
            best_bid = float(bids[0]["price"]) if bids else None
            return float(tick_str), tick_str, best_bid
        except Exception as e:
            logger.warning(
                "orderbook for %s failed (take-profit tick=0.01): %s", asset_id, e
            )
            return 0.01, "0.01", None

    def _reconcile_take_profit(self, pos: dict, open_orders: list):
        asset_id = pos.get("asset", "")
        size = float(pos.get("size", 0) or 0)
        cid = pos.get("conditionId", "")
        if size <= 0:
            return
        avg_fallback = float(pos.get("avgPrice", 0) or 0)
        cost, source = self._cost_with_source(asset_id, size, avg_fallback)
        if cost is None or cost <= 0:
            self._status_add(
                market=cid,
                side="卖出",
                price="-",
                size=str(size),
                matched="-",
                stage="止盈卖单",
                action="跳过(无成交数据)",
                detail="get_trades 无买入成交且无 avgPrice，保持现有卖单不动",
            )
            return
        tick, tick_str, best_bid = self._sell_book(asset_id)
        want = take_profit_price(cost, best_bid, tick)
        sells = [
            o
            for o in open_orders
            if o.get("asset_id") == asset_id and o.get("side") == "SELL"
        ]
        plan = plan_take_profit(size, want, tick, sells)
        if plan["action"] in ("noop", "keep"):
            self._status_add(
                market=cid,
                side="卖出",
                price=f"{want:.4f}",
                size=str(size),
                matched="-",
                stage="止盈卖单",
                action="保持(成本/护栏价)",
                detail=f"成本{cost:.4f} 持仓{size} 已挂一笔 {want:.4f}",
            )
            return
        if plan["cancel_ids"]:
            try:
                self.api.cancel_orders(plan["cancel_ids"])
                self._record_action(
                    market_id=cid,
                    action_type="take_profit_recancel",
                    side="-",
                    price=-1,
                    size=size,
                    reason="撤销与持仓不符的旧止盈卖单（价格/数量不符或被拆成多笔），改为按持仓挂单一笔",
                    price_basis=(
                        f"撤 {len(plan['cancel_ids'])} 笔 SELL；"
                        f"来源：CLOB get_open_orders（asset={asset_id} 的 SELL）"
                    ),
                )
            except Exception as e:
                logger.warning("Cancel stale sells for %s failed: %s", asset_id, e)
                return
        try:
            self.api.place_limit_sell(asset_id, want, size, tick_size=tick_str)
        except Exception as e:
            logger.warning("Place take-profit sell for %s failed: %s", asset_id, e)
            return
        self._record_action(
            market_id=cid,
            action_type="take_profit_sell",
            side="卖出",
            price=want,
            size=size,
            reason="按真实成交加权成本挂止盈卖单，并加穿价护栏（不亏本金、不穿价市价清仓、赚流动性奖励）",
            price_basis=(
                f"成本={source} {cost:.4f}；卖价=max(成本,买一+1tick)={want:.4f}；"
                f"来源：{'CLOB get_trades' if source == 'get_trades加权' else 'Data API avgPrice(兜底)'} + get_orderbook"
            ),
        )
        self._status_add(
            market=cid,
            side="卖出",
            price=f"{want:.4f}",
            size=str(size),
            matched="-",
            stage="止盈卖单",
            action="按成本挂单",
            detail=f"成本{cost:.4f} 持仓{size} 挂卖{want:.4f}",
        )

    # --- Step 2: stop-loss via Data API positions ---
    def check_stop_loss(self):
        settings = self.db.get_settings()
        try:
            positions = self.api.get_user_positions(self._funder())
        except Exception as e:
            logger.warning(
                "Data API positions failed for %s (skip stop-loss): %s",
                self.wallet_address,
                e,
            )
            return
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed for %s: %s", self.wallet_address, e)
            open_orders = []
        for pos in positions:
            try:
                self._check_pos_sl(pos, open_orders, settings)
            except Exception as e:
                logger.error("Stop-loss error on %s: %s", pos.get("asset"), e)

    def _check_pos_sl(self, pos: dict, open_orders: list, settings: dict):
        # Confirmed Data API position fields: asset / size / avgPrice /
        # curPrice / conditionId.
        asset_id = pos.get("asset", "")
        size = float(pos.get("size", 0) or 0)
        cur = float(pos.get("curPrice", 0) or 0)
        if size <= 0:
            return
        avg_fallback = float(pos.get("avgPrice", 0) or 0)
        avg, source = self._cost_with_source(asset_id, size, avg_fallback)
        # get_trades 加权成本优先;取不到时回落 Data API avgPrice。
        # 已知风险(已接受):市价平仓无穿价护栏,avgPrice 读高可能误触发。
        if avg is None or avg <= 0:
            return
        if not stop_loss_triggered(cur, avg, settings["stop_loss_pct"]):
            return
        sell_ids = [
            o["id"]
            for o in open_orders
            if o.get("asset_id") == asset_id and o.get("side") == "SELL"
        ]
        cid = pos.get("conditionId", "")
        if sell_ids:
            try:
                self.api.cancel_orders(sell_ids)
                self._record_action(
                    market_id=cid,
                    action_type="stoploss_cancel_sell",
                    side="-",
                    price=-1,
                    size=size,
                    reason="触发止损，先撤该持仓全部止盈卖单以便市价平仓",
                    price_basis=f"撤 {len(sell_ids)} 笔 SELL；来源：CLOB get_open_orders（asset={asset_id} 的 SELL）",
                )
            except Exception as e:
                logger.warning("Cancel sell orders for %s failed: %s", asset_id, e)
        self.api.place_market_sell(asset_id, size)
        self.db.record_trade(
            wallet=self.wallet_address,
            market_id=cid,
            market_name="",
            side="stop_loss",
            price=cur,
            size=size,
            pnl=(cur - avg) * size,
        )
        self._record_action(
            market_id=cid,
            action_type="stoploss_market_sell",
            side="卖出",
            price=cur,
            size=size,
            reason=f"现价 {cur:.4f} 跌破成本价 {avg:.4f} 的止损阈值 avg×(1-止损比例{settings['stop_loss_pct']}%)，市价平仓止损",
            price_basis=(
                f"成本价={source} {avg:.4f}、现价 curPrice={cur:.4f}；"
                f"来源：{'CLOB get_trades' if source == 'get_trades加权' else 'Data API avgPrice(兜底)'} + Data API /positions"
            ),
        )
        logger.warning(
            "Stop-loss executed: asset=%s size=%s cur=%.4f avg=%.4f",
            asset_id,
            size,
            cur,
            avg,
        )
        self._status_add(
            market=cid,
            side="卖出",
            price=f"{cur:.4f}",
            size=str(size),
            matched="-",
            stage="Step2",
            action="止损→市价平仓",
            detail=f"cur{cur:.4f}<avg{avg:.4f} 触发",
        )

    # --- Step 3: strategy compliance on resting buy orders ---
    def check_sell_orders(self):
        """Reused tick name kept for the manager loop; runs strategy compliance."""
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed for %s: %s", self.wallet_address, e)
            return
        for o in open_orders:
            price_s = f"{float(o.get('price', 0) or 0):.4f}"
            size_s = str(o.get("original_size", ""))
            matched_s = str(o.get("size_matched", "0"))
            if o.get("side") != "BUY":
                self._status_add(
                    market=o.get("market", ""),
                    side="卖出",
                    price=price_s,
                    size=size_s,
                    matched=matched_s,
                    stage="止盈卖单",
                    action="挂单中",
                    detail="",
                )
                continue
            if float(o.get("size_matched", 0) or 0) > 0:
                self._status_add(
                    market=o.get("market", ""),
                    side="买入",
                    price=price_s,
                    size=size_s,
                    matched=matched_s,
                    stage="Step1",
                    action="部分成交",
                    detail=f"已成交{matched_s}",
                )
                continue
            try:
                self._check_compliance(o)
            except Exception as e:
                logger.error("Compliance error on %s: %s", o.get("id"), e)

    def _market_max_spread(self, condition_id: str) -> int | None:
        """Real rewards_max_spread for a market, TTL-cached. None if unknown."""
        if not condition_id:
            return None
        ttl = self.db.get_settings()["rewards_cache_ttl_sec"]
        now = time.time()
        hit = self._max_spread_cache.get(condition_id)
        if hit and (now - hit[1]) < ttl:
            return hit[0]
        try:
            items = self.api.get_rewards_for_market(condition_id)
        except Exception as e:
            logger.warning("get_rewards_for_market(%s) failed: %s", condition_id, e)
            return None
        ms = extract_max_spread(items)
        if ms is None:
            return None
        self._max_spread_cache[condition_id] = (ms, now)
        return ms

    def _check_compliance(self, o: dict):
        """Decide what to do with a resting buy this tick: first re-check the
        bid-ask spread (cancel if it widened past the threshold), else price
        compliance."""
        token_id = o.get("asset_id", "")
        cid = o.get("market", "")
        # 黑名单:该市场不应再有在挂买单——撤掉,绝不重挂(覆盖 Step3 重挂这条路径)。
        if cid in self.db.get_blacklist_ids():
            old_price = float(o.get("price", 0) or 0)
            osize = int(float(o.get("original_size", 0) or 0))
            try:
                self.api.cancel_orders([o.get("id")])
            except Exception as e:
                logger.warning("Blacklist cancel %s failed: %s", o.get("id"), e)
                return
            self._record_action(
                market_id=cid,
                action_type="blacklist_cancel",
                side="-",
                price=-1,
                size=osize,
                reason="市场在黑名单,撤掉在挂买单且不重挂",
                price_basis="黑名单复查；来源：本地 blacklist 表",
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{old_price:.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="黑名单撤单",
                detail="市场已加入黑名单",
            )
            logger.info("[Step3] blacklist cancel %s market %s", o.get("id"), cid)
            return
        settings = self.db.get_settings()
        ob = self.api.get_orderbook(token_id)
        bids = sorted(ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        spread_cents = (
            (best_ask - best_bid) * 100
            if (best_bid is not None and best_ask is not None)
            else None
        )

        # Eligibility re-check: only the bid-ask spread now (cancel a resting buy
        # whose market's spread widened past max_spread_cents). None spread = book
        # side missing -> unknown -> keep.
        cancel, reason = recheck_resting_buy(spread_cents, settings)
        if cancel:
            old_price = float(o.get("price", 0) or 0)
            osize = int(float(o.get("original_size", 0) or 0))
            try:
                self.api.cancel_orders([o.get("id")])
            except Exception as e:
                logger.warning("Eligibility cancel %s failed: %s", o.get("id"), e)
                return
            self._record_action(
                market_id=cid,
                action_type="eligibility_cancel",
                side="-",
                price=-1,
                size=osize,
                reason=reason,
                price_basis="买一卖一价差超阈值复查；来源：CLOB get_orderbook",
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{old_price:.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="复查撤单(价差过大)",
                detail=reason,
            )
            logger.info(
                "[Step3] eligibility cancel %s market %s: %s", o.get("id"), cid, reason
            )
            return

        # --- price compliance (unchanged behavior) ---
        if not bids or not asks:
            logger.info(
                "[Step3] 单 %s 市场 %s | 盘口为空，本轮跳过",
                o.get("id"),
                cid,
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{float(o.get('price', 0) or 0):.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="跳过(盘口为空)",
                detail="盘口为空",
            )
            return
        midpoint = (best_bid + best_ask) / 2
        tick = float(ob.get("tick_size", "0.01"))
        tick_str = ob.get("tick_size", "0.01")
        max_spread = self._market_max_spread(cid)
        if max_spread is None:
            logger.info(
                "[Step3] 单 %s 市场 %s 现价 %.4f | 取不到 rewards_max_spread，"
                "本轮跳过（不撤不重挂）",
                o.get("id"),
                cid,
                float(o.get("price", 0) or 0),
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{float(o.get('price', 0) or 0):.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="跳过(取不到max_spread)",
                detail="取不到 rewards_max_spread",
            )
            return
        rmin = midpoint - max_spread * tick
        rmax = midpoint + max_spread * tick
        try:
            want = determine_order_price(
                bids=bids,
                max_spread=max_spread,
                tick_size=tick,
                reward_range_min=rmin,
                reward_range_max=rmax,
            )
        except Exception as e:
            logger.warning("determine_order_price failed for %s: %s", o.get("id"), e)
            return
        action = needs_replace(float(o.get("price", 0)), want, tick)
        want_str = "无" if want is None else f"{want:.4f}"
        action_zh = {
            "keep": "keep → 保持不动",
            "replace": f"replace → 撤单并重挂 {want_str}",
            "cancel": "cancel → 撤单不重挂",
        }.get(action, action)
        logger.info(
            "[Step3] 单 %s 市场 %s 现价 %.4f | 盘口 bid %.4f ask %.4f mid %.4f "
            "tick %.4f | max_spread=%d 区间[%.4f,%.4f] | 应挂价 %s | 判定 %s",
            o.get("id"),
            cid,
            float(o.get("price", 0) or 0),
            best_bid,
            best_ask,
            midpoint,
            tick,
            max_spread,
            rmin,
            rmax,
            ("无" if want is None else f"{want:.4f}"),
            action_zh,
        )
        self._status_add(
            market=cid,
            side="买入",
            price=f"{float(o.get('price', 0) or 0):.4f}",
            size=str(o.get("original_size", "")),
            matched=str(o.get("size_matched", "0")),
            stage="Step3",
            action=action_zh,
            detail=(
                f"bid{best_bid:.4f} ask{best_ask:.4f} mid{midpoint:.4f} "
                f"ms{max_spread} 区间[{rmin:.4f},{rmax:.4f}] 应挂{want_str}"
            ),
        )
        if action == "keep":
            return
        old_price = float(o.get("price", 0) or 0)
        osize = int(float(o.get("original_size", 0) or 0))
        basis = (
            f"旧价 {old_price:.4f}；区间[{rmin:.4f},{rmax:.4f}] "
            f"mid{midpoint:.4f} ms{max_spread} tick{tick:.4f}；"
            f"来源：CLOB get_orderbook + get_rewards_for_market"
        )
        try:
            self.api.cancel_orders([o["id"]])
        except Exception as e:
            logger.warning("Cancel %s failed: %s", o.get("id"), e)
            return
        if action == "replace":
            self._record_action(
                market_id=cid,
                action_type="step3_cancel_old",
                side="-",
                price=-1,
                size=osize,
                reason=f"挂单价 {old_price:.4f} 不在最新奖励区间内，撤旧买单准备重挂",
                price_basis=basis,
            )
            neg_risk = bool(o.get("neg_risk", False))
            self.api.place_limit_buy(
                token_id, want, osize, tick_size=tick_str, neg_risk=neg_risk
            )
            self._record_action(
                market_id=cid,
                action_type="step3_replace_new",
                side="买入",
                price=want,
                size=osize,
                reason="按策略在奖励区间内重挂买单（贴最优买价深度，最大化奖励占比）",
                price_basis=(
                    f"应挂价 {want:.4f}=determine_order_price(bids, "
                    f"ms{max_spread}, tick{tick:.4f}, "
                    f"区间[{rmin:.4f},{rmax:.4f}])；"
                    f"来源：CLOB get_orderbook + get_rewards_for_market"
                ),
            )
            logger.info("Replaced buy %s -> %.4f", o.get("id"), want)
        else:
            self._record_action(
                market_id=cid,
                action_type="step3_cancel_nocompliant",
                side="-",
                price=-1,
                size=osize,
                reason="奖励区间内无合规价，撤该买单（不重挂）",
                price_basis=basis,
            )
            logger.info("Cancelled non-compliant buy %s (no valid price)", o.get("id"))
