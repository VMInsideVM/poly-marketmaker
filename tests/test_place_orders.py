"""tests/test_place_orders.py — place_orders 多档下单(mock API)。"""

from unittest.mock import MagicMock
from engine.manager import WalletWorker


def _make_worker(balance=10000.0, template=None):
    api = MagicMock()
    db = MagicMock()
    api.get_balance.return_value = balance
    api.get_open_orders.return_value = []
    api.get_user_positions.return_value = []
    api.get_funder.return_value = "0xF"
    db.get_blacklist_ids.return_value = set()
    db.is_in_cooldown.return_value = False
    tmpl = {
        "tiers_k": 6,
        "tier_rules": [
            [{"upper": None, "action": {"type": "min_size"}}] for _ in range(6)
        ],
        "max_exposure_usd": 250,
        "max_exposure_shares": 500,
        "max_concurrent_markets": 10,
        "min_price_double_cents": 10,
    }
    if template:
        tmpl.update(template)
    db.get_template_for.return_value = tmpl
    return WalletWorker(api, db, "0xW", {"fill_check_interval_sec": 5}), api, db


def _ob(bids, asks=None, tick="0.01"):
    return {
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in (asks or [(0.5, 1000)])],
        "tick_size": tick,
    }


def _elig(market_id, token_id, outcome, min_size=100):
    return {
        "market_id": market_id,
        "token_id": token_id,
        "outcome": outcome,
        "market_name": "M",
        "rewards_min_size": min_size,
        "rewards_max_spread": 6,
        "tick_size_str": "0.01",
        "neg_risk": False,
        "market_competitiveness": 0,
    }


def test_places_multi_tier_min_size_on_one_side():
    worker, api, db = _make_worker()
    api.get_orderbook.return_value = _ob([(0.30, 300), (0.29, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    placed = sorted(
        (round(c.args[1], 2), c.args[2]) for c in api.place_limit_buy.call_args_list
    )
    assert (0.30, 100) in placed and (0.29, 100) in placed


def test_exposure_caps_total_usd():
    worker, api, db = _make_worker(template={"max_exposure_usd": 35})
    api.get_orderbook.return_value = _ob([(0.30, 300), (0.29, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    placed = {
        round(c.args[1], 2): c.args[2] for c in api.place_limit_buy.call_args_list
    }
    assert placed.get(0.30) == 100
    assert placed.get(0.29) == 17


def test_concurrent_market_cap_skips_new_markets():
    worker, api, db = _make_worker(template={"max_concurrent_markets": 1})
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "X",
            "asset_id": "X-y",
            "price": "0.30",
            "original_size": "100",
            "id": "o1",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_not_called()


def test_double_sided_floor_blocks_market_when_one_side_only():
    worker, api, db = _make_worker()
    api.get_orderbook.return_value = _ob([(0.08, 300)], [(0.09, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_not_called()


def test_idempotent_skips_existing_price():
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "A",
            "asset_id": "A-y",
            "price": "0.30",
            "original_size": "100",
            "id": "o1",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 300), (0.29, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    placed = {round(c.args[1], 2) for c in api.place_limit_buy.call_args_list}
    assert 0.30 not in placed and 0.29 in placed


def test_skips_held_and_cooldown_and_blacklist():
    worker, api, db = _make_worker()
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    db.is_in_cooldown.return_value = True
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_not_called()


def test_existing_exposure_on_market_reduces_budget():
    # 跨轮敞口:同市场已有挂单的敞口要从本轮预算里扣掉(existing + new <= 上限)。
    worker, api, db = _make_worker(
        template={
            "max_exposure_usd": 50,
            "tier_rules": [
                [{"upper": None, "action": {"type": "fixed_shares", "shares": 200}}]
                for _ in range(6)
            ],
        }
    )
    # 已有挂单 A-y @ 0.20 × 100 = 20U 占用敞口(价位 0.20,与新档 0.30 不同,无幂等冲突)
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "A",
            "asset_id": "A-y",
            "price": "0.20",
            "original_size": "100",
            "id": "o1",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    # 档想 200 份;预算 = min(balance,50) - 20 = 30U,30/0.30=100 封顶 -> 100
    # (若没扣已有敞口,会用满 50U -> 166 份)
    placed = {
        round(c.args[1], 2): c.args[2] for c in api.place_limit_buy.call_args_list
    }
    assert placed.get(0.30) == 100
