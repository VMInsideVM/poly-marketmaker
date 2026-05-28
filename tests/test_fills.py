# tests/test_fills.py
from engine.fills import select_new_buy_fills, extract_buy_fills, extract_fills

FUNDER = "0x98d67a03a5AFf272Dc02016c06EF9c18aec4ae75"

# Mirrors the real spike trade: top-level side=SELL (taker view); maker_orders
# mixes one of OURS (SELL) and one OTHER trader (BUY). No buy fill of ours here.
TRADE_SELL_MIX = {
    "id": "trade-1",
    "side": "SELL",
    "size": "223.88",
    "price": "0.7",
    "asset_id": "TOP_LEVEL_ASSET",
    "market": "COND_A",
    "match_time": "1779035234",
    "trader_side": "MAKER",
    "maker_orders": [
        {
            "order_id": "ord-ours-sell",
            "maker_address": FUNDER,
            "side": "SELL",
            "matched_amount": "190.31",
            "price": "0.3",
            "asset_id": "ASSET_YES",
            "outcome": "Yes",
        },
        {
            "order_id": "ord-other",
            "maker_address": "0x49c40bD313D8599F54B62fff13324a790c4fBf77",
            "side": "BUY",
            "matched_amount": "33.57",
            "price": "0.7",
            "asset_id": "ASSET_NO",
            "outcome": "No",
        },
    ],
}

# A trade where TWO of our BUY orders are filled in the same trade.
TRADE_TWO_OURS_BUY = {
    "id": "trade-2",
    "side": "SELL",
    "size": "50",
    "price": "0.9",
    "asset_id": "TOP",
    "market": "COND_B",
    "match_time": "1779030000",
    "trader_side": "MAKER",
    "maker_orders": [
        {
            "order_id": "ord-b1",
            "maker_address": FUNDER.lower(),
            "side": "BUY",
            "matched_amount": "20",
            "price": "0.4",
            "asset_id": "ASSET_B1",
            "outcome": "Yes",
        },
        {
            "order_id": "ord-b2",
            "maker_address": FUNDER,
            "side": "BUY",
            "matched_amount": "30",
            "price": "0.41",
            "asset_id": "ASSET_B2",
            "outcome": "Yes",
        },
    ],
}


def test_only_our_buy_maker_orders_emitted():
    fills = select_new_buy_fills([TRADE_SELL_MIX], FUNDER, set())
    # our SELL entry skipped, other trader's BUY skipped, top-level ignored
    assert fills == []


def test_emits_one_event_per_our_buy_with_maker_fields():
    fills = select_new_buy_fills([TRADE_TWO_OURS_BUY], FUNDER, set())
    assert [f["order_id"] for f in fills] == ["ord-b1", "ord-b2"]
    f = fills[0]
    assert f["trade_id"] == "trade-2"
    assert f["asset_id"] == "ASSET_B1"  # from maker_order, not top-level
    assert f["price"] == 0.4  # from maker_order, not 0.9
    assert f["size"] == 20.0  # matched_amount, not top-level 50
    assert f["market"] == "COND_B"  # trade top-level (condition id)
    assert f["ts"] == 1779030000.0


def test_dedup_by_trade_id_and_order_id():
    seen = {("trade-2", "ord-b1")}
    fills = select_new_buy_fills([TRADE_TWO_OURS_BUY], FUNDER, seen)
    assert [f["order_id"] for f in fills] == ["ord-b2"]


def test_sorted_by_ts_ascending_across_trades():
    fills = select_new_buy_fills([TRADE_TWO_OURS_BUY, TRADE_SELL_MIX], FUNDER, set())
    # only TRADE_TWO_OURS_BUY yields events; both share ts, order_id order kept
    assert [f["order_id"] for f in fills] == ["ord-b1", "ord-b2"]


def test_funder_match_is_case_insensitive():
    fills = select_new_buy_fills([TRADE_TWO_OURS_BUY], FUNDER.lower(), set())
    assert len(fills) == 2


def test_extract_buy_fills_only_our_buys_on_asset():
    # TRADE_TWO_OURS_BUY 有我们两笔 BUY:ASSET_B1@0.4x20、ASSET_B2@0.41x30
    fills = extract_buy_fills([TRADE_TWO_OURS_BUY], FUNDER, "ASSET_B1")
    assert fills == [
        {"price": 0.4, "size": 20.0, "ts": 1779030000.0, "trade_id": "trade-2"}
    ]


def test_extract_buy_fills_skips_our_sell_and_others():
    # TRADE_SELL_MIX:我们的是 SELL(ASSET_YES),另一笔是别人的 BUY(ASSET_NO)
    assert extract_buy_fills([TRADE_SELL_MIX], FUNDER, "ASSET_YES") == []
    assert extract_buy_fills([TRADE_SELL_MIX], FUNDER, "ASSET_NO") == []


def test_extract_buy_fills_case_insensitive_funder():
    fills = extract_buy_fills([TRADE_TWO_OURS_BUY], FUNDER.lower(), "ASSET_B2")
    assert fills == [
        {"price": 0.41, "size": 30.0, "ts": 1779030000.0, "trade_id": "trade-2"}
    ]


def test_extract_buy_fills_aggregates_across_trades_same_asset():
    t2 = {
        **TRADE_TWO_OURS_BUY,
        "id": "trade-3",
        "match_time": "1779031111",
        "maker_orders": [
            {
                "order_id": "ord-b3",
                "maker_address": FUNDER,
                "side": "BUY",
                "matched_amount": "10",
                "price": "0.42",
                "asset_id": "ASSET_B1",
            }
        ],
    }
    fills = extract_buy_fills([TRADE_TWO_OURS_BUY, t2], FUNDER, "ASSET_B1")
    assert {
        "price": 0.4,
        "size": 20.0,
        "ts": 1779030000.0,
        "trade_id": "trade-2",
    } in fills
    assert {
        "price": 0.42,
        "size": 10.0,
        "ts": 1779031111.0,
        "trade_id": "trade-3",
    } in fills
    assert len(fills) == 2


# 我们当 taker 的买入:成交在 trade 顶层,maker_orders 里是对手方(不是我们)
TRADE_TAKER_BUY = {
    "id": "trade-taker-1",
    "side": "BUY",
    "size": "100",
    "price": "0.33",
    "asset_id": "ASSET_T",
    "market": "COND_T",
    "match_time": "1779040000",
    "trader_side": "TAKER",
    "maker_orders": [
        {
            "order_id": "ord-counterparty",
            "maker_address": "0x49c40bD313D8599F54B62fff13324a790c4fBf77",
            "side": "SELL",
            "matched_amount": "100",
            "price": "0.33",
            "asset_id": "ASSET_T",
        }
    ],
}


def test_extract_buy_fills_includes_taker_buy():
    fills = extract_buy_fills([TRADE_TAKER_BUY], FUNDER, "ASSET_T")
    assert fills == [
        {"price": 0.33, "size": 100.0, "ts": 1779040000.0, "trade_id": "trade-taker-1"}
    ]


def test_extract_buy_fills_taker_sell_ignored():
    t = {**TRADE_TAKER_BUY, "side": "SELL"}
    assert extract_buy_fills([t], FUNDER, "ASSET_T") == []


def test_extract_buy_fills_taker_wrong_asset_ignored():
    assert extract_buy_fills([TRADE_TAKER_BUY], FUNDER, "OTHER_ASSET") == []


def test_extract_buy_fills_maker_and_taker_no_double_count():
    # 一个 maker 买入(ASSET_B1@0.4x20) + 一个 taker 买入(ASSET_T@0.33x100)
    maker_fills = extract_buy_fills([TRADE_TWO_OURS_BUY], FUNDER, "ASSET_B1")
    taker_fills = extract_buy_fills([TRADE_TAKER_BUY], FUNDER, "ASSET_T")
    assert maker_fills == [
        {"price": 0.4, "size": 20.0, "ts": 1779030000.0, "trade_id": "trade-2"}
    ]
    assert taker_fills == [
        {"price": 0.33, "size": 100.0, "ts": 1779040000.0, "trade_id": "trade-taker-1"}
    ]
    # 混在一起按各自 asset 提取,互不串扰
    assert extract_buy_fills(
        [TRADE_TWO_OURS_BUY, TRADE_TAKER_BUY], FUNDER, "ASSET_T"
    ) == [
        {"price": 0.33, "size": 100.0, "ts": 1779040000.0, "trade_id": "trade-taker-1"}
    ]


def test_extract_buy_fills_no_double_count_when_funder_in_both():
    # 防御:同一笔 trade 若我们的 funder 同时出现在 maker_orders 且 trader_side=TAKER
    # (异常/矛盾数据),只能计一次,不能重复计入
    weird = {
        "id": "trade-weird",
        "side": "BUY",
        "size": "100",
        "price": "0.33",
        "asset_id": "ASSET_T",
        "market": "COND_T",
        "match_time": "1779050000",
        "trader_side": "TAKER",
        "maker_orders": [
            {
                "order_id": "ord-ours",
                "maker_address": FUNDER,
                "side": "BUY",
                "matched_amount": "100",
                "price": "0.33",
                "asset_id": "ASSET_T",
            }
        ],
    }
    fills = extract_buy_fills([weird], FUNDER, "ASSET_T")
    assert len(fills) == 1
    assert fills[0]["size"] == 100.0


def test_extract_buy_fills_carries_trade_id_maker_and_taker():
    maker = extract_buy_fills([TRADE_TWO_OURS_BUY], FUNDER, "ASSET_B1")
    assert maker[0]["trade_id"] == "trade-2"
    taker = extract_buy_fills([TRADE_TAKER_BUY], FUNDER, "ASSET_T")
    assert taker[0]["trade_id"] == "trade-taker-1"


# 我们当 taker 的卖出(止损市价平仓):成交在 trade 顶层,我们不在 maker_orders 里
TRADE_TAKER_SELL = {
    "id": "trade-taker-sell",
    "side": "SELL",
    "size": "80",
    "price": "0.29",
    "asset_id": "ASSET_T",
    "market": "COND_T",
    "match_time": "1779060000",
    "trader_side": "TAKER",
    "maker_orders": [
        {
            "order_id": "ord-cp",
            "maker_address": "0x49c40bD313D8599F54B62fff13324a790c4fBf77",
            "side": "BUY",
            "matched_amount": "80",
            "price": "0.29",
            "asset_id": "ASSET_T",
        }
    ],
}


def test_extract_fills_includes_our_maker_buy_with_side():
    fills = extract_fills([TRADE_TWO_OURS_BUY], FUNDER, "ASSET_B1")
    assert fills == [
        {
            "side": "BUY",
            "price": 0.4,
            "size": 20.0,
            "ts": 1779030000.0,
            "trade_id": "trade-2",
        }
    ]


def test_extract_fills_includes_our_maker_sell():
    # TRADE_SELL_MIX:我们挂的卖单被吃(ASSET_YES,190.31@0.3)
    fills = extract_fills([TRADE_SELL_MIX], FUNDER, "ASSET_YES")
    assert fills == [
        {
            "side": "SELL",
            "price": 0.3,
            "size": 190.31,
            "ts": 1779035234.0,
            "trade_id": "trade-1",
        }
    ]


def test_extract_fills_includes_our_taker_buy():
    fills = extract_fills([TRADE_TAKER_BUY], FUNDER, "ASSET_T")
    assert fills == [
        {
            "side": "BUY",
            "price": 0.33,
            "size": 100.0,
            "ts": 1779040000.0,
            "trade_id": "trade-taker-1",
        }
    ]


def test_extract_fills_includes_our_taker_sell():
    fills = extract_fills([TRADE_TAKER_SELL], FUNDER, "ASSET_T")
    assert fills == [
        {
            "side": "SELL",
            "price": 0.29,
            "size": 80.0,
            "ts": 1779060000.0,
            "trade_id": "trade-taker-sell",
        }
    ]


def test_extract_fills_skips_other_traders_and_wrong_asset():
    # ASSET_NO 那笔是别人的 BUY;ASSET_YES 才是我们的
    assert extract_fills([TRADE_SELL_MIX], FUNDER, "ASSET_NO") == []
    assert extract_fills([TRADE_TAKER_BUY], FUNDER, "OTHER") == []


def test_extract_fills_buy_then_sell_across_trades_same_asset():
    fills = extract_fills(
        [TRADE_TAKER_BUY, TRADE_TAKER_SELL], FUNDER.lower(), "ASSET_T"
    )
    sides = [(f["side"], f["size"]) for f in fills]
    assert ("BUY", 100.0) in sides
    assert ("SELL", 80.0) in sides
    assert len(fills) == 2
