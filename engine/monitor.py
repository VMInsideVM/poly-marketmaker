"""engine/monitor.py — API-driven fill detection, stop-loss, strategy compliance."""

import logging
import time
from py_clob_client_v2.clob_types import TradeParams
from engine.fills import select_new_buy_fills, extract_fills
from engine.take_profit import (
    plan_take_profit,
    position_cost_with_lots,
    describe_cost_basis,
    plan_exit,
)
from engine.eligibility import recheck_resting_buy
from engine.resolution import in_resolution
from engine.strategy import reward_price_range
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
        """Seed watermark AND prime the seen-fill-keys set on startup recovery.

        The watermark is the latest recorded trade/action ts — DB created_at is
        local record time while trade match_time is exchange time, so it is only
        a **conservative lower bound**: `get_trades(after=watermark)` deliberately
        re-fetches a recent window and relies on `(trade_id, order_id)` dedup.
        Actions are included so that clearing the trades table does not reset it
        to 0 (which would refetch all history and re-cooldown old markets).

        BUT `_seen_fill_keys` lives only in memory and is empty after a restart,
        so that dedup used to NOT survive a restart: the first ticks re-processed
        already-handled fills, re-recording `cancel_remainder` and re-setting
        cooldown for markets whose position was long closed (real 2026-05-29
        observation). So here we also **prime `_seen_fill_keys` with every fill
        already on record** — only fills that occur AFTER startup then trigger
        `_handle_fill`. (Offline fills are already protected position-side by the
        position-driven take-profit, independent of Step 1.)
        """
        rows = self.db.get_trade_history(self.wallet_address)
        actions = self.db.get_actions(self.wallet_address)
        times = [r.get("created_at", 0) or 0 for r in rows]
        times += [a.get("created_at", 0) or 0 for a in actions]
        self._after_ts = max(times) if times else 0.0
        # Prime dedup with all currently-visible fills so a restart never
        # re-handles pre-startup fills. Best-effort: a fetch failure just leaves
        # the (conservative) watermark in charge, same as before.
        funder = self._funder()
        try:
            trades = self.api.get_trades(TradeParams(maker_address=funder))
            for ev in select_new_buy_fills(trades, funder, set()):
                self._seen_fill_keys.add((ev.get("trade_id"), ev.get("order_id")))
        except Exception as e:
            logger.warning("init_watermark seed seen-keys failed: %s", e)

    def _funder(self) -> str:
        """Proxy/funder address — used for get_trades maker filter and Data API."""
        return self.api.get_funder()

    def _cost_lots(self, asset_id: str, size: float, condition_id: str):
        """该持仓的加权成本 + 剩余逐笔构成(本 tick 缓存)。由 CLOB get_trades 的真实
        成交(买入∪卖出,maker∪taker)按时间回放、FIFO 对冲重建;重建持仓与 size 对不
        上(成交流滞后/外部转入)-> (None, [])。

        用 market=condition_id 过滤拉成交,再由 extract_fills 客户端按 asset 过滤:
        Polymarket /trades 的服务端 asset_id 过滤失效(同一 asset 明明有成交却返回空,
        2026-05-29 真实事故 -> 成本算不出、全仓裸奔),而 market(conditionId)过滤可用,
        且会一并带回我们在该市场的 taker 成交(止损市价单等),正是 extract_fills 所需。"""
        if asset_id in self._cost_cache:
            return self._cost_cache[asset_id]
        funder = self._funder()
        try:
            trades = self.api.get_trades(TradeParams(market=condition_id))
        except Exception as e:
            logger.warning("get_trades(market=%s) for cost failed: %s", condition_id, e)
            self._cost_cache[asset_id] = (None, [])
            return None, []
        fills = extract_fills(trades, funder, asset_id)
        result = position_cost_with_lots(fills, size)
        self._cost_cache[asset_id] = result
        return result

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
        # No trades-table write: buy "history" is the live Data API position.
        # 离场卖单由 check_exit() 的两段式逻辑(plan_exit)按 get_trades 加权成本维护。
        # Step 1 only sets the cooldown and cancels the buy's remainder.
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

    def check_resolution(self):
        """UMA 结算守卫:对已挂买单的市场,一旦 umaResolutionStatus 非空(有人在 UMA
        上提交了 resolution)即撤掉该市场全部 BUY,避免被成交买进一个即将结算的市场。
        持仓与卖单不动(卖单仍由 check_exit 在结算前正常离场)。

        Gamma 失败时 gamma_resolution_status 返回 {} -> 一律不撤(fail-open),绝不
        因一次接口抖动误撤全仓买单;下个 tick 自然重试。
        """
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed (skip resolution guard): %s", e)
            return
        buys_by_cid: dict = {}
        for o in open_orders:
            if o.get("side") != "BUY":
                continue
            cid = o.get("market", "")
            if cid:
                buys_by_cid.setdefault(cid, []).append(o)
        if not buys_by_cid:
            return
        status_map = self.api.gamma_resolution_status(list(buys_by_cid.keys()))
        for cid, buys in buys_by_cid.items():
            status = status_map.get(cid)
            if not in_resolution(status):
                continue
            ids = [o.get("id") for o in buys]
            try:
                self.api.cancel_orders(ids)
            except Exception as e:
                logger.warning(
                    "UMA resolution cancel failed cid=%s: %s — buys 未撤", cid, e
                )
                self._status_add(
                    market=cid,
                    side="买入",
                    price="-",
                    size="-",
                    matched="-",
                    stage="结算守卫",
                    action="⚠️UMA撤单失败",
                    detail=f"umaResolutionStatus={status}，撤 {len(ids)} 笔买单失败：{e}",
                )
                continue
            self._record_action(
                market_id=cid,
                action_type="uma_resolution_cancel",
                side="-",
                price=-1,
                size=0,
                reason=(
                    f"市场进入 UMA 结算(状态={status})，撤销该市场全部买单，"
                    "避免买进将结算的市场"
                ),
                price_basis=(
                    f"status={status}；撤 {len(ids)} 笔 BUY；"
                    "来源：Gamma /markets condition_ids"
                ),
            )
            self._status_add(
                market=cid,
                side="买入",
                price="-",
                size="-",
                matched="-",
                stage="结算守卫",
                action="⚠️UMA已提交·撤买单",
                detail=f"umaResolutionStatus={status}，撤 {len(ids)} 笔买单",
            )

    def _sell_book(self, asset_id: str):
        """(tick_float, tick_str, best_bid, best_ask);失败/空时缺的位回 None。"""
        try:
            ob = self.api.get_orderbook(asset_id)
            tick_str = ob.get("tick_size", "0.01")
            bids = sorted(
                ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True
            )
            asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            return float(tick_str), tick_str, best_bid, best_ask
        except Exception as e:
            logger.warning("orderbook for %s failed (exit tick=0.01): %s", asset_id, e)
            return 0.01, "0.01", None, None

    def check_exit(self):
        """两段式离场:成本<买一挂成本价,成本≥买一挂卖一(亏损≥theta_stop 兜底市价止损)。"""
        tmpl = self.db.get_template_for(self.wallet_address)
        theta_loss = float(tmpl.get("theta_loss_cents", 2)) / 100.0
        theta_stop = float(tmpl.get("theta_stop_cents", 5)) / 100.0
        case_a_mode = tmpl.get("case_a_mode", "ask")
        try:
            positions = self.api.get_user_positions(self._funder())
        except Exception as e:
            logger.warning("Data API positions failed (skip exit): %s", e)
            return
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed (skip exit): %s", e)
            return
        for pos in positions:
            try:
                self._exit_position(
                    pos, open_orders, theta_loss, theta_stop, case_a_mode
                )
            except Exception as e:
                logger.error("Exit error on %s: %s", pos.get("asset"), e)

    def _exit_position(self, pos, open_orders, theta_loss, theta_stop, case_a_mode):
        asset_id = pos.get("asset", "")
        size = float(pos.get("size", 0) or 0)
        cur = float(pos.get("curPrice", 0) or 0)
        cid = pos.get("conditionId", "")
        if size <= 0:
            return
        cost, lots = self._cost_lots(asset_id, size, cid)
        if cost is None or cost <= 0:
            logger.warning(
                "Exit skipped (no buy fills) asset=%s size=%s — UNPROTECTED",
                asset_id,
                size,
            )
            self._status_add(
                market=cid,
                side="卖出",
                price="-",
                size=str(size),
                matched="-",
                stage="离场",
                action="⚠️跳过·裸奔",
                detail="get_trades 无买入成交、无法算成本，未离场，该持仓未受保护",
            )
            return
        tick, tick_str, best_bid, best_ask = self._sell_book(asset_id)
        plan = plan_exit(
            cost, best_bid, best_ask, tick, theta_loss, theta_stop, case_a_mode, size
        )
        sells = [
            o
            for o in open_orders
            if o.get("asset_id") == asset_id and o.get("side") == "SELL"
        ]
        action = plan["action"]
        basis = describe_cost_basis(cost, lots)

        if action == "noop":
            self._status_add(
                market=cid,
                side="卖出",
                price="-",
                size=str(size),
                matched="-",
                stage="离场",
                action="跳过(盘口空)",
                detail="无买盘无卖盘，本轮跳过",
            )
            return

        if action == "rest":
            want = plan["price"]
            p = plan_take_profit(size, want, tick, sells)
            if p["action"] in ("noop", "keep"):
                self._status_add(
                    market=cid,
                    side="卖出",
                    price=f"{want:.4f}",
                    size=str(size),
                    matched="-",
                    stage="离场",
                    action=f"保持({plan['tier']})",
                    detail=f"成本{cost:.4f} 挂卖单{want:.4f}",
                )
                return
            if p["cancel_ids"]:
                try:
                    self.api.cancel_orders(p["cancel_ids"])
                    self._record_action(
                        market_id=cid,
                        action_type="exit_recancel",
                        side="-",
                        price=-1,
                        size=size,
                        reason="撤与持仓不符的旧卖单，改按持仓挂一笔",
                        price_basis=f"撤 {len(p['cancel_ids'])} 笔 SELL",
                    )
                except Exception as e:
                    logger.warning("Cancel stale sells %s failed: %s", asset_id, e)
                    return
            try:
                self.api.place_limit_sell(asset_id, want, size, tick_size=tick_str)
            except Exception as e:
                logger.error("Rest sell failed asset=%s: %s — UNPROTECTED", asset_id, e)
                self._status_add(
                    market=cid,
                    side="卖出",
                    price=f"{want:.4f}",
                    size=str(size),
                    matched="-",
                    stage="离场",
                    action="⚠️挂卖失败·裸奔",
                    detail=f"{plan['tier']} 挂卖单被拒，该持仓未受保护：{e}",
                )
                return
            self._record_action(
                market_id=cid,
                action_type="exit_rest",
                side="卖出",
                price=want,
                size=size,
                reason=f"{plan['tier']}：挂卖单离场",
                price_basis=f"{basis}；挂卖价{want:.4f}；来源：CLOB get_trades+get_orderbook",
            )
            self._status_add(
                market=cid,
                side="卖出",
                price=f"{want:.4f}",
                size=str(size),
                matched="-",
                stage="离场",
                action=f"挂卖单({plan['tier']})",
                detail=f"成本{cost:.4f}",
            )
            return

        sell_ids = [o["id"] for o in sells]
        if sell_ids:
            try:
                self.api.cancel_orders(sell_ids)
                self._record_action(
                    market_id=cid,
                    action_type="exit_cancel_sell",
                    side="-",
                    price=-1,
                    size=size,
                    reason=f"{plan['tier']}：先撤全部止盈卖单以便清仓",
                    price_basis=f"撤 {len(sell_ids)} 笔 SELL",
                )
            except Exception as e:
                # 撤单失败仍继续清仓:离场(尤其 B0 强平)优先保证出手,不因撤单一时失败
                # 把仓晾着不止损。代价是可能短暂与残留挂卖单并存(下个 tick 对账清理);
                # 超卖不会发生(没有的份额卖不出)。与旧 stop-loss 的取舍一致。
                logger.warning(
                    "Cancel sells %s failed (proceed to exit): %s", asset_id, e
                )

        if action == "market":
            try:
                self.api.place_market_sell(asset_id, size)
            except Exception as e:
                logger.error(
                    "Market exit failed asset=%s: %s — UNPROTECTED", asset_id, e
                )
                self._status_add(
                    market=cid,
                    side="卖出",
                    price=f"{cur:.4f}",
                    size=str(size),
                    matched="-",
                    stage="离场",
                    action="⚠️止损失败·裸奔",
                    detail=f"{plan['tier']} 市价清仓被拒，该持仓未受保护：{e}",
                )
                return
            if plan["tier"] == "B0":
                self.db.record_trade(
                    wallet=self.wallet_address,
                    market_id=cid,
                    market_name="",
                    side="stop_loss",
                    price=cur,
                    size=size,
                    pnl=(cur - cost) * size,
                )
            self._record_action(
                market_id=cid,
                action_type="exit_market",
                side="卖出",
                price=cur,
                size=size,
                reason=f"{plan['tier']}：市价清仓离场",
                price_basis=f"{basis}；现价{cur:.4f}；来源：CLOB get_trades+Data API",
            )
            self._status_add(
                market=cid,
                side="卖出",
                price=f"{cur:.4f}",
                size=str(size),
                matched="-",
                stage="离场",
                action=f"市价清仓({plan['tier']})",
                detail=f"成本{cost:.4f}",
            )
            return

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

    def _market_max_spread(self, condition_id: str) -> float | None:
        """Real rewards_max_spread (in cents) for a market, TTL-cached.

        None if unknown. Kept as a float — fractional cents (e.g. 4.5) must
        survive so the reward band isn't narrowed.
        """
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
        settings = self.db.get_template_for(self.wallet_address)
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

        # --- 奖励区间合规检查（SP2：不重挂，多档引擎在下次下单周期重挂）---
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
        # Reward band is max_spread CENTS around the midpoint, NOT max_spread
        # ticks (the tick form is ~10x too narrow on 0.1-cent markets).
        rmin, rmax = reward_price_range(midpoint, max_spread)
        cur_price = float(o.get("price", 0) or 0)
        osize = int(float(o.get("original_size", 0) or 0))

        # 价格区间护栏：挂单价若超出配置「单价区间」[min,max]美分，撤该买单不重挂。
        # 缺失阈值时安全默认 0/100，使该闸门成为 no-op。
        min_pc = float(settings.get("min_price_cents", 0.0))
        max_pc = float(settings.get("max_price_cents", 100.0))
        if cur_price * 100 < min_pc or cur_price * 100 > max_pc:
            reason = (
                f"挂单价 {cur_price * 100:.1f}c 不在单价区间 "
                f"[{min_pc:.0f}c, {max_pc:.0f}c]，撤买单不重挂"
            )
            try:
                self.api.cancel_orders([o.get("id")])
            except Exception as e:
                logger.warning("Price-band cancel %s failed: %s", o.get("id"), e)
                return
            self._record_action(
                market_id=cid,
                action_type="price_band_cancel",
                side="-",
                price=-1,
                size=osize,
                reason=reason,
                price_basis=(
                    f"挂单价={cur_price:.4f}；单价区间[{min_pc:.0f}c,{max_pc:.0f}c]；"
                    f"来源：CLOB get_orderbook + get_rewards_for_market + 设置"
                ),
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{cur_price:.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="撤单(价格超区间)",
                detail=reason,
            )
            logger.info(
                "[Step3] price-band cancel %s market %s: %s", o.get("id"), cid, reason
            )
            return

        # 挂单价不在奖励区间 → 撤单不重挂（多档引擎下次 place_orders 重挂）。
        if not (rmin <= cur_price <= rmax):
            reason = (
                f"挂单价 {cur_price:.4f} 不在奖励区间 "
                f"[{rmin:.4f},{rmax:.4f}]，撤买单不重挂"
            )
            try:
                self.api.cancel_orders([o.get("id")])
            except Exception as e:
                logger.warning("Cancel out-of-band %s failed: %s", o.get("id"), e)
                return
            self._record_action(
                market_id=cid,
                action_type="step3_cancel_outofband",
                side="-",
                price=-1,
                size=osize,
                reason=reason,
                price_basis=(
                    f"挂单价={cur_price:.4f}；奖励区间[{rmin:.4f},{rmax:.4f}] "
                    f"mid{midpoint:.4f} ms{max_spread}；"
                    f"来源：CLOB get_orderbook + get_rewards_for_market"
                ),
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{cur_price:.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="撤单(价格出奖励区间)",
                detail=reason,
            )
            logger.info(
                "[Step3] out-of-band cancel %s market %s | price %.4f not in [%.4f,%.4f]",
                o.get("id"),
                cid,
                cur_price,
                rmin,
                rmax,
            )
            return

        # 挂单价在奖励区间内 → 保持不动。
        logger.info(
            "[Step3] 单 %s 市场 %s 现价 %.4f | bid %.4f ask %.4f mid %.4f "
            "max_spread=%s 区间[%.4f,%.4f] | keep",
            o.get("id"),
            cid,
            cur_price,
            best_bid,
            best_ask,
            midpoint,
            max_spread,
            rmin,
            rmax,
        )
        self._status_add(
            market=cid,
            side="买入",
            price=f"{cur_price:.4f}",
            size=str(o.get("original_size", "")),
            matched=str(o.get("size_matched", "0")),
            stage="Step3",
            action="keep → 保持不动",
            detail=(
                f"bid{best_bid:.4f} ask{best_ask:.4f} mid{midpoint:.4f} "
                f"ms{max_spread} 区间[{rmin:.4f},{rmax:.4f}]"
            ),
        )
