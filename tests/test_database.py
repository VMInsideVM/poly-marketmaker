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
