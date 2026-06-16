"""tests/test_take_profit.py — pure position-driven take-profit planning."""

import pytest
from engine.take_profit import (
    ceil_to_tick,
    plan_take_profit,
    position_cost_with_lots,
    describe_cost_basis,
)


# ---------------------------------------------------------------------------
# ceil_to_tick: round a (possibly off-tick) avgPrice UP to a valid tick so a
# take-profit sell never goes below cost ("原价卖出不亏本金").
# ---------------------------------------------------------------------------


class TestCeilToTick:
    def test_on_tick_price_unchanged(self):
        # avgPrice exactly on a 0.01 tick must not bump up to the next tick.
        assert ceil_to_tick(0.30, 0.01) == 0.30

    def test_on_tick_price_with_float_dirt_unchanged(self):
        # Data API avg often carries float dirt (e.g. 0.30000002); stays 0.30.
        assert ceil_to_tick(0.3000000244755322, 0.01) == 0.30

    def test_off_tick_rounds_up(self):
        # 0.3023 -> 0.31 (smallest 0.01-multiple >= cost, so we never lose).
        assert ceil_to_tick(0.3023, 0.01) == 0.31

    def test_finer_tick_keeps_precision(self):
        assert ceil_to_tick(0.305, 0.001) == 0.305

    def test_zero_tick_returns_input(self):
        assert ceil_to_tick(0.30, 0) == 0.30


# ---------------------------------------------------------------------------
# plan_take_profit: decide how to reconcile resting SELLs for ONE position so
# that exactly one sell rests at the cost price for the full position size.
# ---------------------------------------------------------------------------


def _sell(oid, price, original, matched=0):
    return {
        "id": oid,
        "side": "SELL",
        "price": str(price),
        "original_size": str(original),
        "size_matched": str(matched),
    }


class TestPlanTakeProfit:
    def test_no_position_is_noop(self):
        plan = plan_take_profit(size=0.0, want_price=0.30, tick=0.01, existing_sells=[])
        assert plan["action"] == "noop"

    def test_zero_price_is_noop(self):
        plan = plan_take_profit(
            size=222.0, want_price=0.0, tick=0.01, existing_sells=[]
        )
        assert plan["action"] == "noop"

    def test_no_existing_sell_places_one(self):
        plan = plan_take_profit(
            size=222.08, want_price=0.30, tick=0.01, existing_sells=[]
        )
        assert plan["action"] == "replace"
        assert plan["price"] == 0.30
        assert plan["size"] == 222.08
        assert plan["cancel_ids"] == []

    def test_correct_single_sell_is_kept(self):
        sells = [_sell("s1", 0.30, 222.08)]
        plan = plan_take_profit(
            size=222.08, want_price=0.30, tick=0.01, existing_sells=sells
        )
        assert plan["action"] == "keep"
        assert plan["cancel_ids"] == []

    def test_phantom_high_price_sell_is_replaced(self):
        sells = [_sell("phantom", 0.38, 200.0)]
        plan = plan_take_profit(
            size=222.08, want_price=0.30, tick=0.01, existing_sells=sells
        )
        assert plan["action"] == "replace"
        assert plan["price"] == 0.30
        assert plan["cancel_ids"] == ["phantom"]

    def test_split_orders_collapse_to_one(self):
        sells = [
            _sell("a", 0.38, 177.77),
            _sell("b", 0.38, 22.23),
            _sell("c", 0.30, 25.0),
            _sell("d", 0.30, 22.857),
            _sell("e", 0.30, 8.171),
            _sell("f", 0.30, 7.471),
            _sell("g", 0.30, 7.28),
            _sell("h", 0.30, 7.128),
        ]
        plan = plan_take_profit(
            size=222.08, want_price=0.30, tick=0.01, existing_sells=sells
        )
        assert plan["action"] == "replace"
        assert plan["price"] == 0.30
        assert set(plan["cancel_ids"]) == {"a", "b", "c", "d", "e", "f", "g", "h"}

    def test_single_sell_wrong_size_is_replaced(self):
        sells = [_sell("s1", 0.30, 100.0)]
        plan = plan_take_profit(
            size=222.08, want_price=0.30, tick=0.01, existing_sells=sells
        )
        assert plan["action"] == "replace"
        assert plan["cancel_ids"] == ["s1"]

    def test_single_sell_within_size_tolerance_is_kept(self):
        sells = [_sell("s1", 0.30, 222.08, matched=0.5)]
        plan = plan_take_profit(
            size=222.08, want_price=0.30, tick=0.01, existing_sells=sells
        )
        assert plan["action"] == "keep"


def _f(side, price, size, ts, tid):
    return {"side": side, "price": price, "size": size, "ts": ts, "trade_id": tid}


class TestPositionCostWithLots:
    """成本=对成交流(买入∪卖出)按时间回放、FIFO 对冲后剩余持仓的加权均价。

    剩余持仓数量必须与 Data API 持仓 size 对得上(容差内),否则成本不可信→None。
    """

    def test_single_buy_full_position(self):
        cost, lots = position_cost_with_lots([_f("BUY", 0.28, 361, 100, "T1")], 361)
        assert cost == pytest.approx(0.28)
        assert lots == [{"price": 0.28, "take": 361.0, "ts": 100.0, "trade_id": "T1"}]

    def test_old_sold_lot_excluded_uses_recent_buy(self):
        # 事故场景:20 天前 256.41@0.36 买入后已全部卖出,最近 294@0.38 买入。
        # 旧买单被旧卖出消耗,绝不污染成本 -> 成本=最近的 0.38。
        fills = [
            _f("BUY", 0.36, 256.41, 1, "OLD"),
            _f("SELL", 0.37, 256.41, 2, "OLDSELL"),
            _f("BUY", 0.38, 294, 3, "NEW"),
        ]
        cost, lots = position_cost_with_lots(fills, 294)
        assert cost == pytest.approx(0.38)
        assert [l["trade_id"] for l in lots] == ["NEW"]
        assert lots[0]["take"] == pytest.approx(294.0)

    def test_lag_window_missing_recent_buy_returns_none(self):
        # 延迟窗口:Data API 持仓已 294,但成交流只回来旧的 256.41@0.36。
        # 重建持仓(256.41)对不上 294 -> 成本不可信 -> None(跳过、不乱卖)。
        cost, lots = position_cost_with_lots([_f("BUY", 0.36, 256.41, 1, "OLD")], 294)
        assert cost is None

    def test_old_round_qty_equals_new_size_not_fooled(self):
        # 关键:旧一轮买入数量恰好==新持仓数量,且新买入还没到。
        # 不做对冲会被旧买单凑满骗过去;做对冲后旧仓已清空、新仓缺席 -> None。
        fills = [
            _f("BUY", 0.36, 294, 1, "OLD"),
            _f("SELL", 0.37, 294, 2, "OLDSELL"),
        ]
        cost, lots = position_cost_with_lots(fills, 294)
        assert cost is None

    def test_partial_sell_of_current_position(self):
        # 当前持仓买 294@0.38,卖掉 100,持仓 194 -> 成本仍 0.38。
        fills = [
            _f("BUY", 0.38, 294, 1, "B"),
            _f("SELL", 0.40, 100, 2, "S"),
        ]
        cost, lots = position_cost_with_lots(fills, 194)
        assert cost == pytest.approx(0.38)
        assert lots[0]["take"] == pytest.approx(194.0)

    def test_fifo_consumes_oldest_lot_first(self):
        # 100@0.30(早) + 100@0.40(晚),卖 100 -> FIFO 先消耗 0.30,剩 100@0.40。
        fills = [
            _f("BUY", 0.30, 100, 1, "OLD"),
            _f("BUY", 0.40, 100, 2, "NEW"),
            _f("SELL", 0.45, 100, 3, "S"),
        ]
        cost, lots = position_cost_with_lots(fills, 100)
        assert cost == pytest.approx(0.40)
        assert [l["trade_id"] for l in lots] == ["NEW"]

    def test_multi_lot_weighted_average(self):
        fills = [_f("BUY", 0.20, 200, 1, "A"), _f("BUY", 0.28, 200, 2, "B")]
        cost, lots = position_cost_with_lots(fills, 400)
        assert cost == pytest.approx(0.24)
        assert sum(l["take"] for l in lots) == pytest.approx(400.0)

    def test_size_within_tolerance_trusted(self):
        # 亚股浮点/部分成交漂移:重建 293 对 size 294,差 1 <= 容差 -> 信。
        cost, _ = position_cost_with_lots([_f("BUY", 0.38, 293, 1, "B")], 294)
        assert cost == pytest.approx(0.38)

    def test_empty_fills(self):
        assert position_cost_with_lots([], 361) == (None, [])

    def test_zero_size(self):
        assert position_cost_with_lots([_f("BUY", 0.28, 361, 1, "X")], 0) == (None, [])


class TestDescribeCostBasis:
    def _lot(self, price, take, ts, tid):
        return {"price": price, "take": take, "ts": ts, "trade_id": tid}

    def test_lists_each_buy_with_price_share_and_trade_id(self):
        lots = [
            self._lot(0.27, 200.0, 1779030000, "0xabcdef1234567890ff"),
            self._lot(0.29, 161.0, 1779031111, "0x9c00000000000000d1"),
        ]
        s = describe_cost_basis(0.28, lots)
        assert s.startswith("成本=0.2800")
        assert "加权自2笔买入成交" in s
        assert "0.2700×200股" in s
        assert "0.2900×161股" in s
        assert "共取361股" in s
        # trade_id 缩写为 首6+'..'+末4
        assert "[trade 0xabcd..90ff]" in s
        assert "[trade 0x9c00..00d1]" in s
        # 时间被格式化(含分隔符),不校验具体时区数值
        assert "-" in s and ":" in s

    def test_short_trade_id_kept_as_is(self):
        s = describe_cost_basis(0.30, [self._lot(0.30, 1000.0, 1, "t")])
        assert "[trade t]" in s

    def test_fractional_share_two_decimals(self):
        s = describe_cost_basis(0.30, [self._lot(0.30, 222.08, 1, "t")])
        assert "×222.08股" in s
        assert "共取222.08股" in s

    def test_caps_lots_and_summarizes_overflow(self):
        lots = [self._lot(0.20 + i * 0.01, 10.0, i, f"t{i}") for i in range(8)]
        s = describe_cost_basis(0.235, lots)
        assert "…等共8笔" in s
        assert "共取80股" in s

    def test_none_cost(self):
        assert "无买入成交" in describe_cost_basis(None, [])
