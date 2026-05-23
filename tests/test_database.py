"""tests/test_database.py"""

import os
import pytest
from models.database import Database


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    database.init()
    yield database
    database.close()


class TestSettings:
    def test_get_default_settings(self, db):
        settings = db.get_settings()
        assert settings["min_reward_usd"] == 100.0
        assert settings["stop_loss_pct"] == 15.0

    def test_save_and_load_settings(self, db):
        db.save_settings({"min_reward_usd": 200.0, "stop_loss_pct": 10.0})
        settings = db.get_settings()
        assert settings["min_reward_usd"] == 200.0
        assert settings["stop_loss_pct"] == 10.0

    def test_save_password_hash(self, db):
        db.save_password("hashed_pw", b"salt_bytes")
        pw_hash, salt = db.get_password()
        assert pw_hash == "hashed_pw"
        assert salt == b"salt_bytes"


class TestWallets:
    def test_add_and_list_wallets(self, db):
        db.add_wallet("0xABC", "encrypted_key_1")
        db.add_wallet("0xDEF", "encrypted_key_2")
        wallets = db.list_wallets()
        assert len(wallets) == 2
        assert wallets[0]["address"] == "0xABC"

    def test_remove_wallet(self, db):
        db.add_wallet("0xABC", "encrypted_key_1")
        db.remove_wallet("0xABC")
        assert len(db.list_wallets()) == 0

    def test_toggle_wallet(self, db):
        db.add_wallet("0xABC", "encrypted_key_1")
        db.toggle_wallet("0xABC", enabled=False)
        wallet = db.list_wallets()[0]
        assert wallet["enabled"] == 0


class TestOrders:
    def test_record_and_get_buy_order(self, db):
        db.record_order(
            wallet="0xABC",
            market_id="mkt1",
            token_id="tok1",
            market_name="Test Market",
            side="buy",
            order_id="ord1",
            price=0.25,
            size=1000,
            status="open",
        )
        orders = db.get_open_orders("0xABC")
        assert len(orders) == 1
        assert orders[0]["order_id"] == "ord1"

    def test_update_order_status(self, db):
        db.record_order(
            wallet="0xABC",
            market_id="mkt1",
            token_id="tok1",
            market_name="Test Market",
            side="buy",
            order_id="ord1",
            price=0.25,
            size=1000,
            status="open",
        )
        db.update_order_status("ord1", "filled")
        orders = db.get_open_orders("0xABC")
        assert len(orders) == 0

    def test_record_position(self, db):
        db.record_position(
            wallet="0xABC",
            market_id="mkt1",
            token_id="tok1",
            market_name="Test Market",
            buy_price=0.25,
            size=1000,
            sell_order_id="sell1",
        )
        positions = db.get_positions("0xABC")
        assert len(positions) == 1
        assert positions[0]["buy_price"] == 0.25

    def test_record_trade_history(self, db):
        db.record_trade(
            wallet="0xABC",
            market_id="mkt1",
            market_name="Test Market",
            side="buy",
            price=0.25,
            size=1000,
            pnl=0.0,
        )
        trades = db.get_trade_history()
        assert len(trades) == 1

    def test_cooldown(self, db):
        db.set_cooldown("0xABC", "mkt1", minutes=20)
        assert db.is_in_cooldown("0xABC", "mkt1") is True


class TestActions:
    def test_record_and_get_action(self, db):
        db.record_action(
            wallet="0xABC",
            market_id="mkt1",
            action_type="take_profit_sell",
            side="卖出",
            price=0.33,
            size=120.0,
            reason="买单成交，按成交价挂等价止盈卖单",
            price_basis="卖价=买入成交价 0.3300；来源：CLOB get_trades",
        )
        rows = db.get_actions()
        assert len(rows) == 1
        r = rows[0]
        assert r["wallet"] == "0xABC"
        assert r["action_type"] == "take_profit_sell"
        assert r["side"] == "卖出"
        assert r["price"] == 0.33
        assert r["size"] == 120.0
        assert "止盈" in r["reason"]
        assert "成交价" in r["price_basis"]
        assert r["created_at"] > 0

    def test_get_actions_filters_by_wallet(self, db):
        db.record_action("0xA", "m", "cancel_remainder", "-", -1, 0, "r", "b")
        db.record_action("0xB", "m", "cancel_remainder", "-", -1, 0, "r", "b")
        assert len(db.get_actions(wallet="0xA")) == 1
        assert db.get_actions(wallet="0xA")[0]["wallet"] == "0xA"

    def test_get_actions_filters_by_action_types(self, db):
        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.3, 1, "r", "b")
        db.record_action("0xA", "m", "step3_replace_new", "买入", 0.4, 1, "r", "b")
        db.record_action("0xA", "m", "stoploss_market_sell", "卖出", 0.2, 1, "r", "b")
        rows = db.get_actions(action_types=["take_profit_sell", "stoploss_market_sell"])
        assert len(rows) == 2
        assert {r["action_type"] for r in rows} == {
            "take_profit_sell",
            "stoploss_market_sell",
        }

    def test_get_actions_filters_by_time_range(self, db):
        import time

        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.3, 1, "r", "b")
        now = time.time()
        assert len(db.get_actions(start=now - 3600, end=now + 3600)) == 1
        assert len(db.get_actions(start=now + 3600)) == 0

    def test_get_actions_orders_desc_by_created_at(self, db):
        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.1, 1, "first", "b")
        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.2, 1, "second", "b")
        rows = db.get_actions()
        assert rows[0]["reason"] == "second"
        assert rows[1]["reason"] == "first"


class TestEligibleMarkets:
    def test_min_cost_round_trips(self, db):
        db.save_eligible_markets(
            [
                {
                    "market_id": "0xabc",
                    "token_id": "tok1",
                    "market_name": "M",
                    "outcome": "Yes",
                    "daily_reward": 150.0,
                    "order_price": 0.30,
                    "order_size": 100,
                    "min_cost": 29.0,
                }
            ]
        )
        rows = db.get_eligible_markets()
        assert len(rows) == 1
        assert rows[0]["min_cost"] == 29.0


class TestMarketMeta:
    def test_upsert_and_get(self, db):
        db.upsert_market_meta("0xc1", "市场A", "slug-a", "evt-a")
        meta = db.get_market_meta()
        assert meta["0xc1"] == {
            "name": "市场A",
            "market_slug": "slug-a",
            "event_slug": "evt-a",
        }

    def test_upsert_updates_same_condition_id(self, db):
        db.upsert_market_meta("0xc1", "旧名", "old", "")
        db.upsert_market_meta("0xc1", "新名", "new", "evt")
        meta = db.get_market_meta()
        assert len(meta) == 1
        assert meta["0xc1"]["name"] == "新名"
        assert meta["0xc1"]["market_slug"] == "new"
        assert meta["0xc1"]["event_slug"] == "evt"

    def test_empty_condition_id_skipped(self, db):
        db.upsert_market_meta("", "x", "y", "z")
        assert db.get_market_meta() == {}

    def test_accumulates_across_calls(self, db):
        db.upsert_market_meta("0xc1", "A", "", "")
        db.upsert_market_meta("0xc2", "B", "", "")
        assert set(db.get_market_meta().keys()) == {"0xc1", "0xc2"}


class TestBlacklist:
    def test_add_and_get_ids(self, db):
        db.add_to_blacklist("0xc1", "spam market")
        assert db.get_blacklist_ids() == {"0xc1"}

    def test_get_blacklist_returns_note_and_time(self, db):
        db.add_to_blacklist("0xc1", "spam")
        bl = db.get_blacklist()
        assert len(bl) == 1
        assert bl[0]["condition_id"] == "0xc1"
        assert bl[0]["note"] == "spam"
        assert "added_at" in bl[0]

    def test_remove(self, db):
        db.add_to_blacklist("0xc1", "")
        db.remove_from_blacklist("0xc1")
        assert db.get_blacklist_ids() == set()

    def test_add_dedups_and_updates_note(self, db):
        db.add_to_blacklist("0xc1", "first")
        db.add_to_blacklist("0xc1", "second")
        bl = db.get_blacklist()
        assert len(bl) == 1
        assert bl[0]["note"] == "second"

    def test_empty_condition_id_skipped(self, db):
        db.add_to_blacklist("", "x")
        assert db.get_blacklist_ids() == set()

    def test_empty_when_none(self, db):
        assert db.get_blacklist_ids() == set()
        assert db.get_blacklist() == []
