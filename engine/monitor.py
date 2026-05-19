"""engine/monitor.py — API-driven fill detection, stop-loss, strategy compliance."""

import logging
import time
from py_clob_client_v2.clob_types import TradeParams
from engine.fills import select_new_buy_fills
from engine.strategy_check import needs_replace
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

    def begin_status_tick(self) -> None:
        self._status_rows = []
        self._tick_ts = time.time()

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
        """Seed watermark from latest recorded trade ts for this wallet.

        DB created_at is local record time; trade match_time is exchange
        time. Both ~unix seconds; this is only a conservative lower bound,
        (trade_id, order_id) dedup guarantees no double-processing.
        """
        rows = self.db.get_trade_history(self.wallet_address)
        self._after_ts = (
            max((r.get("created_at", 0) or 0) for r in rows) if rows else 0.0
        )

    def _funder(self) -> str:
        """Proxy/funder address — used for get_trades maker filter and Data API."""
        return self.api.get_funder()

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
        asset_id = ev.get("asset_id", "")
        market_id = ev.get("market", "")
        order_id = ev.get("order_id")
        if size <= 0:
            return
        # Take-profit sell at the fill price (our resting maker buy filled here)
        self.api.place_limit_sell(asset_id, price, size)
        self.db.record_trade(
            wallet=self.wallet_address,
            market_id=market_id,
            market_name="",
            side="buy",
            price=price,
            size=size,
        )
        # Main history also records the take-profit sell so the direction
        # column is complete (buy / sell / stop_loss).
        self.db.record_trade(
            wallet=self.wallet_address,
            market_id=market_id,
            market_name="",
            side="sell",
            price=price,
            size=size,
            pnl=0.0,
        )
        self._record_action(
            market_id=market_id,
            action_type="take_profit_sell",
            side="卖出",
            price=price,
            size=size,
            reason="买单成交，按成交价挂等价止盈卖单（赚流动性奖励，原价卖出不亏本金）",
            price_basis=f"卖价=买入成交价 {price:.4f}；来源：CLOB get_trades 的 maker_orders 成交价",
        )
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
            action="成交→挂止盈+撤余",
            detail=f"成交{size} 止盈卖{price:.4f}",
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
        avg = float(pos.get("avgPrice", 0) or 0)
        cur = float(pos.get("curPrice", 0) or 0)
        if size <= 0:
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
            price_basis=f"成本价 avgPrice={avg:.4f}、现价 curPrice={cur:.4f}；来源：Polymarket Data API /positions",
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
        token_id = o.get("asset_id", "")
        ob = self.api.get_orderbook(token_id)
        bids = sorted(ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
        if not bids or not asks:
            logger.info(
                "[Step3] 单 %s 市场 %s | 盘口为空，本轮跳过",
                o.get("id"),
                o.get("market", ""),
            )
            self._status_add(
                market=o.get("market", ""),
                side="买入",
                price=f"{float(o.get('price', 0) or 0):.4f}",
                size=str(o.get("original_size", "")),
                matched=str(o.get("size_matched", "0")),
                stage="Step3",
                action="跳过(盘口为空)",
                detail="盘口为空",
            )
            return
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        midpoint = (best_bid + best_ask) / 2
        tick = float(ob.get("tick_size", "0.01"))
        tick_str = ob.get("tick_size", "0.01")
        max_spread = self._market_max_spread(o.get("market", ""))
        if max_spread is None:
            logger.info(
                "[Step3] 单 %s 市场 %s 现价 %.4f | 取不到 rewards_max_spread，"
                "本轮跳过（不撤不重挂）",
                o.get("id"),
                o.get("market", ""),
                float(o.get("price", 0) or 0),
            )
            self._status_add(
                market=o.get("market", ""),
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
            o.get("market", ""),
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
            market=o.get("market", ""),
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
        try:
            self.api.cancel_orders([o["id"]])
        except Exception as e:
            logger.warning("Cancel %s failed: %s", o.get("id"), e)
            return
        if action == "replace":
            size = int(float(o.get("original_size", 0) or 0))
            neg_risk = bool(o.get("neg_risk", False))
            self.api.place_limit_buy(
                token_id, want, size, tick_size=tick_str, neg_risk=neg_risk
            )
            logger.info("Replaced buy %s -> %.4f", o.get("id"), want)
        else:
            logger.info("Cancelled non-compliant buy %s (no valid price)", o.get("id"))
