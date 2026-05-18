"""engine/monitor.py — API-driven fill detection, stop-loss, strategy compliance."""

import logging
from py_clob_client_v2.clob_types import TradeParams
from engine.fills import select_new_buy_fills
from engine.strategy_check import needs_replace
from engine.risk import stop_loss_triggered
from engine.strategy import determine_order_price

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
        self.db.set_cooldown(
            self.wallet_address, market_id, self.db.get_settings()["cooldown_minutes"]
        )
        if order_id and order_id not in cancelled_orders:
            try:
                self.api.cancel_orders([order_id])
                cancelled_orders.add(order_id)
            except Exception as e:
                logger.warning("Cancel remainder of %s failed: %s", order_id, e)

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
        if sell_ids:
            try:
                self.api.cancel_orders(sell_ids)
            except Exception as e:
                logger.warning("Cancel sell orders for %s failed: %s", asset_id, e)
        self.api.place_market_sell(asset_id, size)
        self.db.record_trade(
            wallet=self.wallet_address,
            market_id=pos.get("conditionId", ""),
            market_name="",
            side="stop_loss",
            price=cur,
            size=size,
            pnl=(cur - avg) * size,
        )
        logger.warning(
            "Stop-loss executed: asset=%s size=%s cur=%.4f avg=%.4f",
            asset_id,
            size,
            cur,
            avg,
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
            if o.get("side") != "BUY":
                continue
            if float(o.get("size_matched", 0) or 0) > 0:
                continue
            try:
                self._check_compliance(o)
            except Exception as e:
                logger.error("Compliance error on %s: %s", o.get("id"), e)

    def _check_compliance(self, o: dict):
        token_id = o.get("asset_id", "")
        ob = self.api.get_orderbook(token_id)
        bids = sorted(ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
        if not bids or not asks:
            return
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        midpoint = (best_bid + best_ask) / 2
        tick = float(ob.get("tick_size", "0.01"))
        tick_str = ob.get("tick_size", "0.01")
        # rewards_max_spread is not on the order; recover from settings default
        max_spread = 2
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
