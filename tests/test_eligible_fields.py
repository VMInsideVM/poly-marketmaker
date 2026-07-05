"""tests/test_eligible_fields.py — filter_for_template 产出行的市场级新字段。"""

from engine.scanner import MarketScanner


class _DB:
    def is_in_cooldown(self, wallet, cid):
        return False

    def get_blacklist_ids(self):
        return set()


def _candidate():
    # 中点=(0.54+0.56)/2=0.55;rewards_max_spread=4 -> 奖励范围 [0.51,0.59];spread=0.02
    return {
        "condition_id": "c1",
        "question": "Will XYZ resolve YES?",
        "market_reward": 40.0,
        "rewards_config": [{"rate_per_day": 40.0}],
        "rewards_max_spread": 4,
        "rewards_min_size": 100,
        "neg_risk": False,
        "tags": [],
        "end_date": "",
        "tokens": [{"token_id": "tY", "outcome": "YES", "price": 0.55}],
        "_orderbooks": {
            "tY": {
                "bids": [{"price": "0.54", "size": "150"}],
                "asks": [{"price": "0.56", "size": "150"}],
                "tick_size": "0.01",
                "spread": 0.02,
            }
        },
    }


def _template():
    return {
        "min_reward_usd": 1.0,
        "min_price_cents": 1,
        "max_price_cents": 99,
        "max_spread_cents": 3,
        "min_settlement_days": 0,
        "included_categories": [],
        "include_other": True,
    }


def test_eligible_row_has_reward_range_and_spread():
    sc = MarketScanner(api=None, db=_DB(), wallet_address="0xabc")
    rows = sc.filter_for_template([_candidate()], _template(), "0xabc")
    assert len(rows) == 1
    r = rows[0]
    assert round(r["reward_range_min"], 4) == 0.51
    assert round(r["reward_range_max"], 4) == 0.59
    assert round(r["spread_cents"], 4) == 2.0


def test_null_rewards_min_size_skipped_not_crash():
    # API 可能返回 rewards_min_size: null —— 不应 int(None) 崩溃,该市场跳过即可
    c = _candidate()
    c["rewards_min_size"] = None
    sc = MarketScanner(api=None, db=_DB(), wallet_address="0xabc")
    rows = sc.filter_for_template([c], _template(), "0xabc")
    assert rows == []
