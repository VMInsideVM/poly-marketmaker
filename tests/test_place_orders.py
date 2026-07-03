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
    api.gamma_resolution_status.return_value = {}  # UMA 守卫默认:无市场在结算
    db.get_blacklist_ids.return_value = set()
    db.is_in_cooldown.return_value = False
    tmpl = {
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


def test_skips_market_in_uma_resolution():
    # 有人在 UMA 提交 resolution(umaResolutionStatus 非空)的市场 -> 本轮不挂买单,
    # 避免「撤了又挂」。
    worker, api, db = _make_worker()
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    api.gamma_resolution_status.return_value = {"A": "proposed"}
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_not_called()


def test_places_normally_when_not_in_resolution():
    # 正常市场(umaResolutionStatus=None)照常下单,守卫不误伤。
    worker, api, db = _make_worker()
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    api.gamma_resolution_status.return_value = {"A": None}
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_called()


def test_exposure_caps_total_usd():
    # 预算 35:第1档 0.30*100=30 挂满;余 5 只够第2档 int(5/0.29)=17 份 < min_size(100)
    # -> 第2档放弃(不挂残档)。USD 敞口上限仍然生效:总挂出量被限在第1档。
    worker, api, db = _make_worker(template={"max_exposure_usd": 35})
    api.get_orderbook.return_value = _ob([(0.30, 300), (0.29, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    placed = {
        round(c.args[1], 2): c.args[2] for c in api.place_limit_buy.call_args_list
    }
    assert placed.get(0.30) == 100
    assert placed.get(0.29) is None


def test_reconcile_replaces_partial_fill_remnant():
    # 现象2 端到端:某买单原始 100、已成交 60(剩 40 < min_size 100),持仓已平(不在
    # held_assets)。下单轮 reconcile 须按剩余量(原始-已成交)判定量不符,撤掉残单并
    # 重挂满额 100,而非把它当「量符」一直留着。验证 size_matched 确实传到了 reconcile。
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {
            "id": "o1",
            "side": "BUY",
            "asset_id": "A-y",
            "market": "A",
            "price": "0.30",
            "original_size": "100",
            "size_matched": "60",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    cancel_calls = [c.args[0] for c in api.cancel_orders.call_args_list]
    assert ["o1"] in cancel_calls
    placed = {
        round(c.args[1], 2): c.args[2] for c in api.place_limit_buy.call_args_list
    }
    assert placed.get(0.30) == 100


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


def test_full_budget_and_reconcile_cancels_stale_buy():
    # 全额预算(不再扣已挂敞口)+ 撤改收敛:旧价档(0.20,不在新目标)被撤,目标 0.30 挂上。
    worker, api, db = _make_worker(
        template={
            "max_exposure_usd": 50,
            "tier_rules": [
                [{"upper": None, "action": {"type": "fixed_shares", "shares": 200}}]
                for _ in range(6)
            ],
        }
    )
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "A",
            "asset_id": "A-y",
            "price": "0.20",
            "original_size": "100",
            "id": "o-old",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    placed = {
        round(c.args[1], 2): c.args[2] for c in api.place_limit_buy.call_args_list
    }
    assert placed.get(0.30) == 166  # 全额预算 50:fixed 200 被 50/0.30=166 封顶
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-old" in cancelled  # 旧 0.20 档不在目标 -> 撤


def test_empty_tier_rules_places_nothing():
    # 模板未配档位规则表 -> 一单都不挂(并告警,不静默)。
    worker, api, db = _make_worker(template={"tier_rules": []})
    api.get_orderbook.return_value = _ob([(0.30, 300)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    api.place_limit_buy.assert_not_called()


def test_book_shift_cancels_old_tier_and_places_new():
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "A",
            "asset_id": "A-y",
            "price": "0.31",
            "original_size": "100",
            "id": "o-stale",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.32, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-stale" in cancelled
    placed = {round(c.args[1], 2) for c in api.place_limit_buy.call_args_list}
    assert 0.30 in placed and 0.31 not in placed


def test_paused_side_cancels_resting_and_other_side_runs():
    # 持有 YES(A-y) -> YES 侧暂停:撤光 YES 在挂买单、不挂 YES 新单;NO(A-n) 照常挂。
    worker, api, db = _make_worker()
    api.get_user_positions.return_value = [
        {"conditionId": "A", "asset": "A-y", "size": 100.0, "curPrice": 0.30}
    ]
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "A",
            "asset_id": "A-y",
            "price": "0.30",
            "original_size": "100",
            "id": "o-yes",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes"), _elig("A", "A-n", "No")])
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-yes" in cancelled  # YES 旧单被撤
    placed_tokens = [c.args[0] for c in api.place_limit_buy.call_args_list]
    assert placed_tokens and all(t == "A-n" for t in placed_tokens)  # 只挂 NO


def test_held_value_deducts_other_side_budget():
    # 持有 YES 市值 50U(100×0.50);max_exposure_usd=100 -> NO 侧预算 50U,fixed 200 被封到 100。
    worker, api, db = _make_worker(
        template={
            "max_exposure_usd": 100,
            "tier_rules": [
                [{"upper": None, "action": {"type": "fixed_shares", "shares": 200}}]
                for _ in range(6)
            ],
        }
    )
    api.get_user_positions.return_value = [
        {"conditionId": "A", "asset": "A-y", "size": 100.0, "curPrice": 0.50}
    ]
    api.get_orderbook.return_value = _ob([(0.50, 1000)], [(0.51, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes"), _elig("A", "A-n", "No")])
    placed = {
        round(c.args[1], 2): c.args[2]
        for c in api.place_limit_buy.call_args_list
        if c.args[0] == "A-n"
    }
    assert placed.get(0.50) == 100  # 预算 100-50=50U;50/0.50=100 封顶(未扣则 200)
    assert not any(c.args[0] == "A-y" for c in api.place_limit_buy.call_args_list)


def test_both_sides_held_cancels_both_and_places_nothing():
    worker, api, db = _make_worker()
    api.get_user_positions.return_value = [
        {"conditionId": "A", "asset": "A-y", "size": 100.0, "curPrice": 0.30},
        {"conditionId": "A", "asset": "A-n", "size": 100.0, "curPrice": 0.30},
    ]
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "A",
            "asset_id": "A-y",
            "price": "0.30",
            "original_size": "100",
            "id": "o-yes",
        },
        {
            "side": "BUY",
            "market": "A",
            "asset_id": "A-n",
            "price": "0.30",
            "original_size": "100",
            "id": "o-no",
        },
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes"), _elig("A", "A-n", "No")])
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-yes" in cancelled and "o-no" in cancelled
    api.place_limit_buy.assert_not_called()


def test_budget_exhausted_still_cancels_paused_side():
    # 已持仓市值吃满单市场敞口(budget_ok=False);暂停侧仍撤光旧单,活跃侧无预算不挂。
    worker, api, db = _make_worker(template={"max_exposure_usd": 20})
    api.get_user_positions.return_value = [
        {"conditionId": "A", "asset": "A-y", "size": 100.0, "curPrice": 0.50}
    ]  # 市值 50U > 20U cap -> budget_ok False
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "A",
            "asset_id": "A-y",
            "price": "0.50",
            "original_size": "100",
            "id": "o-yes",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.50, 1000)], [(0.51, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes"), _elig("A", "A-n", "No")])
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-yes" in cancelled  # 暂停侧仍撤光(不受 budget_ok 影响)
    api.place_limit_buy.assert_not_called()  # 活跃侧无预算 -> 不挂


def test_dropout_cancels_ineligible_market_buys():
    # 在市场 B 有买单,本轮 eligible 只有 A,cancel_dropouts=True -> B 买单被撤、A 照常挂。
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "B",
            "asset_id": "B-y",
            "price": "0.40",
            "original_size": "100",
            "id": "o-b",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")], cancel_dropouts=True)
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-b" in cancelled
    placed_tokens = [c.args[0] for c in api.place_limit_buy.call_args_list]
    assert "A-y" in placed_tokens


def test_dropout_skips_cooldown_but_cancels_others():
    # C 冷却 -> 保留;D 非冷却且跌出 -> 撤。
    worker, api, db = _make_worker()
    db.is_in_cooldown.side_effect = lambda w, m: (m == "C")
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "C",
            "asset_id": "C-y",
            "price": "0.40",
            "original_size": "100",
            "id": "o-c",
        },
        {
            "side": "BUY",
            "market": "D",
            "asset_id": "D-y",
            "price": "0.40",
            "original_size": "100",
            "id": "o-d",
        },
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")], cancel_dropouts=True)
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-d" in cancelled  # 跌出(非冷却) -> 撤
    assert "o-c" not in cancelled  # 冷却 -> 保留


def test_dropout_off_by_default():
    # cancel_dropouts 默认 False(测试挂单路径):跌出市场买单不被撤。
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "B",
            "asset_id": "B-y",
            "price": "0.40",
            "original_size": "100",
            "id": "o-b",
        }
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-b" not in cancelled


def test_dropout_cancels_only_buys_not_sells():
    # 跌出市场若有卖单,只撤买单、卖单保留。
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {
            "side": "BUY",
            "market": "B",
            "asset_id": "B-y",
            "price": "0.40",
            "original_size": "100",
            "id": "o-b-buy",
        },
        {
            "side": "SELL",
            "market": "B",
            "asset_id": "B-y",
            "price": "0.60",
            "original_size": "100",
            "id": "o-b-sell",
        },
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")], cancel_dropouts=True)
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-b-buy" in cancelled and "o-b-sell" not in cancelled


def test_gap_single_places_one_order_highest_qualifying():
    worker, api, db = _make_worker(
        template={
            "placement_mode": "gap_single",
            "tier_rules": [],  # gap_single 不需要 tier_rules,不应被空 tier_rules 闸拦
            "amount_value_table": [
                {"upper": 0.20, "value": 1},
                {"upper": 0.25, "value": 1.5},
                {"upper": 0.31, "value": 2},
            ],
            "gap_wide_cents": 10,
            "gap_mid_cents": 5,
            "gap_high_coeff_sum_min": 20,
            "single_order_min_coeff": 0,
        }
    )
    # 相邻价差 1¢(规则3);买一 0.28 系数 50/(20*2)=1.25 >0 -> 挂一单 20 股 @0.28。
    api.get_orderbook.return_value = _ob([(0.28, 50), (0.27, 40)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes", min_size=20)])
    assert api.place_limit_buy.call_count == 1
    c = api.place_limit_buy.call_args_list[0]
    assert round(c.args[1], 2) == 0.28 and c.args[2] == 20
