"""models/database.py — SQLite database layer."""

import sqlite3
import json
import time
from config import DEFAULTS, ENGINE_DEFAULTS, TEMPLATE_DEFAULTS


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = None

    def init(self):
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def close(self):
        if self.conn:
            self.conn.close()

    def _create_tables(self):
        c = self.conn.cursor()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                password_hash TEXT NOT NULL,
                salt BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallets (
                address TEXT PRIMARY KEY,
                encrypted_key TEXT NOT NULL,
                funder TEXT NOT NULL DEFAULT '',
                signature_type INTEGER NOT NULL DEFAULT 2,
                proxy TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                market_name TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                market_name TEXT NOT NULL,
                buy_price REAL NOT NULL,
                size INTEGER NOT NULL,
                sell_order_id TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                market_name TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size INTEGER NOT NULL,
                pnl REAL NOT NULL DEFAULT 0.0,
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL DEFAULT -1,
                size REAL NOT NULL DEFAULT 0,   -- REAL: fractional fill sizes (trades.size is INTEGER by legacy design)
                reason TEXT NOT NULL DEFAULT '',
                price_basis TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS cooldowns (
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (wallet, market_id)
            );
            CREATE TABLE IF NOT EXISTS eligible_markets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                market_name TEXT NOT NULL,
                outcome TEXT NOT NULL,
                market_competitiveness REAL DEFAULT 0,
                daily_reward REAL NOT NULL,
                rewards_max_spread INTEGER DEFAULT 0,
                rewards_min_size INTEGER DEFAULT 0,
                tick_size REAL DEFAULT 0.01,
                tick_size_str TEXT DEFAULT '0.01',
                neg_risk INTEGER DEFAULT 0,
                reward_range_min REAL DEFAULT 0,
                reward_range_max REAL DEFAULT 1,
                spread_cents REAL DEFAULT -1,
                order_price REAL NOT NULL,
                order_size INTEGER NOT NULL,
                min_cost REAL DEFAULT 0,
                end_date TEXT DEFAULT '',
                tags TEXT DEFAULT '[]',
                scanned_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS market_meta (
                condition_id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                market_slug TEXT NOT NULL DEFAULT '',
                event_slug TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS blacklist (
                condition_id TEXT PRIMARY KEY,
                note TEXT NOT NULL DEFAULT '',
                added_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS template_settings (
                template_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (template_id, key)
            );
        """
        )
        self.conn.commit()
        self._migrate()

    def _migrate(self):
        """Apply schema migrations for existing databases."""
        c = self.conn.cursor()
        c.execute("PRAGMA table_info(wallets)")
        cols = {row[1] for row in c.fetchall()}
        if "funder" not in cols:
            c.execute("ALTER TABLE wallets ADD COLUMN funder TEXT NOT NULL DEFAULT ''")
            self.conn.commit()
        if "signature_type" not in cols:
            c.execute(
                "ALTER TABLE wallets ADD COLUMN signature_type INTEGER NOT NULL DEFAULT 2"
            )
            self.conn.commit()
        if "proxy" not in cols:
            c.execute("ALTER TABLE wallets ADD COLUMN proxy TEXT NOT NULL DEFAULT ''")
            self.conn.commit()
        c.execute("PRAGMA table_info(eligible_markets)")
        em_cols = {row[1] for row in c.fetchall()}
        if em_cols and "min_cost" not in em_cols:
            c.execute("ALTER TABLE eligible_markets ADD COLUMN min_cost REAL DEFAULT 0")
            self.conn.commit()
        c.execute("PRAGMA table_info(eligible_markets)")
        em_cols2 = {row[1] for row in c.fetchall()}
        if em_cols2 and "tags" not in em_cols2:
            c.execute("ALTER TABLE eligible_markets ADD COLUMN tags TEXT DEFAULT '[]'")
            self.conn.commit()
        c.execute("PRAGMA table_info(eligible_markets)")
        em_cols3 = {row[1] for row in c.fetchall()}
        if em_cols3 and "spread_cents" not in em_cols3:
            c.execute(
                "ALTER TABLE eligible_markets ADD COLUMN spread_cents REAL DEFAULT -1"
            )
            self.conn.commit()
        c.execute("PRAGMA table_info(wallets)")
        wcols = {row[1] for row in c.fetchall()}
        if "template_id" not in wcols:
            c.execute("ALTER TABLE wallets ADD COLUMN template_id INTEGER")
            self.conn.commit()
        c.execute("SELECT COUNT(*) AS n FROM templates")
        if c.fetchone()["n"] == 0:
            c.execute(
                "INSERT INTO templates (name) VALUES (?)", (self.DEFAULT_TEMPLATE_NAME,)
            )
            default_id = c.lastrowid
            c.execute("SELECT key, value FROM settings")
            for row in list(c.fetchall()):
                if row["key"] in TEMPLATE_DEFAULTS:
                    c.execute(
                        "INSERT OR REPLACE INTO template_settings "
                        "(template_id, key, value) VALUES (?, ?, ?)",
                        (default_id, row["key"], row["value"]),
                    )
                    c.execute("DELETE FROM settings WHERE key = ?", (row["key"],))
            self.conn.commit()

    # --- Settings ---

    def get_settings(self) -> dict:
        """引擎级全局参数(策略级参数见 get_template_for)。"""
        c = self.conn.cursor()
        c.execute("SELECT key, value FROM settings")
        stored = {row["key"]: json.loads(row["value"]) for row in c.fetchall()}
        result = dict(ENGINE_DEFAULTS)
        for k in ENGINE_DEFAULTS:
            if k in stored:
                result[k] = stored[k]
        return result

    def save_settings(self, settings: dict):
        c = self.conn.cursor()
        for key, value in settings.items():
            c.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
        self.conn.commit()

    # --- Templates ---

    DEFAULT_TEMPLATE_NAME = "默认"

    def create_template(self, name: str) -> int:
        c = self.conn.cursor()
        c.execute("INSERT INTO templates (name) VALUES (?)", (name,))
        self.conn.commit()
        return c.lastrowid

    def list_templates(self) -> list[dict]:
        c = self.conn.cursor()
        c.execute("SELECT id, name, created_at FROM templates ORDER BY id")
        return [dict(row) for row in c.fetchall()]

    def get_default_template_id(self) -> int:
        c = self.conn.cursor()
        c.execute(
            "SELECT id FROM templates WHERE name = ?", (self.DEFAULT_TEMPLATE_NAME,)
        )
        row = c.fetchone()
        if row is None:
            return self.create_template(self.DEFAULT_TEMPLATE_NAME)
        return row["id"]

    def get_template(self, template_id: int) -> dict:
        """TEMPLATE_DEFAULTS 合并该模板的覆盖键(逐键 + JSON 值)。"""
        c = self.conn.cursor()
        c.execute(
            "SELECT key, value FROM template_settings WHERE template_id = ?",
            (template_id,),
        )
        stored = {row["key"]: json.loads(row["value"]) for row in c.fetchall()}
        result = dict(TEMPLATE_DEFAULTS)
        result.update(stored)
        return result

    def save_template(self, template_id: int, values: dict):
        c = self.conn.cursor()
        for key, value in values.items():
            c.execute(
                "INSERT OR REPLACE INTO template_settings (template_id, key, value) "
                "VALUES (?, ?, ?)",
                (template_id, key, json.dumps(value)),
            )
        self.conn.commit()

    def rename_template(self, template_id: int, name: str):
        c = self.conn.cursor()
        c.execute("UPDATE templates SET name = ? WHERE id = ?", (name, template_id))
        self.conn.commit()

    def set_wallet_template(self, address: str, template_id: int):
        c = self.conn.cursor()
        c.execute(
            "UPDATE wallets SET template_id = ? WHERE address = ?",
            (template_id, address),
        )
        self.conn.commit()

    def get_template_for(self, address: str) -> dict:
        """按钱包地址取其绑定模板;NULL/未知钱包回落默认模板。"""
        c = self.conn.cursor()
        c.execute("SELECT template_id FROM wallets WHERE address = ?", (address,))
        row = c.fetchone()
        tid = row["template_id"] if row and row["template_id"] is not None else None
        if tid is None:
            tid = self.get_default_template_id()
        return self.get_template(tid)

    def delete_template(self, template_id: int):
        if template_id == self.get_default_template_id():
            raise ValueError("默认模板不可删除")
        c = self.conn.cursor()
        c.execute(
            "UPDATE wallets SET template_id = NULL WHERE template_id = ?",
            (template_id,),
        )
        c.execute("DELETE FROM template_settings WHERE template_id = ?", (template_id,))
        c.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        self.conn.commit()

    # --- Auth ---

    def save_password(self, password_hash: str, salt: bytes):
        c = self.conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO auth (id, password_hash, salt) VALUES (1, ?, ?)",
            (password_hash, salt),
        )
        self.conn.commit()

    def get_password(self):
        c = self.conn.cursor()
        c.execute("SELECT password_hash, salt FROM auth WHERE id = 1")
        row = c.fetchone()
        if row is None:
            return None, None
        return row["password_hash"], row["salt"]

    # --- Wallets ---

    def add_wallet(
        self,
        address: str,
        encrypted_key: str,
        funder: str = "",
        signature_type: int = 2,
        proxy: str = "",
    ):
        c = self.conn.cursor()
        c.execute(
            "INSERT INTO wallets (address, encrypted_key, funder, signature_type, proxy) "
            "VALUES (?, ?, ?, ?, ?)",
            (address, encrypted_key, funder, signature_type, proxy),
        )
        self.conn.commit()

    def set_wallet_proxy(self, address: str, proxy: str):
        """更新某钱包的代理串(明文)。下次启动引擎/重建该 worker 时生效。"""
        c = self.conn.cursor()
        c.execute("UPDATE wallets SET proxy = ? WHERE address = ?", (proxy, address))
        self.conn.commit()

    def remove_wallet(self, address: str):
        c = self.conn.cursor()
        c.execute("DELETE FROM wallets WHERE address = ?", (address,))
        self.conn.commit()

    def toggle_wallet(self, address: str, enabled: bool):
        c = self.conn.cursor()
        c.execute(
            "UPDATE wallets SET enabled = ? WHERE address = ?",
            (1 if enabled else 0, address),
        )
        self.conn.commit()

    def list_wallets(self) -> list[dict]:
        c = self.conn.cursor()
        c.execute(
            "SELECT address, encrypted_key, funder, signature_type, proxy, enabled, "
            "created_at, template_id FROM wallets"
        )
        return [dict(row) for row in c.fetchall()]

    # --- Orders ---

    def record_order(
        self,
        wallet: str,
        market_id: str,
        token_id: str,
        market_name: str,
        side: str,
        order_id: str,
        price: float,
        size: int,
        status: str = "open",
    ):
        c = self.conn.cursor()
        c.execute(
            """INSERT INTO orders
            (order_id, wallet, market_id, token_id, market_name, side, price, size, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_id,
                wallet,
                market_id,
                token_id,
                market_name,
                side,
                price,
                size,
                status,
            ),
        )
        self.conn.commit()

    def update_order_status(self, order_id: str, status: str):
        c = self.conn.cursor()
        now = time.time()
        c.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?",
            (status, now, order_id),
        )
        self.conn.commit()

    def get_open_orders(self, wallet: str = None) -> list[dict]:
        c = self.conn.cursor()
        if wallet:
            c.execute(
                "SELECT * FROM orders WHERE status = 'open' AND wallet = ?", (wallet,)
            )
        else:
            c.execute("SELECT * FROM orders WHERE status = 'open'")
        return [dict(row) for row in c.fetchall()]

    def get_open_buy_orders(self, wallet: str = None) -> list[dict]:
        c = self.conn.cursor()
        if wallet:
            c.execute(
                "SELECT * FROM orders WHERE status = 'open' AND side = 'buy' AND wallet = ?",
                (wallet,),
            )
        else:
            c.execute("SELECT * FROM orders WHERE status = 'open' AND side = 'buy'")
        return [dict(row) for row in c.fetchall()]

    # --- Positions ---

    def record_position(
        self,
        wallet: str,
        market_id: str,
        token_id: str,
        market_name: str,
        buy_price: float,
        size: int,
        sell_order_id: str = None,
    ):
        c = self.conn.cursor()
        c.execute(
            """INSERT INTO positions
            (wallet, market_id, token_id, market_name, buy_price, size, sell_order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (wallet, market_id, token_id, market_name, buy_price, size, sell_order_id),
        )
        self.conn.commit()

    def get_positions(self, wallet: str = None) -> list[dict]:
        c = self.conn.cursor()
        if wallet:
            c.execute(
                "SELECT * FROM positions WHERE status = 'open' AND wallet = ?",
                (wallet,),
            )
        else:
            c.execute("SELECT * FROM positions WHERE status = 'open'")
        return [dict(row) for row in c.fetchall()]

    def close_position(self, position_id: int):
        c = self.conn.cursor()
        c.execute("UPDATE positions SET status = 'closed' WHERE id = ?", (position_id,))
        self.conn.commit()

    # --- Trades ---

    def record_trade(
        self,
        wallet: str,
        market_id: str,
        market_name: str,
        side: str,
        price: float,
        size: int,
        pnl: float = 0.0,
    ):
        c = self.conn.cursor()
        c.execute(
            """INSERT INTO trades (wallet, market_id, market_name, side, price, size, pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (wallet, market_id, market_name, side, price, size, pnl),
        )
        self.conn.commit()

    def get_trade_history(
        self, wallet: str = None, start: float = None, end: float = None
    ) -> list[dict]:
        c = self.conn.cursor()
        query = "SELECT * FROM trades WHERE 1=1"
        params = []
        if wallet:
            query += " AND wallet = ?"
            params.append(wallet)
        if start:
            query += " AND created_at >= ?"
            params.append(start)
        if end:
            query += " AND created_at <= ?"
            params.append(end)
        query += " ORDER BY created_at DESC"
        c.execute(query, params)
        return [dict(row) for row in c.fetchall()]

    # --- Actions (monitor order-mutating actions log) ---

    def record_action(
        self,
        wallet: str,
        market_id: str,
        action_type: str,
        side: str,
        price: float,
        size: float,
        reason: str,
        price_basis: str,
    ):
        c = self.conn.cursor()
        c.execute(
            """INSERT INTO actions
            (wallet, market_id, action_type, side, price, size,
             reason, price_basis)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                wallet,
                market_id,
                action_type,
                side,
                price,
                size,
                reason,
                price_basis,
            ),
        )
        self.conn.commit()

    def get_actions(
        self,
        wallet: str = None,
        start: float = None,
        end: float = None,
        action_types: list[str] = None,
    ) -> list[dict]:
        c = self.conn.cursor()
        query = "SELECT * FROM actions WHERE 1=1"
        params = []
        if wallet:
            query += " AND wallet = ?"
            params.append(wallet)
        if start:
            query += " AND created_at >= ?"
            params.append(start)
        if end:
            query += " AND created_at <= ?"
            params.append(end)
        if action_types:
            placeholders = ",".join("?" * len(action_types))
            query += f" AND action_type IN ({placeholders})"
            params.extend(action_types)
        query += " ORDER BY created_at DESC, id DESC"
        c.execute(query, params)
        return [dict(row) for row in c.fetchall()]

    # --- Cooldowns ---

    def set_cooldown(self, wallet: str, market_id: str, minutes: int):
        expires_at = time.time() + minutes * 60
        c = self.conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO cooldowns (wallet, market_id, expires_at) VALUES (?, ?, ?)",
            (wallet, market_id, expires_at),
        )
        self.conn.commit()

    def is_in_cooldown(self, wallet: str, market_id: str) -> bool:
        c = self.conn.cursor()
        c.execute(
            "SELECT expires_at FROM cooldowns WHERE wallet = ? AND market_id = ?",
            (wallet, market_id),
        )
        row = c.fetchone()
        if row is None:
            return False
        return time.time() < row["expires_at"]

    # --- Eligible Markets ---

    def save_eligible_markets(self, markets: list[dict]):
        """Replace all eligible markets with new scan results."""
        c = self.conn.cursor()
        c.execute("DELETE FROM eligible_markets")
        now = time.time()
        for m in markets:
            c.execute(
                """INSERT INTO eligible_markets
                (market_id, token_id, market_name, outcome, market_competitiveness,
                 daily_reward, rewards_max_spread, rewards_min_size,
                 tick_size, tick_size_str, neg_risk,
                 reward_range_min, reward_range_max, spread_cents,
                 order_price, order_size, min_cost, end_date, tags, scanned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    m.get("market_id", ""),
                    m.get("token_id", ""),
                    m.get("market_name", ""),
                    m.get("outcome", ""),
                    m.get("market_competitiveness", 0),
                    m.get("daily_reward", 0),
                    m.get("rewards_max_spread", 0),
                    m.get("rewards_min_size", 0),
                    m.get("tick_size", 0.01),
                    m.get("tick_size_str", "0.01"),
                    1 if m.get("neg_risk", False) else 0,
                    m.get("reward_range_min", 0),
                    m.get("reward_range_max", 1),
                    m.get("spread_cents", -1),
                    m.get("order_price", 0),
                    m.get("order_size", 0),
                    m.get("min_cost", 0),
                    m.get("end_date", ""),
                    json.dumps(m.get("tags", []) or []),
                    now,
                ),
            )
        self.conn.commit()

    def get_eligible_markets(self) -> list[dict]:
        """Get all eligible markets from last scan."""
        c = self.conn.cursor()
        c.execute("SELECT * FROM eligible_markets ORDER BY market_competitiveness DESC")
        out = []
        for row in c.fetchall():
            d = dict(row)
            try:
                d["tags"] = json.loads(d.get("tags") or "[]")
            except (ValueError, TypeError):
                d["tags"] = []
            out.append(d)
        return out

    # --- Market Meta (condition_id -> name + slugs, persistent across scans) ---

    def upsert_market_meta(
        self, condition_id: str, name: str, market_slug: str = "", event_slug: str = ""
    ):
        """Insert or update market metadata. No-op for empty condition_id."""
        if not condition_id:
            return
        c = self.conn.cursor()
        c.execute(
            """INSERT OR REPLACE INTO market_meta
            (condition_id, name, market_slug, event_slug, updated_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                condition_id,
                name or "",
                market_slug or "",
                event_slug or "",
                time.time(),
            ),
        )
        self.conn.commit()

    def get_market_meta(self) -> dict:
        """Return {condition_id: {name, market_slug, event_slug}}."""
        c = self.conn.cursor()
        c.execute("SELECT condition_id, name, market_slug, event_slug FROM market_meta")
        return {
            row["condition_id"]: {
                "name": row["name"],
                "market_slug": row["market_slug"],
                "event_slug": row["event_slug"],
            }
            for row in c.fetchall()
        }

    # --- Blacklist (global, by condition_id) ---

    def add_to_blacklist(self, condition_id: str, note: str = ""):
        """加入(或更新)一个 condition_id 到全局黑名单。空 id 跳过。"""
        if not condition_id:
            return
        c = self.conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO blacklist (condition_id, note, added_at) "
            "VALUES (?, ?, ?)",
            (condition_id, note or "", time.time()),
        )
        self.conn.commit()

    def remove_from_blacklist(self, condition_id: str):
        c = self.conn.cursor()
        c.execute("DELETE FROM blacklist WHERE condition_id = ?", (condition_id,))
        self.conn.commit()

    def get_blacklist(self) -> list[dict]:
        """全部黑名单条目(最新在前),供管理界面用。"""
        c = self.conn.cursor()
        c.execute(
            "SELECT condition_id, note, added_at FROM blacklist ORDER BY added_at DESC"
        )
        return [dict(row) for row in c.fetchall()]

    def get_blacklist_ids(self) -> set:
        """黑名单 condition_id 集合,供拦截热路径快速 membership 判断。"""
        c = self.conn.cursor()
        c.execute("SELECT condition_id FROM blacklist")
        return {row["condition_id"] for row in c.fetchall()}
