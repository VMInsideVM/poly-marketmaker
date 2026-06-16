"""tests/test_database.py"""

import os
import pytest
from models.database import Database


def test_settings_page_engine_strategy_split_roundtrip(tmp_path):
    """/api/settings 的拆分机制:引擎键存 settings、策略键存默认模板,GET 合并取回。"""
    from config import ENGINE_DEFAULTS, TEMPLATE_DEFAULTS

    database = Database(str(tmp_path / "split.db"))
    database.init()
    try:
        data = {"scan_interval_sec": 90, "min_reward_usd": 250.0}
        engine = {k: v for k, v in data.items() if k in ENGINE_DEFAULTS}
        strategy = {k: v for k, v in data.items() if k in TEMPLATE_DEFAULTS}
        database.save_settings(engine)
        database.save_template(database.get_default_template_id(), strategy)
        merged = dict(database.get_settings())
        merged.update(database.get_template(database.get_default_template_id()))
        assert merged["scan_interval_sec"] == 90
        assert merged["min_reward_usd"] == 250.0
        # 策略键没有泄漏进引擎级 settings
        assert "stop_loss_pct" not in database.get_settings()
    finally:
        database.close()


def test_config_split_engine_and_template_defaults():
    from config import ENGINE_DEFAULTS, TEMPLATE_DEFAULTS, DEFAULTS

    assert set(ENGINE_DEFAULTS) == {
        "scan_interval_sec",
        "fill_check_interval_sec",
        "cooldown_minutes",
        "rewards_cache_ttl_sec",
        "discovery_interval_sec",
    }
    assert TEMPLATE_DEFAULTS["excluded_categories"] == ["sports", "esports", "weather"]
    assert TEMPLATE_DEFAULTS["min_reward_usd"] == 100.0
    assert "stop_loss_pct" not in TEMPLATE_DEFAULTS
    # 向后兼容:DEFAULTS 仍是两者合并(get_settings 在最后一个任务前仍用它)
    assert DEFAULTS["scan_interval_sec"] == 30
    assert DEFAULTS["min_reward_usd"] == 100.0
    assert set(ENGINE_DEFAULTS) & set(TEMPLATE_DEFAULTS) == set()
    assert ENGINE_DEFAULTS["discovery_interval_sec"] == 14400


def test_template_defaults_has_multitier_keys():
    from config import TEMPLATE_DEFAULTS

    assert TEMPLATE_DEFAULTS["tiers_k"] == 6
    assert len(TEMPLATE_DEFAULTS["tier_rules"]) == 6
    assert TEMPLATE_DEFAULTS["tier_rules"][0] == [
        {"upper": None, "action": {"type": "min_size"}}
    ]
    assert TEMPLATE_DEFAULTS["max_exposure_usd"] == 250
    assert TEMPLATE_DEFAULTS["max_exposure_shares"] == 500
    assert TEMPLATE_DEFAULTS["max_concurrent_markets"] == 10
    assert TEMPLATE_DEFAULTS["min_price_double_cents"] == 10
    assert "order_size_mode" not in TEMPLATE_DEFAULTS
    assert "order_size_custom_usd" not in TEMPLATE_DEFAULTS
    assert "max_buy_orders_per_wallet" not in TEMPLATE_DEFAULTS


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    database.init()
    yield database
    database.close()


class TestSettings:
    def test_get_default_settings_engine_only(self, db):
        settings = db.get_settings()
        assert settings["scan_interval_sec"] == 30
        assert settings["cooldown_minutes"] == 20
        assert "stop_loss_pct" not in settings
        assert "min_reward_usd" not in settings

    def test_save_and_load_engine_settings(self, db):
        db.save_settings({"scan_interval_sec": 60})
        assert db.get_settings()["scan_interval_sec"] == 60

    def test_save_password_hash(self, db):
        db.save_password("hashed_pw", b"salt_bytes")
        pw_hash, salt = db.get_password()
        assert pw_hash == "hashed_pw"
        assert salt == b"salt_bytes"


class TestSettingsToTemplateMigration:
    def _make_legacy_state(self, db):
        """模拟老库:templates 空,settings 里有策略键+引擎键。"""
        import json as _json

        c = db.conn.cursor()
        c.execute("DELETE FROM template_settings")
        c.execute("DELETE FROM templates")
        c.execute("DELETE FROM settings")
        legacy = {
            "stop_loss_pct": 10.0,
            "min_reward_usd": 200.0,
            "order_size_mode": "balance",
            "scan_interval_sec": 45,
        }
        for k, v in legacy.items():
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)", (k, _json.dumps(v))
            )
        db.conn.commit()

    def test_strategy_keys_move_to_default_template(self, db):
        self._make_legacy_state(db)
        db._migrate()
        t = db.get_template(db.get_default_template_id())
        assert t["min_reward_usd"] == 200.0
        # order_size_mode was retired (SP2); migration no longer moves it
        # stop_loss_pct was retired (SP3 T5); migration no longer moves it

    def test_engine_keys_stay_in_settings(self, db):
        self._make_legacy_state(db)
        db._migrate()
        assert db.get_settings()["scan_interval_sec"] == 45

    def test_strategy_keys_removed_from_settings(self, db):
        self._make_legacy_state(db)
        db._migrate()
        c = db.conn.cursor()
        c.execute("SELECT key FROM settings")
        keys = {row["key"] for row in c.fetchall()}
        # min_reward_usd is still in TEMPLATE_DEFAULTS → migrated out of settings
        assert "min_reward_usd" not in keys
        # stop_loss_pct retired (SP3 T5): no longer in TEMPLATE_DEFAULTS,
        # so migration leaves it stranded in settings (harmless; nothing reads it)

    def test_migration_idempotent(self, db):
        self._make_legacy_state(db)
        db._migrate()
        db._migrate()
        assert len([t for t in db.list_templates() if t["name"] == "默认"]) == 1
        assert db.get_template(db.get_default_template_id())["min_reward_usd"] == 200.0

    def test_fresh_install_no_copy(self, db):
        t = db.get_template(db.get_default_template_id())
        assert t["min_reward_usd"] == 100.0  # 默认值,非迁移值


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

    def test_signature_type_defaults_to_2(self, db):
        db.add_wallet("0xABC", "encrypted_key_1")
        assert db.list_wallets()[0]["signature_type"] == 2

    def test_signature_type_round_trips(self, db):
        db.add_wallet("0xPROXY", "enc", funder="0xfund", signature_type=1)
        w = db.list_wallets()[0]
        assert w["signature_type"] == 1
        assert w["funder"] == "0xfund"

    def test_migration_adds_signature_type_to_old_db(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "old.db")
        # 模拟老库:wallets 表没有 signature_type 列
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE wallets (address TEXT PRIMARY KEY, encrypted_key TEXT NOT NULL,"
            " funder TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,"
            " created_at REAL NOT NULL DEFAULT (strftime('%s','now')))"
        )
        conn.execute(
            "INSERT INTO wallets (address, encrypted_key) VALUES ('0xOLD', 'enc')"
        )
        conn.commit()
        conn.close()

        database = Database(db_path)
        database.init()  # 应迁移补列,默认 2
        try:
            w = database.list_wallets()[0]
            assert w["address"] == "0xOLD"
            assert w["signature_type"] == 2
        finally:
            database.close()


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

    def test_save_and_get_tags(self, db):
        db.save_eligible_markets(
            [
                {
                    "market_id": "A",
                    "token_id": "t1",
                    "market_name": "M",
                    "outcome": "Yes",
                    "daily_reward": 50,
                    "order_price": 0,
                    "order_size": 0,
                    "tags": ["sports"],
                }
            ]
        )
        assert db.get_eligible_markets()[0]["tags"] == ["sports"]

    def test_tags_default_empty_list(self, db):
        db.save_eligible_markets(
            [
                {
                    "market_id": "B",
                    "token_id": "t2",
                    "market_name": "M2",
                    "outcome": "No",
                    "daily_reward": 50,
                    "order_price": 0,
                    "order_size": 0,
                }
            ]
        )
        assert db.get_eligible_markets()[0]["tags"] == []


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


def test_template_defaults_has_exit_keys():
    from config import TEMPLATE_DEFAULTS

    assert TEMPLATE_DEFAULTS["theta_loss_cents"] == 2
    assert TEMPLATE_DEFAULTS["theta_stop_cents"] == 5
    assert TEMPLATE_DEFAULTS["case_a_mode"] == "ask"
    assert "stop_loss_pct" not in TEMPLATE_DEFAULTS


def test_template_defaults_has_per_share_thresholds():
    from config import TEMPLATE_DEFAULTS

    t = TEMPLATE_DEFAULTS["per_share_reward_thresholds"]
    assert t == {"20": 0.30, "50": 0.30, "100": 0.30, "200": 0.30, "250": 0.30}


class TestTemplateCRUD:
    def test_create_and_list_templates(self, db):
        tid = db.create_template("保守")
        assert isinstance(tid, int)
        assert "保守" in [t["name"] for t in db.list_templates()]

    def test_create_duplicate_name_raises(self, db):
        db.create_template("保守")
        with pytest.raises(Exception):
            db.create_template("保守")

    def test_save_and_get_template_merges_defaults(self, db):
        tid = db.create_template("激进")
        db.save_template(tid, {"max_spread_cents": 6.0})
        t = db.get_template(tid)
        assert t["max_spread_cents"] == 6.0
        assert t["min_reward_usd"] == 100.0
        assert t["excluded_categories"] == ["sports", "esports", "weather"]
        assert "scan_interval_sec" not in t

    def test_default_template_exists_after_init(self, db):
        assert isinstance(db.get_default_template_id(), int)
        assert "默认" in [t["name"] for t in db.list_templates()]

    def test_set_wallet_template_and_get_template_for(self, db):
        db.add_wallet("0xAAA", "k")
        tid = db.create_template("激进")
        db.save_template(tid, {"max_spread_cents": 6.0})
        db.set_wallet_template("0xAAA", tid)
        assert db.get_template_for("0xAAA")["max_spread_cents"] == 6.0

    def test_get_template_for_null_falls_back_to_default(self, db):
        db.add_wallet("0xBBB", "k")
        t = db.get_template_for("0xBBB")
        assert t["min_reward_usd"] == 100.0
        assert t["max_spread_cents"] == 3.0

    def test_get_template_for_unknown_wallet_falls_back_to_default(self, db):
        assert db.get_template_for("0xNOPE")["max_spread_cents"] == 3.0

    def test_delete_default_template_rejected(self, db):
        with pytest.raises(Exception):
            db.delete_template(db.get_default_template_id())

    def test_delete_template_rebinds_wallets_to_default(self, db):
        db.add_wallet("0xCCC", "k")
        tid = db.create_template("临时")
        db.set_wallet_template("0xCCC", tid)
        db.delete_template(tid)
        w = next(w for w in db.list_wallets() if w["address"] == "0xCCC")
        assert w["template_id"] is None
        assert db.get_template_for("0xCCC")["max_spread_cents"] == 3.0


class TestTemplateSchema:
    def test_templates_table_exists(self, db):
        c = db.conn.cursor()
        c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='templates'"
        )
        assert c.fetchone() is not None

    def test_template_settings_table_exists(self, db):
        c = db.conn.cursor()
        c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='template_settings'"
        )
        assert c.fetchone() is not None

    def test_wallets_has_template_id_column(self, db):
        c = db.conn.cursor()
        c.execute("PRAGMA table_info(wallets)")
        cols = {row[1] for row in c.fetchall()}
        assert "template_id" in cols
