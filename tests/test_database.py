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
