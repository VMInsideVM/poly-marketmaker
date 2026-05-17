# Polymarket 自动挂单做市程序 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local web app that automatically places market-making buy orders on Polymarket, monitors fills, and manages sell orders with stop-loss.

**Architecture:** Flask web server with background engine threads (one per wallet). SQLite for persistence. py-clob-client-v2 for Polymarket API. AES-256 encryption for private keys. PyInstaller packaging.

**Tech Stack:** Python 3.10+, Flask, py-clob-client-v2, SQLite, cryptography, threading

---

## File Structure

```
poly简单做市/
├── app.py                  # Flask 应用入口 + 启动逻辑
├── config.py               # 默认配置常量
├── requirements.txt        # 依赖清单
├── models/
│   ├── __init__.py
│   └── database.py         # SQLite schema + CRUD 操作
├── utils/
│   ├── __init__.py
│   └── crypto.py           # AES 加密/解密 + 密码派生
├── api/
│   ├── __init__.py
│   └── polymarket_api.py   # Polymarket CLOB + Rewards API 封装
├── engine/
│   ├── __init__.py
│   ├── scanner.py          # 市场扫描器
│   ├── strategy.py         # 挂单价格策略
│   ├── monitor.py          # 订单监控 + 止损
│   └── manager.py          # 多钱包引擎管理器
├── web/
│   ├── __init__.py
│   ├── routes.py           # Flask 路由 + API
│   ├── templates/
│   │   ├── base.html       # 基础模板（导航 + 布局）
│   │   ├── setup.html      # 首次设置密码页
│   │   ├── login.html      # 登录页
│   │   ├── dashboard.html  # 仪表盘
│   │   ├── config.html     # 配置页
│   │   ├── orders.html     # 订单管理页
│   │   └── history.html    # 历史记录页
│   └── static/
│       ├── style.css       # 全局样式
│       └── app.js          # AJAX 轮询 + 交互逻辑
└── tests/
    ├── __init__.py
    ├── test_crypto.py
    ├── test_database.py
    ├── test_strategy.py
    ├── test_scanner.py
    ├── test_monitor.py
    └── test_manager.py
```

---

### Task 1: Project Setup + Dependencies

**Files:**
- Create: `requirements.txt`
- Create: `config.py`
- Create: `models/__init__.py`
- Create: `utils/__init__.py`
- Create: `api/__init__.py`
- Create: `engine/__init__.py`
- Create: `web/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Create requirements.txt**

```txt
flask==3.1.1
py-clob-client-v2>=0.1.0
cryptography>=44.0.0
```

- [ ] **Step 2: Create config.py with default constants**

```python
"""Default configuration constants."""

DEFAULTS = {
    "min_reward_usd": 100.0,
    "max_spread_cents": 3.0,
    "min_price_cents": 10.0,
    "max_price_cents": 50.0,
    "min_settlement_days": 4,
    "stop_loss_pct": 15.0,
    "scan_interval_sec": 30,
    "fill_check_interval_sec": 5,
    "cooldown_minutes": 20,
}

DB_PATH = "market_maker.db"
HOST = "127.0.0.1"
PORT = 5000
SECRET_KEY = None  # Set at runtime from user password
```

- [ ] **Step 3: Create empty __init__.py files for all packages**

Create empty `__init__.py` in: `models/`, `utils/`, `api/`, `engine/`, `web/`, `tests/`.

- [ ] **Step 4: Install dependencies**

Run: `pip install -r requirements.txt`

- [ ] **Step 5: Commit**

```bash
git init
git add requirements.txt config.py models/__init__.py utils/__init__.py api/__init__.py engine/__init__.py web/__init__.py tests/__init__.py
git commit -m "feat: project setup with dependencies and config defaults"
```

---

### Task 2: Encryption Utility (crypto.py)

**Files:**
- Create: `utils/crypto.py`
- Create: `tests/test_crypto.py`

- [ ] **Step 1: Write tests for crypto module**

```python
"""tests/test_crypto.py"""
import pytest
from utils.crypto import derive_key, encrypt, decrypt


def test_derive_key_deterministic():
    salt = b"test_salt_16bytes"
    key1 = derive_key("mypassword", salt)
    key2 = derive_key("mypassword", salt)
    assert key1 == key2
    assert len(key1) == 32


def test_derive_key_different_passwords():
    salt = b"test_salt_16bytes"
    key1 = derive_key("password1", salt)
    key2 = derive_key("password2", salt)
    assert key1 != key2


def test_encrypt_decrypt_roundtrip():
    key = derive_key("testpass", b"test_salt_16bytes")
    plaintext = "0xabc123def456"
    encrypted = encrypt(plaintext, key)
    assert encrypted != plaintext
    decrypted = decrypt(encrypted, key)
    assert decrypted == plaintext


def test_decrypt_wrong_key_fails():
    key1 = derive_key("correct", b"test_salt_16bytes")
    key2 = derive_key("wrong", b"test_salt_16bytes")
    encrypted = encrypt("secret", key1)
    with pytest.raises(Exception):
        decrypt(encrypted, key2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_crypto.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement crypto.py**

```python
"""utils/crypto.py — AES-256 encryption with PBKDF2 key derivation."""
import os
import base64
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 256-bit key from password using PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,
    )
    return kdf.derive(password.encode("utf-8"))


def encrypt(plaintext: str, key: bytes) -> str:
    """Encrypt plaintext string, return base64-encoded ciphertext."""
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("utf-8")


def decrypt(encrypted: str, key: bytes) -> str:
    """Decrypt base64-encoded ciphertext, return plaintext string."""
    raw = base64.b64decode(encrypted)
    nonce = raw[:12]
    ciphertext = raw[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_crypto.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add utils/crypto.py tests/test_crypto.py
git commit -m "feat: AES-256 encryption utility with PBKDF2 key derivation"
```

---

### Task 3: Database Layer (database.py)

**Files:**
- Create: `models/database.py`
- Create: `tests/test_database.py`

- [ ] **Step 1: Write tests for database module**

```python
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
            wallet="0xABC", market_id="mkt1", token_id="tok1",
            market_name="Test Market", side="buy", order_id="ord1",
            price=0.25, size=1000, status="open",
        )
        orders = db.get_open_orders("0xABC")
        assert len(orders) == 1
        assert orders[0]["order_id"] == "ord1"

    def test_update_order_status(self, db):
        db.record_order(
            wallet="0xABC", market_id="mkt1", token_id="tok1",
            market_name="Test Market", side="buy", order_id="ord1",
            price=0.25, size=1000, status="open",
        )
        db.update_order_status("ord1", "filled")
        orders = db.get_open_orders("0xABC")
        assert len(orders) == 0

    def test_record_position(self, db):
        db.record_position(
            wallet="0xABC", market_id="mkt1", token_id="tok1",
            market_name="Test Market", buy_price=0.25, size=1000,
            sell_order_id="sell1",
        )
        positions = db.get_positions("0xABC")
        assert len(positions) == 1
        assert positions[0]["buy_price"] == 0.25

    def test_record_trade_history(self, db):
        db.record_trade(
            wallet="0xABC", market_id="mkt1", market_name="Test Market",
            side="buy", price=0.25, size=1000, pnl=0.0,
        )
        trades = db.get_trade_history()
        assert len(trades) == 1

    def test_cooldown(self, db):
        db.set_cooldown("0xABC", "mkt1", minutes=20)
        assert db.is_in_cooldown("0xABC", "mkt1") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_database.py -v`
Expected: FAIL

- [ ] **Step 3: Implement database.py**

```python
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
        c.executescript("""
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
        """)
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

    def record_order(self, wallet: str, market_id: str, token_id: str,
                     market_name: str, side: str, order_id: str,
                     price: float, size: int, status: str = "open"):
        c = self.conn.cursor()
        c.execute(
            """INSERT INTO orders
            (order_id, wallet, market_id, token_id, market_name, side, price, size, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_id, wallet, market_id, token_id, market_name, side, price, size, status),
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
            c.execute("SELECT * FROM orders WHERE status = 'open' AND wallet = ?", (wallet,))
        else:
            c.execute("SELECT * FROM orders WHERE status = 'open'")
        return [dict(row) for row in c.fetchall()]

    def get_open_buy_orders(self, wallet: str = None) -> list[dict]:
        c = self.conn.cursor()
        if wallet:
            c.execute("SELECT * FROM orders WHERE status = 'open' AND side = 'buy' AND wallet = ?", (wallet,))
        else:
            c.execute("SELECT * FROM orders WHERE status = 'open' AND side = 'buy'")
        return [dict(row) for row in c.fetchall()]

    # --- Positions ---

    def record_position(self, wallet: str, market_id: str, token_id: str,
                        market_name: str, buy_price: float, size: int,
                        sell_order_id: str = None):
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
            c.execute("SELECT * FROM positions WHERE status = 'open' AND wallet = ?", (wallet,))
        else:
            c.execute("SELECT * FROM positions WHERE status = 'open'")
        return [dict(row) for row in c.fetchall()]

    def close_position(self, position_id: int):
        c = self.conn.cursor()
        c.execute("UPDATE positions SET status = 'closed' WHERE id = ?", (position_id,))
        self.conn.commit()

    # --- Trades ---

    def record_trade(self, wallet: str, market_id: str, market_name: str,
                     side: str, price: float, size: int, pnl: float = 0.0):
        c = self.conn.cursor()
        c.execute(
            """INSERT INTO trades (wallet, market_id, market_name, side, price, size, pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (wallet, market_id, market_name, side, price, size, pnl),
        )
        self.conn.commit()

    def get_trade_history(self, wallet: str = None, start: float = None, end: float = None) -> list[dict]:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_database.py -v`
Expected: All 10 tests passed

- [ ] **Step 5: Commit**

```bash
git add models/database.py tests/test_database.py
git commit -m "feat: SQLite database layer with settings, wallets, orders, positions, trades, cooldowns"
```

---

### Task 4: Polymarket API Wrapper (polymarket_api.py)

**Files:**
- Create: `api/polymarket_api.py`

This module wraps py-clob-client-v2. It will not have unit tests because it's a thin wrapper over an external SDK — we test it via integration in higher-level modules.

- [ ] **Step 1: Implement polymarket_api.py**

```python
"""api/polymarket_api.py — Polymarket CLOB + Rewards API wrapper."""
import logging
import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

logger = logging.getLogger(__name__)

POLYMARKET_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

# Rewards API base — public endpoint
REWARDS_API = "https://data-api.polymarket.com"


class PolymarketAPI:
    """Wrapper for one wallet's Polymarket connection."""

    def __init__(self, private_key: str):
        self.private_key = private_key
        self.client = ClobClient(
            host=POLYMARKET_HOST,
            chain_id=CHAIN_ID,
            key=private_key,
        )
        # Derive or create API credentials (L2 auth)
        self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def get_address(self) -> str:
        """Return wallet address derived from private key."""
        return self.client.get_address()

    # --- Market Data ---

    def get_orderbook(self, token_id: str) -> dict:
        """Get orderbook for a token. Returns {bids: [...], asks: [...]}."""
        return self.client.get_order_book(token_id)

    def get_last_trade_price(self, token_id: str) -> float:
        """Get last trade price for a token."""
        resp = self.client.get_last_trade_price(token_id)
        return float(resp.get("price", 0))

    # --- Balance ---

    def get_balance(self) -> float:
        """Get USDC balance for this wallet."""
        bal = self.client.get_balance_allowance()
        return float(bal.get("balance", 0))

    # --- Order Placement ---

    def place_limit_buy(self, token_id: str, price: float, size: int) -> dict:
        """Place a limit buy order. Returns order response with order_id."""
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side="BUY",
        )
        return self.client.create_and_post_order(order_args, OrderType.GTC)

    def place_limit_sell(self, token_id: str, price: float, size: int) -> dict:
        """Place a limit sell order at specified price."""
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side="SELL",
        )
        return self.client.create_and_post_order(order_args, OrderType.GTC)

    def place_market_sell(self, token_id: str, size: int) -> dict:
        """Place a market sell order (FOK)."""
        order_args = OrderArgs(
            token_id=token_id,
            price=0.001,  # Very low price for market sell
            size=size,
            side="SELL",
        )
        return self.client.create_and_post_order(order_args, OrderType.FOK)

    # --- Order Management ---

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a single order."""
        return self.client.cancel(order_id)

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders for this wallet."""
        return self.client.cancel_all()

    def get_order(self, order_id: str) -> dict:
        """Get order details by ID."""
        return self.client.get_order(order_id)

    def get_open_orders(self) -> list:
        """Get all open orders for this wallet."""
        return self.client.get_orders(open_only=True)

    def get_trades(self) -> list:
        """Get trade history for this wallet."""
        return self.client.get_trades()

    # --- Rewards (public API) ---

    @staticmethod
    def get_rewards_markets() -> list[dict]:
        """Fetch all markets with active rewards from Polymarket data API.

        Returns list of dicts with keys:
          - market_id, token_id, market_name, end_date
          - reward_usd, max_spread, min_size, reward_range_min, reward_range_max
          - tick_size (price increment: 0.01 or 0.001)
        """
        try:
            resp = requests.get(f"{REWARDS_API}/rewards/markets", timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Failed to fetch rewards markets: %s", e)
            return []

    @staticmethod
    def get_market_info(market_id: str) -> dict:
        """Get detailed market info including settlement date."""
        try:
            resp = requests.get(f"{REWARDS_API}/markets/{market_id}", timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Failed to fetch market info for %s: %s", market_id, e)
            return {}
```

- [ ] **Step 2: Commit**

```bash
git add api/polymarket_api.py
git commit -m "feat: Polymarket CLOB + Rewards API wrapper"
```

> **Note:** The exact API response shapes may differ from what's documented. During integration testing (Task 10), we'll adjust field names and parsing as needed based on actual API responses.

---

### Task 5: Order Strategy (strategy.py)

**Files:**
- Create: `engine/strategy.py`
- Create: `tests/test_strategy.py`

- [ ] **Step 1: Write tests for strategy module**

```python
"""tests/test_strategy.py"""
import pytest
from engine.strategy import determine_order_price


def _make_bids(price_size_pairs):
    """Helper: create bids list from [(price, size), ...]."""
    return [{"price": p, "size": s} for p, s in price_size_pairs]


class TestMaxSpread2_TickSize1Cent:
    """max_spread=2, tick_size=0.01 (1 cent increments)."""

    def test_bid1_gt_2000_place_at_bid2(self):
        bids = _make_bids([(0.30, 3000), (0.29, 500)])
        result = determine_order_price(
            bids=bids, max_spread=2, tick_size=0.01,
            reward_range_min=0.28, reward_range_max=0.32,
        )
        assert result == 0.29

    def test_bid1_le_2000_place_at_bid1(self):
        bids = _make_bids([(0.30, 1500), (0.29, 500)])
        result = determine_order_price(
            bids=bids, max_spread=2, tick_size=0.01,
            reward_range_min=0.28, reward_range_max=0.32,
        )
        assert result == 0.30

    def test_result_outside_reward_range_returns_none(self):
        bids = _make_bids([(0.30, 3000), (0.29, 500)])
        result = determine_order_price(
            bids=bids, max_spread=2, tick_size=0.01,
            reward_range_min=0.30, reward_range_max=0.32,
        )
        # bid2 is 0.29, below reward_range_min 0.30
        assert result is None


class TestMaxSpread2_TickSize01Cent:
    """max_spread=2, tick_size=0.001 (0.1 cent increments)."""

    def test_cumulative_gt_6000_next_position(self):
        bids = _make_bids([
            (0.300, 3000), (0.299, 2000), (0.298, 2000), (0.297, 500),
        ])
        # cumsum: 3000, 5000, 7000 -> 7000 > 6000 at 0.298, next = 0.297
        result = determine_order_price(
            bids=bids, max_spread=2, tick_size=0.001,
            reward_range_min=0.290, reward_range_max=0.310,
        )
        assert result == 0.297

    def test_cumulative_never_exceeds_returns_none(self):
        bids = _make_bids([(0.300, 1000), (0.299, 1000)])
        result = determine_order_price(
            bids=bids, max_spread=2, tick_size=0.001,
            reward_range_min=0.290, reward_range_max=0.310,
        )
        assert result is None


class TestMaxSpreadGE3_TickSize1Cent:
    """max_spread>=3, tick_size=0.01."""

    def test_bid1_gt_2000_place_bid2(self):
        bids = _make_bids([(0.30, 3000), (0.29, 500), (0.28, 500)])
        result = determine_order_price(
            bids=bids, max_spread=3, tick_size=0.01,
            reward_range_min=0.27, reward_range_max=0.32,
        )
        assert result == 0.29

    def test_bid1_le_2000_bid2_gt_2000_place_bid3(self):
        bids = _make_bids([(0.30, 1000), (0.29, 3000), (0.28, 500)])
        result = determine_order_price(
            bids=bids, max_spread=3, tick_size=0.01,
            reward_range_min=0.27, reward_range_max=0.32,
        )
        assert result == 0.28

    def test_bid1_bid2_lt_2000_bid3_gt_2000_place_bid4(self):
        bids = _make_bids([
            (0.30, 500), (0.29, 500), (0.28, 3000), (0.27, 100),
        ])
        result = determine_order_price(
            bids=bids, max_spread=3, tick_size=0.01,
            reward_range_min=0.26, reward_range_max=0.32,
        )
        assert result == 0.27

    def test_fallback_keeps_searching(self):
        # All levels <= 2000, keep going until exceed max_spread
        bids = _make_bids([
            (0.30, 500), (0.29, 500), (0.28, 500), (0.27, 500),
        ])
        result = determine_order_price(
            bids=bids, max_spread=3, tick_size=0.01,
            reward_range_min=0.26, reward_range_max=0.32,
        )
        # No level > 2000, exhausted max_spread range
        assert result is None


class TestMaxSpreadGE3_TickSize01Cent:
    """max_spread>=3, tick_size=0.001."""

    def test_cumulative_gt_6000(self):
        bids = _make_bids([
            (0.300, 2000), (0.299, 2000), (0.298, 3000), (0.297, 100),
        ])
        # cumsum: 2000, 4000, 7000 -> next = 0.297
        result = determine_order_price(
            bids=bids, max_spread=3, tick_size=0.001,
            reward_range_min=0.290, reward_range_max=0.310,
        )
        assert result == 0.297
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_strategy.py -v`
Expected: FAIL

- [ ] **Step 3: Implement strategy.py**

```python
"""engine/strategy.py — Order placement strategy based on orderbook depth."""


def determine_order_price(
    bids: list[dict],
    max_spread: int,
    tick_size: float,
    reward_range_min: float,
    reward_range_max: float,
) -> float | None:
    """Determine the price at which to place a buy order.

    Args:
        bids: Sorted list of bid levels [{price, size}, ...], highest price first.
        max_spread: Reward max spread parameter (number of ticks).
        tick_size: Price tick size (0.01 for 1-cent, 0.001 for 0.1-cent).
        reward_range_min: Minimum price in reward range.
        reward_range_max: Maximum price in reward range.

    Returns:
        Price to place order at, or None if no valid position found.
    """
    if not bids:
        return None

    is_fine_tick = tick_size < 0.01  # 0.1-cent increments

    if is_fine_tick:
        price = _strategy_cumulative(bids, reward_range_min, reward_range_max)
    elif max_spread == 2:
        price = _strategy_spread2_coarse(bids, reward_range_min, reward_range_max)
    else:
        price = _strategy_spread_ge3_coarse(
            bids, max_spread, tick_size, reward_range_min, reward_range_max
        )

    return price


def _strategy_cumulative(
    bids: list[dict],
    reward_range_min: float,
    reward_range_max: float,
    threshold: int = 6000,
) -> float | None:
    """0.1-cent tick strategy: find cumulative > threshold, return next position."""
    cumulative = 0
    for i, bid in enumerate(bids):
        cumulative += int(bid["size"])
        if cumulative > threshold:
            # Place at the next position (one level deeper)
            if i + 1 < len(bids):
                target = bids[i + 1]["price"]
            else:
                # No next level exists
                return None
            target = float(target)
            if reward_range_min <= target <= reward_range_max:
                return target
            return None
    return None


def _strategy_spread2_coarse(
    bids: list[dict],
    reward_range_min: float,
    reward_range_max: float,
) -> float | None:
    """max_spread=2, 1-cent tick: bid1 > 2000 -> bid2, else bid1."""
    if not bids:
        return None

    bid1_size = int(bids[0]["size"])
    if bid1_size > 2000:
        if len(bids) < 2:
            return None
        target = float(bids[1]["price"])
    else:
        target = float(bids[0]["price"])

    if reward_range_min <= target <= reward_range_max:
        return target
    return None


def _strategy_spread_ge3_coarse(
    bids: list[dict],
    max_spread: int,
    tick_size: float,
    reward_range_min: float,
    reward_range_max: float,
) -> float | None:
    """max_spread>=3, 1-cent tick: find level with size > 2000, place one below."""
    best_bid_price = float(bids[0]["price"])
    min_price = best_bid_price - max_spread * tick_size

    # Walk through bids, looking for a level > 2000 to place after
    # Logic: check if current level is "big" (> 2000), if so place at next level
    cumulative_small = 0  # track sum of consecutive small levels for the spec logic

    for i, bid in enumerate(bids):
        bid_price = float(bid["price"])
        bid_size = int(bid["size"])

        if bid_price < min_price:
            break  # Exceeded max_spread, stop

        if bid_size > 2000:
            # Place at the next level
            if i + 1 < len(bids):
                target = float(bids[i + 1]["price"])
                if target >= min_price and reward_range_min <= target <= reward_range_max:
                    return target
            return None

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_strategy.py -v`
Expected: All 11 tests passed

- [ ] **Step 5: Commit**

```bash
git add engine/strategy.py tests/test_strategy.py
git commit -m "feat: order placement strategy based on orderbook depth and reward parameters"
```

---

### Task 6: Market Scanner (scanner.py)

**Files:**
- Create: `engine/scanner.py`
- Create: `tests/test_scanner.py`

- [ ] **Step 1: Write tests for scanner**

```python
"""tests/test_scanner.py"""
import time
import pytest
from unittest.mock import MagicMock
from engine.scanner import MarketScanner


def _make_scanner(balance=500.0, settings=None):
    """Create a scanner with mocked API and DB."""
    api = MagicMock()
    db = MagicMock()

    api.get_balance.return_value = balance

    default_settings = {
        "min_reward_usd": 100.0,
        "max_spread_cents": 3.0,
        "min_price_cents": 10.0,
        "max_price_cents": 50.0,
        "min_settlement_days": 4,
    }
    if settings:
        default_settings.update(settings)
    db.get_settings.return_value = default_settings
    db.is_in_cooldown.return_value = False

    return MarketScanner(api, db, "0xABC"), api, db


def _sample_market(**overrides):
    base = {
        "market_id": "mkt1",
        "token_id": "tok1",
        "market_name": "Test Market",
        "end_date": time.time() + 86400 * 10,  # 10 days from now
        "reward_usd": 200.0,
        "max_spread": 2,
        "min_size": 100,
        "reward_range_min": 0.10,
        "reward_range_max": 0.50,
        "tick_size": 0.01,
    }
    base.update(overrides)
    return base


def _sample_orderbook():
    return {
        "bids": [
            {"price": "0.30", "size": "3000"},
            {"price": "0.29", "size": "500"},
        ],
        "asks": [
            {"price": "0.31", "size": "1000"},
        ],
    }


class TestMarketFiltering:
    def test_accepts_valid_market(self):
        scanner, api, db = _make_scanner(balance=500.0)
        api.get_rewards_markets.return_value = [_sample_market()]
        api.get_orderbook.return_value = _sample_orderbook()

        results = scanner.scan()
        assert len(results) == 1

    def test_rejects_low_reward(self):
        scanner, api, db = _make_scanner()
        api.get_rewards_markets.return_value = [_sample_market(reward_usd=50.0)]
        api.get_orderbook.return_value = _sample_orderbook()

        results = scanner.scan()
        assert len(results) == 0

    def test_rejects_near_settlement(self):
        scanner, api, db = _make_scanner()
        api.get_rewards_markets.return_value = [
            _sample_market(end_date=time.time() + 86400 * 2)  # 2 days
        ]
        results = scanner.scan()
        assert len(results) == 0

    def test_rejects_wide_spread(self):
        scanner, api, db = _make_scanner()
        api.get_rewards_markets.return_value = [_sample_market()]
        api.get_orderbook.return_value = {
            "bids": [{"price": "0.25", "size": "1000"}],
            "asks": [{"price": "0.30", "size": "1000"}],  # 5-cent spread
        }
        results = scanner.scan()
        assert len(results) == 0

    def test_rejects_price_out_of_range(self):
        scanner, api, db = _make_scanner()
        api.get_rewards_markets.return_value = [_sample_market()]
        api.get_orderbook.return_value = {
            "bids": [{"price": "0.05", "size": "1000"}],  # below 10 cents
            "asks": [{"price": "0.06", "size": "1000"}],
        }
        results = scanner.scan()
        assert len(results) == 0

    def test_rejects_insufficient_balance(self):
        scanner, api, db = _make_scanner(balance=1.0)  # Only $1
        api.get_rewards_markets.return_value = [
            _sample_market(min_size=1000)  # 1000 * 0.30 = $300 needed
        ]
        api.get_orderbook.return_value = _sample_orderbook()

        results = scanner.scan()
        assert len(results) == 0

    def test_rejects_cooldown_market(self):
        scanner, api, db = _make_scanner()
        db.is_in_cooldown.return_value = True
        api.get_rewards_markets.return_value = [_sample_market()]
        api.get_orderbook.return_value = _sample_orderbook()

        results = scanner.scan()
        assert len(results) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scanner.py -v`
Expected: FAIL

- [ ] **Step 3: Implement scanner.py**

```python
"""engine/scanner.py — Market scanner that filters eligible markets."""
import time
import logging
from engine.strategy import determine_order_price

logger = logging.getLogger(__name__)


class MarketScanner:
    def __init__(self, api, db, wallet_address: str):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address

    def scan(self) -> list[dict]:
        """Scan all reward markets and return eligible ones with order prices."""
        settings = self.db.get_settings()
        balance = self.api.get_balance()
        markets = self.api.get_rewards_markets()
        eligible = []

        for market in markets:
            result = self._evaluate_market(market, settings, balance)
            if result is not None:
                eligible.append(result)

        return eligible

    def _evaluate_market(self, market: dict, settings: dict, balance: float) -> dict | None:
        """Evaluate a single market. Return enriched dict if eligible, else None."""
        # Filter: reward amount
        if market.get("reward_usd", 0) < settings["min_reward_usd"]:
            return None

        # Filter: settlement date
        end_date = market.get("end_date", 0)
        days_remaining = (end_date - time.time()) / 86400
        if days_remaining < settings["min_settlement_days"]:
            return None

        # Filter: cooldown
        if self.db.is_in_cooldown(self.wallet_address, market["market_id"]):
            return None

        # Get orderbook
        try:
            orderbook = self.api.get_orderbook(market["token_id"])
        except Exception as e:
            logger.warning("Failed to get orderbook for %s: %s", market["market_id"], e)
            return None

        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if not bids or not asks:
            return None

        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])

        # Filter: bid-ask spread
        spread_cents = (best_ask - best_bid) * 100
        if spread_cents >= settings["max_spread_cents"]:
            return None

        # Filter: price range
        if best_bid * 100 < settings["min_price_cents"] or best_bid * 100 > settings["max_price_cents"]:
            return None

        # Determine order price using strategy
        order_price = determine_order_price(
            bids=bids,
            max_spread=market.get("max_spread", 2),
            tick_size=market.get("tick_size", 0.01),
            reward_range_min=market.get("reward_range_min", 0),
            reward_range_max=market.get("reward_range_max", 1),
        )
        if order_price is None:
            return None

        # Filter: balance sufficient for min_size
        min_size = market.get("min_size", 0)
        required = min_size * order_price
        if required > balance:
            return None

        return {
            **market,
            "order_price": order_price,
            "order_size": min_size,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_scanner.py -v`
Expected: All 7 tests passed

- [ ] **Step 5: Commit**

```bash
git add engine/scanner.py tests/test_scanner.py
git commit -m "feat: market scanner with filtering logic"
```

---

### Task 7: Order Monitor (monitor.py)

**Files:**
- Create: `engine/monitor.py`
- Create: `tests/test_monitor.py`

- [ ] **Step 1: Write tests for monitor**

```python
"""tests/test_monitor.py"""
import pytest
from unittest.mock import MagicMock, call
from engine.monitor import OrderMonitor


def _make_monitor(settings=None):
    api = MagicMock()
    db = MagicMock()
    default_settings = {
        "stop_loss_pct": 15.0,
        "cooldown_minutes": 20,
    }
    if settings:
        default_settings.update(settings)
    db.get_settings.return_value = default_settings
    monitor = OrderMonitor(api, db, "0xABC")
    return monitor, api, db


class TestCheckBuyOrders:
    def test_filled_order_triggers_sell(self):
        monitor, api, db = _make_monitor()
        db.get_open_buy_orders.return_value = [{
            "order_id": "ord1", "market_id": "mkt1", "token_id": "tok1",
            "market_name": "Test", "price": 0.25, "size": 1000,
        }]
        api.get_order.return_value = {"status": "MATCHED", "size_matched": "1000"}
        api.place_limit_sell.return_value = {"orderID": "sell1"}

        monitor.check_buy_orders()

        db.update_order_status.assert_called_with("ord1", "filled")
        api.place_limit_sell.assert_called_with("tok1", 0.25, 1000)
        db.record_position.assert_called_once()
        db.set_cooldown.assert_called_with("0xABC", "mkt1", 20)

    def test_partial_fill_places_sell_for_filled_portion(self):
        monitor, api, db = _make_monitor()
        db.get_open_buy_orders.return_value = [{
            "order_id": "ord1", "market_id": "mkt1", "token_id": "tok1",
            "market_name": "Test", "price": 0.25, "size": 1000,
        }]
        api.get_order.return_value = {"status": "OPEN", "size_matched": "600"}
        api.place_limit_sell.return_value = {"orderID": "sell1"}

        monitor.check_buy_orders()

        # Should place sell for 600, keep order open
        api.place_limit_sell.assert_called_with("tok1", 0.25, 600)
        db.update_order_status.assert_not_called()

    def test_unfilled_order_no_action(self):
        monitor, api, db = _make_monitor()
        db.get_open_buy_orders.return_value = [{
            "order_id": "ord1", "market_id": "mkt1", "token_id": "tok1",
            "market_name": "Test", "price": 0.25, "size": 1000,
        }]
        api.get_order.return_value = {"status": "OPEN", "size_matched": "0"}

        monitor.check_buy_orders()

        api.place_limit_sell.assert_not_called()


class TestStopLoss:
    def test_triggers_stop_loss_when_price_drops(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        db.get_positions.return_value = [{
            "id": 1, "token_id": "tok1", "buy_price": 0.30,
            "size": 1000, "sell_order_id": "sell1",
        }]
        # Price dropped to 0.24 = 20% drop, exceeds 15% threshold
        api.get_last_trade_price.return_value = 0.24

        monitor.check_stop_loss()

        api.cancel_order.assert_called_with("sell1")
        api.place_market_sell.assert_called_with("tok1", 1000)
        db.close_position.assert_called_with(1)

    def test_no_stop_loss_when_price_stable(self):
        monitor, api, db = _make_monitor(settings={"stop_loss_pct": 15.0})
        db.get_positions.return_value = [{
            "id": 1, "token_id": "tok1", "buy_price": 0.30,
            "size": 1000, "sell_order_id": "sell1",
        }]
        # Price at 0.28 = 6.7% drop, within threshold
        api.get_last_trade_price.return_value = 0.28

        monitor.check_stop_loss()

        api.cancel_order.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_monitor.py -v`
Expected: FAIL

- [ ] **Step 3: Implement monitor.py**

```python
"""engine/monitor.py — Order fill monitoring and stop-loss."""
import logging

logger = logging.getLogger(__name__)


class OrderMonitor:
    def __init__(self, api, db, wallet_address: str):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address
        # Track partially filled amounts to detect new fills
        self._last_matched: dict[str, int] = {}

    def check_buy_orders(self):
        """Check all open buy orders for fills. Handle full and partial fills."""
        settings = self.db.get_settings()
        orders = self.db.get_open_buy_orders(self.wallet_address)

        for order in orders:
            try:
                self._process_order(order, settings)
            except Exception as e:
                logger.error("Error checking order %s: %s", order["order_id"], e)

    def _process_order(self, order: dict, settings: dict):
        order_id = order["order_id"]
        remote = self.api.get_order(order_id)
        size_matched = int(remote.get("size_matched", 0))
        prev_matched = self._last_matched.get(order_id, 0)
        new_fill = size_matched - prev_matched

        if new_fill <= 0:
            return

        self._last_matched[order_id] = size_matched

        # Place sell order for newly filled portion
        sell_resp = self.api.place_limit_sell(
            order["token_id"], order["price"], new_fill
        )
        sell_order_id = sell_resp.get("orderID", "")

        # Record position
        self.db.record_position(
            wallet=self.wallet_address,
            market_id=order["market_id"],
            token_id=order["token_id"],
            market_name=order["market_name"],
            buy_price=order["price"],
            size=new_fill,
            sell_order_id=sell_order_id,
        )

        # Record trade history
        self.db.record_trade(
            wallet=self.wallet_address,
            market_id=order["market_id"],
            market_name=order["market_name"],
            side="buy",
            price=order["price"],
            size=new_fill,
        )

        is_fully_filled = remote.get("status") == "MATCHED"
        if is_fully_filled:
            self.db.update_order_status(order_id, "filled")
            del self._last_matched[order_id]

        # Set cooldown
        self.db.set_cooldown(
            self.wallet_address,
            order["market_id"],
            settings["cooldown_minutes"],
        )
        logger.info("Buy order %s filled %d shares at %.4f", order_id, new_fill, order["price"])

    def check_stop_loss(self):
        """Check positions for stop-loss trigger."""
        settings = self.db.get_settings()
        stop_loss_pct = settings["stop_loss_pct"] / 100.0
        positions = self.db.get_positions(self.wallet_address)

        for pos in positions:
            try:
                self._check_position_stop_loss(pos, stop_loss_pct)
            except Exception as e:
                logger.error("Error checking stop loss for position %s: %s", pos["id"], e)

    def _check_position_stop_loss(self, pos: dict, stop_loss_pct: float):
        current_price = self.api.get_last_trade_price(pos["token_id"])
        threshold = pos["buy_price"] * (1 - stop_loss_pct)

        if current_price <= threshold:
            logger.warning(
                "Stop loss triggered for position %d: price %.4f <= threshold %.4f",
                pos["id"], current_price, threshold,
            )
            # Cancel existing sell order
            if pos.get("sell_order_id"):
                self.api.cancel_order(pos["sell_order_id"])

            # Market sell
            sell_resp = self.api.place_market_sell(pos["token_id"], pos["size"])

            # Record trade
            pnl = (current_price - pos["buy_price"]) * pos["size"]
            self.db.record_trade(
                wallet=self.wallet_address,
                market_id=pos["market_id"],
                market_name=pos.get("market_name", ""),
                side="stop_loss",
                price=current_price,
                size=pos["size"],
                pnl=pnl,
            )
            self.db.close_position(pos["id"])

    def check_sell_orders(self):
        """Check if any sell orders have been filled."""
        positions = self.db.get_positions(self.wallet_address)
        for pos in positions:
            if not pos.get("sell_order_id"):
                continue
            try:
                remote = self.api.get_order(pos["sell_order_id"])
                if remote.get("status") == "MATCHED":
                    pnl = (pos["buy_price"] - pos["buy_price"]) * pos["size"]
                    # Sell at buy_price means ~0 pnl (it's limit at buy price)
                    # Actual pnl depends on fill price from API
                    self.db.record_trade(
                        wallet=self.wallet_address,
                        market_id=pos["market_id"],
                        market_name=pos.get("market_name", ""),
                        side="sell",
                        price=pos["buy_price"],
                        size=pos["size"],
                        pnl=0.0,
                    )
                    self.db.close_position(pos["id"])
                    logger.info("Sell order filled for position %d", pos["id"])
            except Exception as e:
                logger.error("Error checking sell order for position %s: %s", pos["id"], e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_monitor.py -v`
Expected: All 5 tests passed

- [ ] **Step 5: Commit**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "feat: order monitor with fill detection, partial fills, and stop-loss"
```

---

### Task 8: Engine Manager (manager.py)

**Files:**
- Create: `engine/manager.py`
- Create: `tests/test_manager.py`

- [ ] **Step 1: Write tests for manager**

```python
"""tests/test_manager.py"""
import time
import pytest
from unittest.mock import MagicMock, patch
from engine.manager import EngineManager


def _make_manager():
    db = MagicMock()
    db.get_settings.return_value = {
        "scan_interval_sec": 30,
        "fill_check_interval_sec": 5,
        "cooldown_minutes": 20,
        "stop_loss_pct": 15.0,
        "min_reward_usd": 100.0,
        "max_spread_cents": 3.0,
        "min_price_cents": 10.0,
        "max_price_cents": 50.0,
        "min_settlement_days": 4,
    }
    db.list_wallets.return_value = [
        {"address": "0xABC", "encrypted_key": "enc1", "enabled": 1},
        {"address": "0xDEF", "encrypted_key": "enc2", "enabled": 1},
    ]
    db.get_open_buy_orders.return_value = []
    manager = EngineManager(db, encryption_key=b"x" * 32)
    return manager, db


class TestEngineLifecycle:
    def test_start_creates_threads(self):
        manager, db = _make_manager()
        with patch("engine.manager.decrypt", return_value="0x_fake_key"):
            with patch("engine.manager.PolymarketAPI"):
                manager.start_all()
                assert len(manager.engines) == 2
                manager.stop_all()

    def test_stop_cancels_buy_orders(self):
        manager, db = _make_manager()
        mock_api = MagicMock()
        mock_api.get_open_orders.return_value = [
            {"id": "o1", "side": "BUY"},
            {"id": "o2", "side": "SELL"},
        ]
        with patch("engine.manager.decrypt", return_value="0x_fake_key"):
            with patch("engine.manager.PolymarketAPI", return_value=mock_api):
                manager.start_all()
                manager.stop_all()
                # Should only cancel buy orders
                mock_api.cancel_order.assert_called_once_with("o1")

    def test_get_status(self):
        manager, db = _make_manager()
        status = manager.get_status()
        assert isinstance(status, dict)
        assert "engines" in status
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_manager.py -v`
Expected: FAIL

- [ ] **Step 3: Implement manager.py**

```python
"""engine/manager.py — Multi-wallet engine manager."""
import logging
import threading
import time
from api.polymarket_api import PolymarketAPI
from engine.scanner import MarketScanner
from engine.monitor import OrderMonitor
from utils.crypto import decrypt

logger = logging.getLogger(__name__)


class WalletEngine:
    """Engine for a single wallet — runs scan, order, monitor loops."""

    def __init__(self, api: PolymarketAPI, db, wallet_address: str, settings: dict):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address
        self.settings = settings
        self.scanner = MarketScanner(api, db, wallet_address)
        self.monitor = OrderMonitor(api, db, wallet_address)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.running = True
        logger.info("Engine started for wallet %s", self.wallet_address)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=30)
        self.running = False
        self._cancel_buy_orders()
        logger.info("Engine stopped for wallet %s", self.wallet_address)

    def _cancel_buy_orders(self):
        """Cancel all open buy orders on the exchange."""
        try:
            open_orders = self.api.get_open_orders()
            for order in open_orders:
                if order.get("side") == "BUY":
                    self.api.cancel_order(order["id"])
                    logger.info("Cancelled buy order %s", order["id"])
        except Exception as e:
            logger.error("Error cancelling buy orders for %s: %s", self.wallet_address, e)

    def _run(self):
        scan_interval = self.settings["scan_interval_sec"]
        check_interval = self.settings["fill_check_interval_sec"]
        last_scan = 0

        while not self._stop_event.is_set():
            now = time.time()

            # Scan and place orders at scan_interval
            if now - last_scan >= scan_interval:
                self._scan_and_place()
                self._check_existing_orders()
                last_scan = now

            # Check fills and stop-loss at check_interval
            self.monitor.check_buy_orders()
            self.monitor.check_stop_loss()
            self.monitor.check_sell_orders()

            self._stop_event.wait(timeout=check_interval)

    def _scan_and_place(self):
        """Scan markets and place buy orders."""
        try:
            eligible = self.scanner.scan()
            for market in eligible:
                # Check if we already have an open order in this market
                open_orders = self.db.get_open_buy_orders(self.wallet_address)
                already_ordered = any(
                    o["market_id"] == market["market_id"] for o in open_orders
                )
                if already_ordered:
                    continue

                resp = self.api.place_limit_buy(
                    market["token_id"],
                    market["order_price"],
                    market["order_size"],
                )
                order_id = resp.get("orderID", "")
                self.db.record_order(
                    wallet=self.wallet_address,
                    market_id=market["market_id"],
                    token_id=market["token_id"],
                    market_name=market["market_name"],
                    side="buy",
                    order_id=order_id,
                    price=market["order_price"],
                    size=market["order_size"],
                )
                logger.info(
                    "Placed buy order %s: %s @ %.4f x %d",
                    order_id, market["market_name"],
                    market["order_price"], market["order_size"],
                )
        except Exception as e:
            logger.error("Error in scan_and_place for %s: %s", self.wallet_address, e)

    def _check_existing_orders(self):
        """Check if existing orders are still in reward range, cancel if not."""
        try:
            open_orders = self.db.get_open_buy_orders(self.wallet_address)
            for order in open_orders:
                # Re-fetch reward data to check if still in range
                markets = self.api.get_rewards_markets()
                market_data = next(
                    (m for m in markets if m["market_id"] == order["market_id"]), None
                )
                if market_data is None:
                    # Market no longer has rewards, cancel
                    self.api.cancel_order(order["order_id"])
                    self.db.update_order_status(order["order_id"], "cancelled")
                    continue

                reward_min = market_data.get("reward_range_min", 0)
                reward_max = market_data.get("reward_range_max", 1)
                if not (reward_min <= order["price"] <= reward_max):
                    self.api.cancel_order(order["order_id"])
                    self.db.update_order_status(order["order_id"], "cancelled")
                    logger.info(
                        "Cancelled order %s: price %.4f outside reward range [%.4f, %.4f]",
                        order["order_id"], order["price"], reward_min, reward_max,
                    )
        except Exception as e:
            logger.error("Error checking existing orders: %s", e)


class EngineManager:
    """Manages engines for all wallets."""

    def __init__(self, db, encryption_key: bytes):
        self.db = db
        self.encryption_key = encryption_key
        self.engines: dict[str, WalletEngine] = {}

    def start_all(self):
        """Start engines for all enabled wallets."""
        wallets = self.db.list_wallets()
        for w in wallets:
            if w["enabled"]:
                self.start_wallet(w["address"], w["encrypted_key"])

    def stop_all(self):
        """Stop all running engines (cancels buy orders)."""
        for address in list(self.engines.keys()):
            self.stop_wallet(address)

    def restart_all(self):
        """Restart all engines with fresh settings."""
        self.stop_all()
        self.start_all()

    def start_wallet(self, address: str, encrypted_key: str = None):
        if address in self.engines and self.engines[address].running:
            return

        if encrypted_key is None:
            wallets = self.db.list_wallets()
            wallet = next((w for w in wallets if w["address"] == address), None)
            if not wallet:
                return
            encrypted_key = wallet["encrypted_key"]

        private_key = decrypt(encrypted_key, self.encryption_key)
        api = PolymarketAPI(private_key)
        settings = self.db.get_settings()
        engine = WalletEngine(api, self.db, address, settings)
        self.engines[address] = engine
        engine.start()

    def stop_wallet(self, address: str):
        engine = self.engines.pop(address, None)
        if engine:
            engine.stop()

    def startup_recovery(self):
        """Run on program start: cancel stale buy orders, handle offline fills."""
        wallets = self.db.list_wallets()
        for w in wallets:
            try:
                private_key = decrypt(w["encrypted_key"], self.encryption_key)
                api = PolymarketAPI(private_key)

                # Cancel all remaining buy orders
                open_orders = api.get_open_orders()
                for order in open_orders:
                    if order.get("side") == "BUY":
                        api.cancel_order(order["id"])
                        logger.info("Recovery: cancelled stale buy order %s", order["id"])

                # Check for offline fills — orders that filled while program was closed
                db_orders = self.db.get_open_buy_orders(w["address"])
                for db_order in db_orders:
                    remote = api.get_order(db_order["order_id"])
                    size_matched = int(remote.get("size_matched", 0))
                    if size_matched > 0:
                        # Place sell order for filled amount
                        sell_resp = api.place_limit_sell(
                            db_order["token_id"], db_order["price"], size_matched
                        )
                        self.db.record_position(
                            wallet=w["address"],
                            market_id=db_order["market_id"],
                            token_id=db_order["token_id"],
                            market_name=db_order["market_name"],
                            buy_price=db_order["price"],
                            size=size_matched,
                            sell_order_id=sell_resp.get("orderID", ""),
                        )
                        logger.info(
                            "Recovery: found offline fill for %s, placed sell order",
                            db_order["order_id"],
                        )
                    self.db.update_order_status(db_order["order_id"], "recovered")

            except Exception as e:
                logger.error("Recovery error for wallet %s: %s", w["address"], e)

    def get_status(self) -> dict:
        """Get status of all engines."""
        return {
            "engines": {
                addr: {"running": eng.running}
                for addr, eng in self.engines.items()
            }
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_manager.py -v`
Expected: All 3 tests passed

- [ ] **Step 5: Commit**

```bash
git add engine/manager.py tests/test_manager.py
git commit -m "feat: multi-wallet engine manager with lifecycle, recovery, and order monitoring"
```

---

### Task 9: Flask Web Server — Routes + API

**Files:**
- Create: `web/routes.py`

- [ ] **Step 1: Implement routes.py**

```python
"""web/routes.py — Flask routes and API endpoints."""
import os
import hashlib
import logging
from functools import wraps
from flask import (
    Flask, render_template, request, jsonify, session, redirect, url_for, flash,
)
from models.database import Database
from engine.manager import EngineManager
from utils.crypto import derive_key, encrypt
from config import DB_PATH, HOST, PORT

logger = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
)
app.secret_key = os.urandom(32)

db: Database = None
manager: EngineManager = None
encryption_key: bytes = None


def init_app(database: Database):
    global db
    db = database


def init_manager(mgr: EngineManager):
    global manager
    manager = mgr


def set_encryption_key(key: bytes):
    global encryption_key
    encryption_key = key


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# --- Auth Pages ---

@app.route("/setup", methods=["GET", "POST"])
def setup():
    pw_hash, _ = db.get_password()
    if pw_hash is not None:
        return redirect(url_for("login"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(password) < 6:
            flash("密码至少6个字符")
            return render_template("setup.html")
        if password != confirm:
            flash("两次输入的密码不一致")
            return render_template("setup.html")
        salt = os.urandom(16)
        key = derive_key(password, salt)
        hashed = hashlib.sha256(key).hexdigest()
        db.save_password(hashed, salt)
        set_encryption_key(key)
        session["logged_in"] = True
        return redirect(url_for("dashboard"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    pw_hash, salt = db.get_password()
    if pw_hash is None:
        return redirect(url_for("setup"))
    if request.method == "POST":
        password = request.form.get("password", "")
        key = derive_key(password, salt)
        hashed = hashlib.sha256(key).hexdigest()
        if hashed == pw_hash:
            set_encryption_key(key)
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        flash("密码错误")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- Pages ---

@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/config")
@login_required
def config_page():
    return render_template("config.html")


@app.route("/orders")
@login_required
def orders_page():
    return render_template("orders.html")


@app.route("/history")
@login_required
def history_page():
    return render_template("history.html")


# --- API: Settings ---

@app.route("/api/settings", methods=["GET"])
@login_required
def api_get_settings():
    return jsonify(db.get_settings())


@app.route("/api/settings", methods=["POST"])
@login_required
def api_save_settings():
    data = request.get_json()
    db.save_settings(data)
    return jsonify({"ok": True, "message": "参数已保存。如需立即生效，请重启引擎；否则将在下次启动时生效。"})


# --- API: Wallets ---

@app.route("/api/wallets", methods=["GET"])
@login_required
def api_list_wallets():
    wallets = db.list_wallets()
    # Don't expose encrypted keys to frontend
    for w in wallets:
        w.pop("encrypted_key", None)
    # Add balance info if engine is running
    if manager:
        for w in wallets:
            eng = manager.engines.get(w["address"])
            if eng and eng.running:
                try:
                    w["balance"] = eng.api.get_balance()
                except Exception:
                    w["balance"] = None
                w["running"] = True
            else:
                w["balance"] = None
                w["running"] = False
    return jsonify(wallets)


@app.route("/api/wallets", methods=["POST"])
@login_required
def api_add_wallet():
    data = request.get_json()
    private_key = data.get("private_key", "").strip()
    if not private_key:
        return jsonify({"error": "请输入私钥"}), 400

    from api.polymarket_api import PolymarketAPI
    try:
        api = PolymarketAPI(private_key)
        address = api.get_address()
    except Exception as e:
        return jsonify({"error": f"私钥无效: {e}"}), 400

    encrypted = encrypt(private_key, encryption_key)
    try:
        db.add_wallet(address, encrypted)
    except Exception:
        return jsonify({"error": "该钱包已存在"}), 400

    return jsonify({"ok": True, "address": address})


@app.route("/api/wallets/<address>", methods=["DELETE"])
@login_required
def api_remove_wallet(address):
    if manager:
        manager.stop_wallet(address)
    db.remove_wallet(address)
    return jsonify({"ok": True})


@app.route("/api/wallets/<address>/toggle", methods=["POST"])
@login_required
def api_toggle_wallet(address):
    data = request.get_json()
    enabled = data.get("enabled", True)
    db.toggle_wallet(address, enabled)
    return jsonify({"ok": True})


# --- API: Engine Control ---

@app.route("/api/engine/start-all", methods=["POST"])
@login_required
def api_start_all():
    if manager:
        manager.start_all()
    return jsonify({"ok": True})


@app.route("/api/engine/stop-all", methods=["POST"])
@login_required
def api_stop_all():
    if manager:
        manager.stop_all()
    return jsonify({"ok": True, "message": "止损监控已停止，请注意现有持仓风险"})


@app.route("/api/engine/restart", methods=["POST"])
@login_required
def api_restart():
    if manager:
        manager.restart_all()
    return jsonify({"ok": True})


@app.route("/api/engine/<address>/start", methods=["POST"])
@login_required
def api_start_wallet(address):
    if manager:
        manager.start_wallet(address)
    return jsonify({"ok": True})


@app.route("/api/engine/<address>/stop", methods=["POST"])
@login_required
def api_stop_wallet(address):
    if manager:
        manager.stop_wallet(address)
    return jsonify({"ok": True, "message": "止损监控已停止，请注意现有持仓风险"})


# --- API: Orders ---

@app.route("/api/orders", methods=["GET"])
@login_required
def api_get_orders():
    wallet = request.args.get("wallet")
    orders = db.get_open_orders(wallet)
    return jsonify(orders)


@app.route("/api/orders/<order_id>/cancel", methods=["POST"])
@login_required
def api_cancel_order(order_id):
    # Find which wallet owns this order
    orders = db.get_open_orders()
    order = next((o for o in orders if o["order_id"] == order_id), None)
    if not order:
        return jsonify({"error": "订单不存在"}), 404

    if manager:
        eng = manager.engines.get(order["wallet"])
        if eng and eng.running:
            eng.api.cancel_order(order_id)

    db.update_order_status(order_id, "cancelled")
    return jsonify({"ok": True})


@app.route("/api/orders/cancel-batch", methods=["POST"])
@login_required
def api_cancel_batch():
    data = request.get_json()
    order_ids = data.get("order_ids", [])
    for oid in order_ids:
        orders = db.get_open_orders()
        order = next((o for o in orders if o["order_id"] == oid), None)
        if order and manager:
            eng = manager.engines.get(order["wallet"])
            if eng and eng.running:
                eng.api.cancel_order(oid)
            db.update_order_status(oid, "cancelled")
    return jsonify({"ok": True})


# --- API: Positions ---

@app.route("/api/positions", methods=["GET"])
@login_required
def api_get_positions():
    wallet = request.args.get("wallet")
    positions = db.get_positions(wallet)
    # Add current price info
    if manager:
        for pos in positions:
            eng = manager.engines.get(pos["wallet"])
            if eng and eng.running:
                try:
                    pos["current_price"] = eng.api.get_last_trade_price(pos["token_id"])
                    pos["pnl"] = (pos["current_price"] - pos["buy_price"]) * pos["size"]
                    pos["stop_price"] = pos["buy_price"] * (1 - db.get_settings()["stop_loss_pct"] / 100)
                except Exception:
                    pos["current_price"] = None
    return jsonify(positions)


# --- API: History ---

@app.route("/api/history", methods=["GET"])
@login_required
def api_get_history():
    wallet = request.args.get("wallet")
    start = request.args.get("start", type=float)
    end = request.args.get("end", type=float)
    trades = db.get_trade_history(wallet, start, end)
    return jsonify(trades)


# --- API: Dashboard Summary ---

@app.route("/api/dashboard", methods=["GET"])
@login_required
def api_dashboard():
    wallets = db.list_wallets()
    total_orders = len(db.get_open_orders())
    total_positions = len(db.get_positions())
    trades = db.get_trade_history()
    total_pnl = sum(t.get("pnl", 0) for t in trades)

    wallet_summaries = []
    for w in wallets:
        w_orders = db.get_open_orders(w["address"])
        w_positions = db.get_positions(w["address"])
        balance = None
        running = False
        if manager:
            eng = manager.engines.get(w["address"])
            if eng and eng.running:
                running = True
                try:
                    balance = eng.api.get_balance()
                except Exception:
                    pass
        wallet_summaries.append({
            "address": w["address"],
            "enabled": w["enabled"],
            "running": running,
            "balance": balance,
            "open_orders": len(w_orders),
            "positions": len(w_positions),
        })

    return jsonify({
        "total_orders": total_orders,
        "total_positions": total_positions,
        "total_pnl": total_pnl,
        "wallets": wallet_summaries,
    })
```

- [ ] **Step 2: Commit**

```bash
git add web/routes.py
git commit -m "feat: Flask routes with auth, dashboard, config, orders, history APIs"
```

---

### Task 10: HTML Templates

**Files:**
- Create: `web/templates/base.html`
- Create: `web/templates/setup.html`
- Create: `web/templates/login.html`
- Create: `web/templates/dashboard.html`
- Create: `web/templates/config.html`
- Create: `web/templates/orders.html`
- Create: `web/templates/history.html`

- [ ] **Step 1: Create base.html**

```html
<!-- web/templates/base.html -->
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polymarket 做市助手</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
</head>
<body>
    <nav class="navbar">
        <div class="nav-brand">Polymarket 做市助手</div>
        <div class="nav-links">
            <a href="{{ url_for('dashboard') }}" class="{% if request.endpoint == 'dashboard' %}active{% endif %}">仪表盘</a>
            <a href="{{ url_for('config_page') }}" class="{% if request.endpoint == 'config_page' %}active{% endif %}">配置</a>
            <a href="{{ url_for('orders_page') }}" class="{% if request.endpoint == 'orders_page' %}active{% endif %}">订单管理</a>
            <a href="{{ url_for('history_page') }}" class="{% if request.endpoint == 'history_page' %}active{% endif %}">历史记录</a>
            <a href="{{ url_for('logout') }}">退出</a>
        </div>
    </nav>
    <main class="container">
        {% with messages = get_flashed_messages() %}
        {% if messages %}
        <div class="flash-messages">
            {% for msg in messages %}
            <div class="flash">{{ msg }}</div>
            {% endfor %}
        </div>
        {% endif %}
        {% endwith %}
        {% block content %}{% endblock %}
    </main>
    <script src="{{ url_for('static', filename='app.js') }}"></script>
    {% block scripts %}{% endblock %}
</body>
</html>
```

- [ ] **Step 2: Create setup.html and login.html**

```html
<!-- web/templates/setup.html -->
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>初始设置 - Polymarket 做市助手</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
</head>
<body>
    <div class="auth-container">
        <h1>初始设置</h1>
        <p>请设置访问密码（用于加密保护您的钱包私钥）</p>
        {% with messages = get_flashed_messages() %}
        {% if messages %}
        <div class="flash-messages">
            {% for msg in messages %}<div class="flash">{{ msg }}</div>{% endfor %}
        </div>
        {% endif %}
        {% endwith %}
        <form method="POST">
            <div class="form-group">
                <label>设置密码</label>
                <input type="password" name="password" required minlength="6">
            </div>
            <div class="form-group">
                <label>确认密码</label>
                <input type="password" name="confirm" required minlength="6">
            </div>
            <button type="submit" class="btn btn-primary">确认设置</button>
        </form>
    </div>
</body>
</html>
```

```html
<!-- web/templates/login.html -->
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>登录 - Polymarket 做市助手</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
</head>
<body>
    <div class="auth-container">
        <h1>Polymarket 做市助手</h1>
        {% with messages = get_flashed_messages() %}
        {% if messages %}
        <div class="flash-messages">
            {% for msg in messages %}<div class="flash">{{ msg }}</div>{% endfor %}
        </div>
        {% endif %}
        {% endwith %}
        <form method="POST">
            <div class="form-group">
                <label>访问密码</label>
                <input type="password" name="password" required>
            </div>
            <button type="submit" class="btn btn-primary">登录</button>
        </form>
    </div>
</body>
</html>
```

- [ ] **Step 3: Create dashboard.html**

```html
<!-- web/templates/dashboard.html -->
{% extends "base.html" %}
{% block content %}
<div class="page-header">
    <h1>仪表盘</h1>
    <div class="controls">
        <button class="btn btn-success" onclick="engineAction('start-all')">全部启动</button>
        <button class="btn btn-danger" onclick="engineAction('stop-all', '止损监控将停止，确定要停止所有引擎吗？')">全部停止</button>
        <button class="btn btn-warning" onclick="engineAction('restart', '将重启所有引擎以加载新参数，确定吗？')">重启引擎</button>
    </div>
</div>

<div class="summary-cards" id="summary">
    <div class="card"><h3>总挂单</h3><p id="total-orders">-</p></div>
    <div class="card"><h3>总持仓</h3><p id="total-positions">-</p></div>
    <div class="card"><h3>总盈亏</h3><p id="total-pnl">-</p></div>
</div>

<h2>钱包状态</h2>
<table class="data-table" id="wallet-table">
    <thead>
        <tr>
            <th>钱包地址</th>
            <th>余额 (USDC)</th>
            <th>挂单数</th>
            <th>持仓数</th>
            <th>状态</th>
            <th>操作</th>
        </tr>
    </thead>
    <tbody id="wallet-body"></tbody>
</table>
{% endblock %}

{% block scripts %}
<script>
function refreshDashboard() {
    fetch('/api/dashboard').then(r => r.json()).then(data => {
        document.getElementById('total-orders').textContent = data.total_orders;
        document.getElementById('total-positions').textContent = data.total_positions;
        document.getElementById('total-pnl').textContent = (data.total_pnl || 0).toFixed(2) + ' USDC';
        const tbody = document.getElementById('wallet-body');
        tbody.innerHTML = data.wallets.map(w => `
            <tr>
                <td title="${w.address}">${w.address.slice(0,6)}...${w.address.slice(-4)}</td>
                <td>${w.balance != null ? w.balance.toFixed(2) : '-'}</td>
                <td>${w.open_orders}</td>
                <td>${w.positions}</td>
                <td><span class="status ${w.running ? 'running' : 'stopped'}">${w.running ? '运行中' : '已停止'}</span></td>
                <td>
                    ${w.running
                        ? `<button class="btn btn-sm btn-danger" onclick="walletAction('${w.address}', 'stop')">停止</button>`
                        : `<button class="btn btn-sm btn-success" onclick="walletAction('${w.address}', 'start')">启动</button>`
                    }
                </td>
            </tr>
        `).join('');
    });
}

function engineAction(action, confirmMsg) {
    if (confirmMsg && !confirm(confirmMsg)) return;
    fetch(`/api/engine/${action}`, {method: 'POST'}).then(r => r.json()).then(data => {
        if (data.message) alert(data.message);
        refreshDashboard();
    });
}

function walletAction(address, action) {
    if (action === 'stop' && !confirm('止损监控将停止，确定要停止该钱包引擎吗？')) return;
    fetch(`/api/engine/${address}/${action}`, {method: 'POST'}).then(r => r.json()).then(data => {
        if (data.message) alert(data.message);
        refreshDashboard();
    });
}

refreshDashboard();
setInterval(refreshDashboard, 5000);
</script>
{% endblock %}
```

- [ ] **Step 4: Create config.html**

```html
<!-- web/templates/config.html -->
{% extends "base.html" %}
{% block content %}
<h1>配置</h1>

<section class="config-section">
    <h2>钱包管理</h2>
    <div class="form-inline">
        <input type="password" id="new-private-key" placeholder="输入钱包私钥">
        <button class="btn btn-primary" onclick="addWallet()">添加钱包</button>
    </div>
    <table class="data-table" id="wallet-config-table">
        <thead>
            <tr><th>地址</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody id="wallet-config-body"></tbody>
    </table>
</section>

<section class="config-section">
    <h2>策略参数</h2>
    <form id="settings-form">
        <div class="form-grid">
            <div class="form-group">
                <label>最低奖励金额 (USD)</label>
                <input type="number" name="min_reward_usd" step="1">
            </div>
            <div class="form-group">
                <label>买卖价差上限 (美分)</label>
                <input type="number" name="max_spread_cents" step="0.1">
            </div>
            <div class="form-group">
                <label>单价范围下限 (美分)</label>
                <input type="number" name="min_price_cents" step="1">
            </div>
            <div class="form-group">
                <label>单价范围上限 (美分)</label>
                <input type="number" name="max_price_cents" step="1">
            </div>
            <div class="form-group">
                <label>最短结算天数</label>
                <input type="number" name="min_settlement_days" step="1">
            </div>
            <div class="form-group">
                <label>止损比例 (%)</label>
                <input type="number" name="stop_loss_pct" step="1">
            </div>
        </div>

        <h2>运行参数</h2>
        <div class="form-grid">
            <div class="form-group">
                <label>市场扫描间隔 (秒)</label>
                <input type="number" name="scan_interval_sec" step="1">
            </div>
            <div class="form-group">
                <label>成交检查间隔 (秒)</label>
                <input type="number" name="fill_check_interval_sec" step="1">
            </div>
            <div class="form-group">
                <label>成交后冷却时间 (分钟)</label>
                <input type="number" name="cooldown_minutes" step="1">
            </div>
        </div>
        <button type="submit" class="btn btn-primary">保存设置</button>
    </form>
</section>
{% endblock %}

{% block scripts %}
<script>
let originalSettings = {};
let currentSettings = {};

function loadSettings() {
    fetch('/api/settings').then(r => r.json()).then(data => {
        originalSettings = {...data};
        currentSettings = {...data};
        const form = document.getElementById('settings-form');
        Object.keys(data).forEach(key => {
            const input = form.querySelector(`[name="${key}"]`);
            if (input) input.value = data[key];
        });
    });
}

function loadWallets() {
    fetch('/api/wallets').then(r => r.json()).then(wallets => {
        const tbody = document.getElementById('wallet-config-body');
        tbody.innerHTML = wallets.map(w => `
            <tr>
                <td title="${w.address}">${w.address.slice(0,6)}...${w.address.slice(-4)}</td>
                <td>
                    <label class="switch">
                        <input type="checkbox" ${w.enabled ? 'checked' : ''}
                            onchange="toggleWallet('${w.address}', this.checked)">
                        <span class="slider"></span>
                    </label>
                </td>
                <td>
                    <button class="btn btn-sm btn-danger" onclick="removeWallet('${w.address}')">删除</button>
                </td>
            </tr>
        `).join('');
    });
}

function addWallet() {
    const input = document.getElementById('new-private-key');
    const key = input.value.trim();
    if (!key) { alert('请输入私钥'); return; }
    fetch('/api/wallets', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({private_key: key}),
    }).then(r => r.json()).then(data => {
        if (data.error) { alert(data.error); return; }
        input.value = '';
        loadWallets();
    });
}

function removeWallet(address) {
    if (!confirm('确定删除该钱包？相关订单数据将保留在历史记录中。')) return;
    fetch(`/api/wallets/${address}`, {method: 'DELETE'}).then(() => loadWallets());
}

function toggleWallet(address, enabled) {
    fetch(`/api/wallets/${address}/toggle`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled}),
    });
}

document.getElementById('settings-form').addEventListener('submit', function(e) {
    e.preventDefault();
    const formData = new FormData(this);
    const data = {};
    formData.forEach((val, key) => { data[key] = parseFloat(val); });
    fetch('/api/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data),
    }).then(r => r.json()).then(resp => {
        alert(resp.message);
        originalSettings = {...data};
    });
});

// Unsaved changes warning
document.getElementById('settings-form').addEventListener('input', function() {
    const formData = new FormData(this);
    formData.forEach((val, key) => { currentSettings[key] = parseFloat(val); });
});

window.addEventListener('beforeunload', function(e) {
    const hasChanges = Object.keys(originalSettings).some(
        k => currentSettings[k] !== originalSettings[k]
    );
    if (hasChanges) {
        e.preventDefault();
        e.returnValue = '参数已修改但未保存，确定要离开吗？';
    }
});

// Intercept nav clicks for unsaved changes
document.querySelectorAll('.nav-links a').forEach(link => {
    link.addEventListener('click', function(e) {
        const hasChanges = Object.keys(originalSettings).some(
            k => currentSettings[k] !== originalSettings[k]
        );
        if (hasChanges && !confirm('参数已修改但未保存，是否离开？')) {
            e.preventDefault();
        }
    });
});

loadSettings();
loadWallets();
</script>
{% endblock %}
```

- [ ] **Step 5: Create orders.html**

```html
<!-- web/templates/orders.html -->
{% extends "base.html" %}
{% block content %}
<h1>订单管理</h1>

<div class="filter-bar">
    <label>钱包筛选：</label>
    <select id="wallet-filter" onchange="refreshOrders()">
        <option value="">全部</option>
    </select>
</div>

<h2>当前挂单</h2>
<div class="batch-controls">
    <button class="btn btn-sm btn-danger" onclick="cancelSelected()">批量撤单</button>
</div>
<table class="data-table">
    <thead>
        <tr>
            <th><input type="checkbox" id="select-all" onchange="toggleAll(this)"></th>
            <th>钱包</th><th>市场</th><th>价格</th><th>数量</th><th>时间</th><th>操作</th>
        </tr>
    </thead>
    <tbody id="orders-body"></tbody>
</table>

<h2>当前持仓</h2>
<table class="data-table">
    <thead>
        <tr>
            <th>钱包</th><th>市场</th><th>买入价</th><th>当前价</th>
            <th>卖单价</th><th>止损价</th><th>盈亏</th>
        </tr>
    </thead>
    <tbody id="positions-body"></tbody>
</table>
{% endblock %}

{% block scripts %}
<script>
function getFilter() {
    return document.getElementById('wallet-filter').value;
}

function refreshOrders() {
    const wallet = getFilter();
    const qs = wallet ? `?wallet=${wallet}` : '';

    fetch(`/api/orders${qs}`).then(r => r.json()).then(orders => {
        document.getElementById('orders-body').innerHTML = orders.map(o => `
            <tr>
                <td><input type="checkbox" class="order-check" value="${o.order_id}"></td>
                <td title="${o.wallet}">${o.wallet.slice(0,6)}...${o.wallet.slice(-4)}</td>
                <td>${o.market_name}</td>
                <td>${o.price.toFixed(4)}</td>
                <td>${o.size}</td>
                <td>${new Date(o.created_at * 1000).toLocaleString('zh-CN')}</td>
                <td><button class="btn btn-sm btn-danger" onclick="cancelOrder('${o.order_id}')">撤单</button></td>
            </tr>
        `).join('');
    });

    fetch(`/api/positions${qs}`).then(r => r.json()).then(positions => {
        document.getElementById('positions-body').innerHTML = positions.map(p => `
            <tr>
                <td title="${p.wallet}">${p.wallet.slice(0,6)}...${p.wallet.slice(-4)}</td>
                <td>${p.market_name}</td>
                <td>${p.buy_price.toFixed(4)}</td>
                <td>${p.current_price != null ? p.current_price.toFixed(4) : '-'}</td>
                <td>${p.buy_price.toFixed(4)}</td>
                <td>${p.stop_price != null ? p.stop_price.toFixed(4) : '-'}</td>
                <td class="${(p.pnl || 0) >= 0 ? 'profit' : 'loss'}">${(p.pnl || 0).toFixed(2)}</td>
            </tr>
        `).join('');
    });
}

function cancelOrder(orderId) {
    if (!confirm('确定撤销该订单？')) return;
    fetch(`/api/orders/${orderId}/cancel`, {method: 'POST'}).then(() => refreshOrders());
}

function toggleAll(checkbox) {
    document.querySelectorAll('.order-check').forEach(c => c.checked = checkbox.checked);
}

function cancelSelected() {
    const ids = [...document.querySelectorAll('.order-check:checked')].map(c => c.value);
    if (!ids.length) { alert('请先勾选要撤销的订单'); return; }
    if (!confirm(`确定撤销 ${ids.length} 笔订单？`)) return;
    fetch('/api/orders/cancel-batch', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({order_ids: ids}),
    }).then(() => refreshOrders());
}

// Load wallet filter options
fetch('/api/wallets').then(r => r.json()).then(wallets => {
    const select = document.getElementById('wallet-filter');
    wallets.forEach(w => {
        const opt = document.createElement('option');
        opt.value = w.address;
        opt.textContent = `${w.address.slice(0,6)}...${w.address.slice(-4)}`;
        select.appendChild(opt);
    });
});

refreshOrders();
setInterval(refreshOrders, 5000);
</script>
{% endblock %}
```

- [ ] **Step 6: Create history.html**

```html
<!-- web/templates/history.html -->
{% extends "base.html" %}
{% block content %}
<h1>历史记录</h1>

<div class="filter-bar">
    <label>钱包：</label>
    <select id="wallet-filter" onchange="refreshHistory()">
        <option value="">全部</option>
    </select>
    <label>开始日期：</label>
    <input type="date" id="start-date" onchange="refreshHistory()">
    <label>结束日期：</label>
    <input type="date" id="end-date" onchange="refreshHistory()">
</div>

<table class="data-table">
    <thead>
        <tr>
            <th>时间</th><th>钱包</th><th>市场</th>
            <th>方向</th><th>价格</th><th>数量</th><th>盈亏</th>
        </tr>
    </thead>
    <tbody id="history-body"></tbody>
</table>
{% endblock %}

{% block scripts %}
<script>
function refreshHistory() {
    const wallet = document.getElementById('wallet-filter').value;
    const startDate = document.getElementById('start-date').value;
    const endDate = document.getElementById('end-date').value;
    const params = new URLSearchParams();
    if (wallet) params.set('wallet', wallet);
    if (startDate) params.set('start', new Date(startDate).getTime() / 1000);
    if (endDate) params.set('end', new Date(endDate + 'T23:59:59').getTime() / 1000);

    fetch(`/api/history?${params}`).then(r => r.json()).then(trades => {
        const sideLabels = {buy: '买入', sell: '卖出', stop_loss: '止损'};
        document.getElementById('history-body').innerHTML = trades.map(t => `
            <tr>
                <td>${new Date(t.created_at * 1000).toLocaleString('zh-CN')}</td>
                <td title="${t.wallet}">${t.wallet.slice(0,6)}...${t.wallet.slice(-4)}</td>
                <td>${t.market_name}</td>
                <td class="${t.side === 'buy' ? 'buy-side' : t.side === 'stop_loss' ? 'loss' : 'sell-side'}">${sideLabels[t.side] || t.side}</td>
                <td>${t.price.toFixed(4)}</td>
                <td>${t.size}</td>
                <td class="${t.pnl >= 0 ? 'profit' : 'loss'}">${t.pnl.toFixed(2)}</td>
            </tr>
        `).join('');
    });
}

fetch('/api/wallets').then(r => r.json()).then(wallets => {
    const select = document.getElementById('wallet-filter');
    wallets.forEach(w => {
        const opt = document.createElement('option');
        opt.value = w.address;
        opt.textContent = `${w.address.slice(0,6)}...${w.address.slice(-4)}`;
        select.appendChild(opt);
    });
});

refreshHistory();
</script>
{% endblock %}
```

- [ ] **Step 7: Commit**

```bash
git add web/templates/
git commit -m "feat: HTML templates for all pages — dashboard, config, orders, history"
```

---

### Task 11: Static Assets (CSS + JS)

**Files:**
- Create: `web/static/style.css`
- Create: `web/static/app.js`

- [ ] **Step 1: Create style.css**

```css
/* web/static/style.css */
* { margin: 0; padding: 0; box-sizing: border-box; }

body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f5f5f5;
    color: #333;
}

/* Navbar */
.navbar {
    background: #1a1a2e;
    color: white;
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0 24px;
    height: 56px;
}
.nav-brand { font-size: 18px; font-weight: 600; }
.nav-links a {
    color: #aaa;
    text-decoration: none;
    margin-left: 24px;
    font-size: 14px;
}
.nav-links a.active, .nav-links a:hover { color: white; }

/* Container */
.container { max-width: 1200px; margin: 24px auto; padding: 0 24px; }

/* Cards */
.summary-cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 24px; }
.card {
    background: white;
    border-radius: 8px;
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}
.card h3 { font-size: 13px; color: #888; margin-bottom: 8px; }
.card p { font-size: 24px; font-weight: 600; }

/* Tables */
.data-table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 24px; }
.data-table th { background: #f8f9fa; text-align: left; padding: 12px 16px; font-size: 13px; color: #666; }
.data-table td { padding: 12px 16px; border-top: 1px solid #eee; font-size: 14px; }
.data-table tr:hover { background: #f8f9fa; }

/* Status */
.status { padding: 4px 8px; border-radius: 4px; font-size: 12px; }
.status.running { background: #d4edda; color: #155724; }
.status.stopped { background: #f8d7da; color: #721c24; }

/* Buttons */
.btn {
    padding: 8px 16px;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    font-size: 14px;
    font-weight: 500;
}
.btn-primary { background: #4361ee; color: white; }
.btn-success { background: #2ec4b6; color: white; }
.btn-danger { background: #e63946; color: white; }
.btn-warning { background: #f4a261; color: white; }
.btn-sm { padding: 4px 10px; font-size: 12px; }
.btn:hover { opacity: 0.9; }

/* Forms */
.form-group { margin-bottom: 16px; }
.form-group label { display: block; font-size: 13px; color: #666; margin-bottom: 4px; }
.form-group input, .form-group select {
    width: 100%;
    padding: 8px 12px;
    border: 1px solid #ddd;
    border-radius: 6px;
    font-size: 14px;
}
.form-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 24px; }
.form-inline { display: flex; gap: 12px; margin-bottom: 16px; }
.form-inline input { flex: 1; padding: 8px 12px; border: 1px solid #ddd; border-radius: 6px; }

/* Auth */
.auth-container {
    max-width: 400px;
    margin: 100px auto;
    background: white;
    padding: 40px;
    border-radius: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
    text-align: center;
}
.auth-container h1 { margin-bottom: 16px; }
.auth-container p { color: #666; margin-bottom: 24px; }

/* Config sections */
.config-section { background: white; border-radius: 8px; padding: 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.config-section h2 { margin-bottom: 16px; font-size: 16px; }

/* Filter bar */
.filter-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.filter-bar select, .filter-bar input { padding: 6px 10px; border: 1px solid #ddd; border-radius: 6px; }

/* PnL colors */
.profit { color: #2ec4b6; }
.loss { color: #e63946; }
.buy-side { color: #4361ee; }
.sell-side { color: #2ec4b6; }

/* Flash messages */
.flash-messages { margin-bottom: 16px; }
.flash { background: #fff3cd; color: #856404; padding: 12px; border-radius: 6px; margin-bottom: 8px; }

/* Batch controls */
.batch-controls { margin-bottom: 8px; }

/* Page header */
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
.controls { display: flex; gap: 8px; }

/* Toggle switch */
.switch { position: relative; display: inline-block; width: 40px; height: 22px; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider {
    position: absolute; cursor: pointer; inset: 0;
    background: #ccc; border-radius: 22px; transition: 0.3s;
}
.slider::before {
    content: ""; position: absolute; height: 16px; width: 16px;
    left: 3px; bottom: 3px; background: white; border-radius: 50%; transition: 0.3s;
}
.switch input:checked + .slider { background: #2ec4b6; }
.switch input:checked + .slider::before { transform: translateX(18px); }
```

- [ ] **Step 2: Create app.js (minimal shared utilities)**

```javascript
/* web/static/app.js — Shared utilities */

// Empty for now — page-specific JS is inline in templates.
// This file exists for any shared helpers we add later.
```

- [ ] **Step 3: Commit**

```bash
git add web/static/
git commit -m "feat: CSS styling and static assets"
```

---

### Task 12: Application Entry Point (app.py)

**Files:**
- Create: `app.py`

- [ ] **Step 1: Implement app.py**

```python
"""app.py — Application entry point."""
import logging
import signal
import sys
import webbrowser
import threading
from models.database import Database
from engine.manager import EngineManager
from web.routes import app, init_app, init_manager, set_encryption_key
from config import DB_PATH, HOST, PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("market_maker.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

db = Database(DB_PATH)
manager = None


def on_shutdown(signum=None, frame=None):
    """Graceful shutdown: stop engines, close DB."""
    logger.info("Shutting down...")
    if manager:
        manager.stop_all()
    db.close()
    logger.info("Shutdown complete.")
    sys.exit(0)


def main():
    global manager

    db.init()
    init_app(db)

    # Check if password is set; if so, we can't auto-start engines
    # (user must log in first to provide the password for decryption)
    pw_hash, _ = db.get_password()

    # Register shutdown handler
    signal.signal(signal.SIGINT, on_shutdown)
    signal.signal(signal.SIGTERM, on_shutdown)

    # Open browser after a short delay
    def open_browser():
        import time
        time.sleep(1.5)
        url = f"http://{HOST}:{PORT}"
        if pw_hash is None:
            url += "/setup"
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()

    logger.info("Starting Polymarket Market Maker on http://%s:%d", HOST, PORT)
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Update routes.py login to initialize manager after authentication**

In `web/routes.py`, update the `login` and `setup` POST handlers to create the EngineManager and run recovery after successful authentication:

```python
# Add to the end of the setup() POST handler, after set_encryption_key(key):
    global manager
    manager_instance = EngineManager(db, key)
    init_manager(manager_instance)
    manager = manager_instance

# Add to the end of the login() POST handler, after set_encryption_key(key):
    global manager
    if manager is None:
        manager_instance = EngineManager(db, key)
        manager_instance.startup_recovery()
        init_manager(manager_instance)
        manager = manager_instance
```

Add `manager = None` at module level in routes.py and import EngineManager at top.

- [ ] **Step 3: Verify the app starts**

Run: `python app.py`
Expected: Browser opens to setup page at `http://127.0.0.1:5000/setup`

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat: application entry point with graceful shutdown and browser auto-open"
```

---

### Task 13: Integration Testing

**Files:** No new files — manual testing in browser

- [ ] **Step 1: Start the app and set up password**

Run: `python app.py`
1. Browser opens to `/setup`
2. Set a password
3. Verify redirect to dashboard

- [ ] **Step 2: Test wallet management**

1. Go to Config page
2. Add a wallet (test with a known private key on Polygon)
3. Verify wallet appears in list
4. Toggle enabled/disabled
5. Remove wallet

- [ ] **Step 3: Test settings save/load**

1. Change strategy parameters
2. Click save, verify success message
3. Refresh page, verify values persisted
4. Modify a value, try navigating away — verify unsaved changes warning

- [ ] **Step 4: Test engine controls**

1. Add a wallet with real credentials
2. Click "全部启动" on dashboard
3. Verify wallet shows "运行中"
4. Click "全部停止", verify stop warning message
5. Test "重启引擎"

- [ ] **Step 5: Test order management**

1. While engine is running, check Orders page
2. Verify wallet filter works
3. Test manual cancel if any orders exist

- [ ] **Step 6: Fix any issues found during testing**

Address any bugs or UI issues discovered during manual testing.

- [ ] **Step 7: Commit fixes**

```bash
git add -u
git commit -m "fix: integration testing fixes"
```

---

### Task 14: PyInstaller Packaging

**Files:**
- Create: `build.spec` (or use command-line)

- [ ] **Step 1: Install PyInstaller**

Run: `pip install pyinstaller`

- [ ] **Step 2: Create the exe**

Run:
```bash
pyinstaller --onefile --name "PolyMarketMaker" \
    --add-data "web/templates:web/templates" \
    --add-data "web/static:web/static" \
    --hidden-import "py_clob_client" \
    app.py
```

On Windows, use `;` instead of `:` for `--add-data` separator:
```bash
pyinstaller --onefile --name "PolyMarketMaker" --add-data "web/templates;web/templates" --add-data "web/static;web/static" --hidden-import "py_clob_client" app.py
```

- [ ] **Step 3: Test the packaged exe**

Run: `dist/PolyMarketMaker.exe` (or `dist/PolyMarketMaker` on Linux/Mac)
1. Verify browser opens
2. Verify setup flow works
3. Verify static files load correctly

- [ ] **Step 4: Commit build config**

```bash
git add PolyMarketMaker.spec
git commit -m "feat: PyInstaller packaging configuration"
```
