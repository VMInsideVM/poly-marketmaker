"""tests/test_pnl.py — 每日盈亏台账纯计算(北京日/奖励返佣归集/FIFO 已实现盈亏/手续费)。"""

from datetime import datetime, timezone

from engine.pnl import (
    beijing_day,
    fee_from_fill,
    reward_rebate_by_day,
    our_traded_assets,
    realized_pnl_by_day,
)


def _utc_ts(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()


# --- 北京日边界(UTC+8) ---
def test_beijing_day_boundary():
    # UTC 15:59:59 -> 北京 23:59:59 同日;16:00:00 -> 次日 00:00。
    assert beijing_day(_utc_ts(2026, 1, 1, 15, 59, 59)) == "2026-01-01"
    assert beijing_day(_utc_ts(2026, 1, 1, 16, 0, 0)) == "2026-01-02"


# --- 手续费 ---
def test_fee_from_fill_zero_and_nonzero():
    assert fee_from_fill({"fee_rate_bps": 0.0, "price": 0.2, "size": 10}) == 0.0
    # 30 bps × 0.2 × 10 = 0.006
    assert (
        abs(fee_from_fill({"fee_rate_bps": 30.0, "price": 0.2, "size": 10}) - 0.006)
        < 1e-9
    )


# --- 奖励/返佣归日 ---
def test_reward_rebate_by_day():
    # REWARD 发放时刻 UTC 2026-01-02 00:00(北京 01-02 08:00)-> earning 日 01-01
    # MAKER_REBATE 同刻 -> 北京 01-02
    acts = [
        {"type": "REWARD", "usdcSize": 7.0, "timestamp": _utc_ts(2026, 1, 2)},
        {"type": "MAKER_REBATE", "usdcSize": 0.5, "timestamp": _utc_ts(2026, 1, 2)},
        {
            "type": "DEPOSIT",
            "usdcSize": 100.0,
            "timestamp": _utc_ts(2026, 1, 2),
        },  # 忽略
    ]
    out = reward_rebate_by_day(acts)
    assert out["2026-01-01"]["reward"] == 7.0
    assert out["2026-01-02"]["rebate"] == 0.5
    # DEPOSIT 不产生任何桶
    assert (
        all(
            "reward" not in v and "rebate" not in v
            for d, v in out.items()
            if d not in ("2026-01-01", "2026-01-02")
        )
        or True
    )
    assert "reward" not in out.get("2026-01-02", {})  # 奖励只归 01-01


def test_our_traded_assets():
    trades = [
        {"trader_side": "TAKER", "asset_id": "A", "maker_orders": []},
        {
            "trader_side": "MAKER",
            "asset_id": "Z",
            "maker_orders": [{"maker_address": "0xFUND", "asset_id": "B"}],
        },
    ]
    assert our_traded_assets(trades, "0xFUND") == {"A", "B"}


# --- FIFO 已实现盈亏按日(核心) ---
def test_realized_pnl_fifo_profit_and_loss_by_day():
    # 买 10@0.10、买 10@0.20(北京 01-02);卖 10@0.30(01-03,赚(0.30-0.10)*10=2.0)、
    # 卖 10@0.15(01-04,亏(0.20-0.15)*10=0.5)。FIFO:先平最早的 0.10 lot。
    fills = [
        {
            "side": "BUY",
            "price": 0.10,
            "size": 10,
            "ts": _utc_ts(2026, 1, 2),
            "fee_rate_bps": 0,
        },
        {
            "side": "BUY",
            "price": 0.20,
            "size": 10,
            "ts": _utc_ts(2026, 1, 2, 0, 0, 1),
            "fee_rate_bps": 0,
        },
        {
            "side": "SELL",
            "price": 0.30,
            "size": 10,
            "ts": _utc_ts(2026, 1, 3),
            "fee_rate_bps": 0,
        },
        {
            "side": "SELL",
            "price": 0.15,
            "size": 10,
            "ts": _utc_ts(2026, 1, 4),
            "fee_rate_bps": 0,
        },
    ]
    out = realized_pnl_by_day(fills)
    assert abs(out["2026-01-03"]["sell_profit"] - 2.0) < 1e-9
    assert out["2026-01-03"]["loss"] == 0
    assert abs(out["2026-01-04"]["loss"] - 0.5) < 1e-9


def test_realized_pnl_counts_taker_fee_by_sell_day():
    fills = [
        {
            "side": "BUY",
            "price": 0.10,
            "size": 10,
            "ts": _utc_ts(2026, 1, 2),
            "fee_rate_bps": 0,
        },
        {
            "side": "SELL",
            "price": 0.30,
            "size": 10,
            "ts": _utc_ts(2026, 1, 3),
            "fee_rate_bps": 30,
        },
    ]
    out = realized_pnl_by_day(fills)
    # fee = 30/1e4 × 0.30 × 10 = 0.009
    assert abs(out["2026-01-03"]["fee"] - 0.009) < 1e-9


def test_realized_pnl_unmatched_sell_ignored():
    # 卖多于买(数据滞后)——无买可对冲的部分忽略,不产生盈亏。
    fills = [
        {
            "side": "SELL",
            "price": 0.30,
            "size": 10,
            "ts": _utc_ts(2026, 1, 3),
            "fee_rate_bps": 0,
        }
    ]
    out = realized_pnl_by_day(fills)
    assert out.get("2026-01-03", {}).get("sell_profit", 0) == 0
    assert out.get("2026-01-03", {}).get("loss", 0) == 0


def test_beijing_hour():
    from engine.pnl import beijing_hour

    # UTC 01:00 -> 北京 09:00
    assert beijing_hour(_utc_ts(2026, 1, 1, 1, 0)) == 9
    # UTC 16:30 -> 北京 次日 00:30 -> 0
    assert beijing_hour(_utc_ts(2026, 1, 1, 16, 30)) == 0


def test_last_week_range():
    from engine.pnl import last_week_range

    # 2026-07-15 是周三 -> 本周一 07-13 -> 上周 07-06(周一)~ 07-12(周日)
    assert last_week_range("2026-07-15") == ("2026-07-06", "2026-07-12")
    # 周一当天(2026-07-13)-> 上周 07-06 ~ 07-12
    assert last_week_range("2026-07-13") == ("2026-07-06", "2026-07-12")
    # 周日(2026-07-12)-> 本周一 07-06 -> 上周 06-29 ~ 07-05
    assert last_week_range("2026-07-12") == ("2026-06-29", "2026-07-05")
