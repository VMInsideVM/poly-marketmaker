# tests/test_fills.py
from engine.fills import select_new_buy_fills

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
