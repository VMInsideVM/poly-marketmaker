"""models/database.py — SQLite database layer."""

import sqlite3
import json
import time
from config import DEFAULTS


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
            CREATE TABLE IF NOT EXISTS cooldowns (
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (wallet, market_id)
            );
        """
        )
        self.conn.commit()

    # --- Settings ---

    def get_settings(self) -> dict:
        c = self.conn.cursor()
        c.execute("SELECT key, value FROM settings")
        stored = {row["key"]: json.loads(row["value"]) for row in c.fetchall()}
        result = dict(DEFAULTS)
        result.update(stored)
        return result

    def save_settings(self, settings: dict):
        c = self.conn.cursor()
        for key, value in settings.items():
            c.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
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

    def add_wallet(self, address: str, encrypted_key: str):
        c = self.conn.cursor()
        c.execute(
            "INSERT INTO wallets (address, encrypted_key) VALUES (?, ?)",
            (address, encrypted_key),
        )
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
        c.execute("SELECT address, encrypted_key, enabled, created_at FROM wallets")
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
