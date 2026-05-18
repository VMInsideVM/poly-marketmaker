# Funder = Gnosis Safe 代理地址（maker 修正）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `signature_type != 0` 时下单的 `maker` 永远等于由私钥确定性派生出的 Polymarket Gnosis Safe 代理地址，而非静默兜底成 EOA。

**Architecture:** 方案 A——在 `PolymarketAPI` 内派生 Safe 地址。核心是一个纯离线静态方法 `derive_proxy_address`（基于 `RelayClient.get_expected_safe()` 的 CREATE2 派生）与一个纯函数 `_resolve_funder`，构造函数调用后者；现有 4 处 `PolymarketAPI(private_key)` 调用点无需改动即自动正确。周边补齐 DB schema/迁移、添加钱包回写、启动回填。

**Tech Stack:** Python 3.12, `py-clob-client-v2`, `py-builder-relayer-client`, SQLite, Flask, pytest, `eth-account`。

参考 spec：`docs/superpowers/specs/2026-05-18-funder-maker-proxy-address-design.md`

---

### Task 1: 数据库 schema 增加 funder 列 + 幂等迁移

**Files:**
- Modify: `models/database.py`（`_create_tables` ~36-41；`init` ~14-17）
- Test: `tests/test_database.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_database.py` 末尾追加：

```python
class TestWalletFunder:
    def test_fresh_db_wallets_has_funder_column(self, db):
        cols = [r[1] for r in db.conn.execute("PRAGMA table_info(wallets)")]
        assert "funder" in cols

    def test_legacy_db_without_funder_gets_migrated(self, tmp_path):
        # Simulate an old DB whose wallets table lacks the funder column
        import sqlite3
        from models.database import Database

        path = str(tmp_path / "legacy.db")
        raw = sqlite3.connect(path)
        raw.execute(
            "CREATE TABLE wallets ("
            "address TEXT PRIMARY KEY, encrypted_key TEXT NOT NULL, "
            "enabled INTEGER NOT NULL DEFAULT 1, "
            "created_at REAL NOT NULL DEFAULT (strftime('%s','now')))"
        )
        raw.execute(
            "INSERT INTO wallets (address, encrypted_key) VALUES ('0xOLD', 'enc')"
        )
        raw.commit()
        raw.close()

        database = Database(path)
        database.init()
        cols = [r[1] for r in database.conn.execute("PRAGMA table_info(wallets)")]
        assert "funder" in cols
        row = database.conn.execute(
            "SELECT funder FROM wallets WHERE address='0xOLD'"
        ).fetchone()
        assert row[0] is None
        database.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_database.py::TestWalletFunder -v`
Expected: FAIL —— `test_fresh_db_wallets_has_funder_column` 断言失败（无 funder 列）；`test_legacy_db_without_funder_gets_migrated` 同样断言失败。

- [ ] **Step 3: 实现 —— schema 补列 + 迁移**

在 `models/database.py` 的 `_create_tables` 中，把 `wallets` 表定义（当前 36-41 行）改为含 `funder`：

```python
            CREATE TABLE IF NOT EXISTS wallets (
                address TEXT PRIMARY KEY,
                encrypted_key TEXT NOT NULL,
                funder TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
```

在 `init` 中，于 `self._create_tables()` 之后追加迁移调用：

```python
    def init(self):
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        self._migrate()
```

并新增方法（放在 `_create_tables` 之后）：

```python
    def _migrate(self):
        """Idempotent migrations for DBs created by older code."""
        c = self.conn.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(wallets)")]
        if "funder" not in cols:
            c.execute("ALTER TABLE wallets ADD COLUMN funder TEXT")
            self.conn.commit()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_database.py::TestWalletFunder -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add models/database.py tests/test_database.py
git commit -m "feat(db): add wallets.funder column + idempotent migration

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: DB 读写 funder（add_wallet / list_wallets / update_wallet_funder）

**Files:**
- Modify: `models/database.py`（`add_wallet` ~147-153；`list_wallets` ~168-171）
- Test: `tests/test_database.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_database.py` 的 `TestWalletFunder` 类中追加：

```python
    def test_add_wallet_stores_funder(self, db):
        db.add_wallet("0xEOA", "enc", "0xSAFE")
        w = db.list_wallets()[0]
        assert w["funder"] == "0xSAFE"

    def test_add_wallet_funder_optional(self, db):
        db.add_wallet("0xEOA", "enc")
        w = db.list_wallets()[0]
        assert w["funder"] is None

    def test_update_wallet_funder(self, db):
        db.add_wallet("0xEOA", "enc", "0xOLD")
        db.update_wallet_funder("0xEOA", "0xNEW")
        w = db.list_wallets()[0]
        assert w["funder"] == "0xNEW"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_database.py::TestWalletFunder -v`
Expected: FAIL —— `add_wallet` 不接受第三参数 / `list_wallets` 无 `funder` 键 / `update_wallet_funder` 不存在。

- [ ] **Step 3: 实现**

`models/database.py` —— 替换 `add_wallet`（当前 147-153）：

```python
    def add_wallet(self, address: str, encrypted_key: str, funder: str = None):
        c = self.conn.cursor()
        c.execute(
            "INSERT INTO wallets (address, encrypted_key, funder) VALUES (?, ?, ?)",
            (address, encrypted_key, funder),
        )
        self.conn.commit()
```

替换 `list_wallets`（当前 168-171）：

```python
    def list_wallets(self) -> list[dict]:
        c = self.conn.cursor()
        c.execute(
            "SELECT address, encrypted_key, funder, enabled, created_at FROM wallets"
        )
        return [dict(row) for row in c.fetchall()]
```

在 `list_wallets` 之后新增：

```python
    def update_wallet_funder(self, address: str, funder: str):
        c = self.conn.cursor()
        c.execute(
            "UPDATE wallets SET funder = ? WHERE address = ?",
            (funder, address),
        )
        self.conn.commit()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_database.py -v`
Expected: PASS（含原有钱包测试，全部通过）

- [ ] **Step 5: 提交**

```bash
git add models/database.py tests/test_database.py
git commit -m "feat(db): persist and update wallets.funder

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `PolymarketAPI.derive_proxy_address` 静态方法 + RELAYER_URL 常量

**Files:**
- Modify: `api/polymarket_api.py`（imports 1-23；类内）
- Test: `tests/test_proxy_derivation.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_proxy_derivation.py`：

```python
"""tests/test_proxy_derivation.py — 纯离线代理地址派生测试，无网络。"""

import re
from eth_account import Account
from api.polymarket_api import PolymarketAPI

# 标准 secp256k1 测试向量私钥（确定性，公开已知）
TEST_PK = "0x0000000000000000000000000000000000000000000000000000000000000001"
TEST_EOA = Account.from_key(TEST_PK).address  # 0x7E5F...395Bdf

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def test_derive_proxy_address_is_valid_address():
    addr = PolymarketAPI.derive_proxy_address(TEST_PK)
    assert _ADDR_RE.match(addr), f"not an address: {addr}"


def test_derive_proxy_address_is_deterministic():
    assert PolymarketAPI.derive_proxy_address(
        TEST_PK
    ) == PolymarketAPI.derive_proxy_address(TEST_PK)


def test_derive_proxy_address_differs_from_eoa():
    addr = PolymarketAPI.derive_proxy_address(TEST_PK)
    assert addr.lower() != TEST_EOA.lower()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_proxy_derivation.py -v`
Expected: FAIL —— `AttributeError: type object 'PolymarketAPI' has no attribute 'derive_proxy_address'`。

- [ ] **Step 3: 实现**

`api/polymarket_api.py` —— 在 imports 区（现有 `import requests` 之后）加入：

```python
import os
from py_builder_relayer_client.client import RelayClient
```

在 `CHAIN_ID = 137` 之后新增常量：

```python
RELAYER_URL = os.environ.get(
    "RELAYER_URL", "https://relayer-v2.polymarket.com/"
)
```

在 `PolymarketAPI` 类中（`get_address` 方法之后）新增静态方法：

```python
    @staticmethod
    def derive_proxy_address(private_key: str) -> str:
        """Deterministically derive the Polymarket Gnosis Safe proxy
        (maker) address for an EOA private key. Pure local CREATE2
        computation — no network, no builder creds."""
        relayer = RelayClient(RELAYER_URL, CHAIN_ID, private_key, None)
        return relayer.get_expected_safe()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_proxy_derivation.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add api/polymarket_api.py tests/test_proxy_derivation.py
git commit -m "feat(api): add PolymarketAPI.derive_proxy_address (offline Safe derivation)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `_resolve_funder` 纯函数 + 构造函数接线

**Files:**
- Modify: `api/polymarket_api.py`（`__init__` 现 29-53）
- Test: `tests/test_proxy_derivation.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_proxy_derivation.py` 末尾追加：

```python
def test_resolve_funder_explicit_wins():
    assert (
        PolymarketAPI._resolve_funder(2, "0xEXPLICIT", TEST_EOA, TEST_PK)
        == "0xEXPLICIT"
    )


def test_resolve_funder_eoa_for_sig0():
    assert (
        PolymarketAPI._resolve_funder(0, None, TEST_EOA, TEST_PK) == TEST_EOA
    )


def test_resolve_funder_derives_safe_for_sig2():
    result = PolymarketAPI._resolve_funder(2, None, TEST_EOA, TEST_PK)
    assert result == PolymarketAPI.derive_proxy_address(TEST_PK)
    assert result.lower() != TEST_EOA.lower()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_proxy_derivation.py -v`
Expected: FAIL —— `AttributeError: ... has no attribute '_resolve_funder'`。

- [ ] **Step 3: 实现**

`api/polymarket_api.py` —— 在 `derive_proxy_address` 之后新增纯静态方法：

```python
    @staticmethod
    def _resolve_funder(
        signature_type: int,
        funder: str,
        eoa_address: str,
        private_key: str,
    ) -> str:
        """Decide the maker/funder address.

        - explicit funder wins
        - sig 0 (EOA): maker == signer == EOA
        - sig 1/2/3: derive the Gnosis Safe proxy address
        """
        if funder:
            return funder
        if signature_type == 0:
            return eoa_address
        try:
            return PolymarketAPI.derive_proxy_address(private_key)
        except Exception as e:
            raise RuntimeError(f"无法派生代理钱包地址: {e}") from e
```

替换 `__init__`（现 29-53）—— 仅改 funder 解析与新增 `self.funder`，其余不变：

```python
    def __init__(self, private_key: str, signature_type: int = 2, funder: str = None):
        """Initialize with private key.

        Args:
            private_key: Hex private key string.
            signature_type: 0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE (default,
                for browser wallets).
            funder: Proxy/maker address. If None and signature_type != 0,
                the Gnosis Safe proxy address is derived from the key.
        """
        self.private_key = private_key
        # Step 1: Create temp client to derive API creds
        temp_client = ClobClient(
            host=POLYMARKET_HOST,
            key=private_key,
            chain_id=CHAIN_ID,
        )
        api_creds = temp_client.derive_api_key()
        eoa_address = temp_client.get_address()
        self.funder = self._resolve_funder(
            signature_type, funder, eoa_address, private_key
        )
        # Step 2: Create full client with L2 auth
        self.client = ClobClient(
            host=POLYMARKET_HOST,
            key=private_key,
            chain_id=CHAIN_ID,
            creds=api_creds,
            signature_type=signature_type,
            funder=self.funder,
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_proxy_derivation.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add api/polymarket_api.py tests/test_proxy_derivation.py
git commit -m "fix(api): maker uses derived Safe proxy, not EOA fallback

Replaces 'funder or temp_client.get_address()' which silently set the
order maker to the EOA instead of the funded Gnosis Safe proxy.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 添加钱包时持久化并返回 funder

**Files:**
- Modify: `web/routes.py`（`api_add_wallet` ~237-251）

> 说明：该路径在 `PolymarketAPI` 构造中调用 `derive_api_key()`（联网），不纳入 pytest；通过代码核对 + Task 7 手动验证。

- [ ] **Step 1: 实现**

`web/routes.py` —— `api_add_wallet` 中，把现有片段（约 239-251）：

```python
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
```

替换为：

```python
    try:
        api = PolymarketAPI(private_key)
        address = api.get_address()
        funder = api.funder
    except Exception as e:
        return jsonify({"error": f"私钥无效或无法派生代理钱包地址: {e}"}), 400

    encrypted = encrypt(private_key, encryption_key)
    try:
        db.add_wallet(address, encrypted, funder)
    except Exception:
        return jsonify({"error": "该钱包已存在"}), 400

    return jsonify({"ok": True, "address": address, "funder": funder})
```

- [ ] **Step 2: 静态自检**

Run: `python -c "import ast; ast.parse(open('web/routes.py', encoding='utf-8').read()); print('OK')"`
Expected: `OK`

确认 `web/templates/config.html:120` 仍为 `alert(... + data.funder)` 无需改动（仅核对，不修改）。

- [ ] **Step 3: 提交**

```bash
git add web/routes.py
git commit -m "feat(web): persist and return wallet funder on add

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 启动时回填已存钱包的 funder

**Files:**
- Modify: `engine/manager.py`（`startup_recovery` 400-442）

> 说明：`startup_recovery` 联网，不纳入 pytest；代码核对 + Task 7 手动验证。

- [ ] **Step 1: 实现**

`engine/manager.py` —— `startup_recovery` 中，在 `api = PolymarketAPI(private_key)`（406 行）之后、`# Cancel all remaining buy orders` 之前插入：

```python
                # Backfill stale/empty funder (older code stored deposit
                # wallet or nothing). Runtime correctness does not depend
                # on this — the API constructor re-derives — but keep the
                # DB/UI honest.
                if w.get("funder") != api.funder:
                    self.db.update_wallet_funder(w["address"], api.funder)
                    logger.info(
                        "Recovery: backfilled funder for %s -> %s",
                        w["address"],
                        api.funder,
                    )
```

- [ ] **Step 2: 静态自检**

Run: `python -c "import ast; ast.parse(open('engine/manager.py', encoding='utf-8').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: 提交**

```bash
git add engine/manager.py
git commit -m "feat(engine): backfill wallet funder on startup recovery

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: 全量回归 + 手动联网验证 + 收尾

**Files:** 无（验证 + 文档）

- [ ] **Step 1: 全量单测**

Run: `pytest`
Expected: 全部 PASS（含 `tests/test_proxy_derivation.py` 6 项、`tests/test_database.py` 全部、其余原有用例不回归）。

- [ ] **Step 2: 手动联网验证（需用户在已登录环境执行）**

用户在已配置钱包的环境运行 `python test_simulate.py`（或在 REPL 中
`from api.polymarket_api import PolymarketAPI; a=PolymarketAPI(pk); print(a.funder, a.get_address(), a.get_balance())`），核对：
- `a.funder` 与 polymarket.com/settings 显示的代理/存款钱包地址一致；
- `a.funder` ≠ `a.get_address()`（EOA）；
- `a.get_balance()` 返回预期非零余额（证明 maker 指向有资金账户）。

启动 app 并登录一次，确认日志出现 `Recovery: backfilled funder ...`，且
`market_maker.db` 中 `0x42Fe...A3f8` 的 `funder` 被更新为派生 Safe 地址。

- [ ] **Step 3: 验收对照**

逐条核对 spec「验收标准」1-7，全部满足。

- [ ] **Step 4: 收尾提交（如有 spec 更新尚未提交）**

```bash
git add docs/superpowers/specs/2026-05-18-funder-maker-proxy-address-design.md docs/superpowers/plans/2026-05-18-funder-maker-proxy-address.md
git commit -m "docs: sync spec (funder column migration) + add implementation plan

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## 自审记录

- **Spec 覆盖**：构造函数派生(Task 3/4)、DB schema+迁移(Task 1)、读写 funder(Task 2)、添加钱包接线(Task 5)、启动回填(Task 6)、测试(Task 3/4/7)、范围外项不实现——全部 spec 章节有对应任务。spec 新增的「schema 不含 funder 列、需迁移」已由 Task 1 覆盖。
- **占位符扫描**：无 TBD/TODO；每个代码步骤含完整代码与确切命令/预期。
- **类型/命名一致**：`derive_proxy_address`、`_resolve_funder(signature_type, funder, eoa_address, private_key)`、`self.funder`、`db.add_wallet(address, encrypted_key, funder=None)`、`db.update_wallet_funder(address, funder)`、`list_wallets()` 含 `funder` 键——跨任务一致。
- **TDD 边界**：纯逻辑（DB、派生、_resolve_funder）走 TDD；联网路径（路由、startup_recovery）走代码核对 + 手动验证，符合项目 `tests/` 无网络约定。
