# SP1 模板与配置解耦 + 采集器拆分 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把全局单套 `settings` 配置拆成「引擎级（全局）+ 策略级（每钱包模板）」两个作用域，并把扫描拆成「共享候选采集器（按品类黑名单用 `tag_slug` 在采集阶段排除）+ 每钱包按自己模板精筛」，老 `determine_order_price` 算法暂时保留不动。

**Architecture:** 新增 `templates` / `template_settings` 表（与现有 `settings` 同形状：逐键 + JSON 值），`wallets` 加 `template_id` 列（NULL = 默认模板）。`config.py` 的 `DEFAULTS` 拆成 `ENGINE_DEFAULTS` / `TEMPLATE_DEFAULTS`。新增 `get_template_for(address)` 给策略级读取点用；`get_settings()` 最后才收窄为引擎级。`MarketScanner` 拆成 `fetch_candidates`（网络、共享、含品类相减 + 精确奖励 + 订单簿缓存，不算价）与 `filter_for_template`（CPU、每钱包、按模板门槛 + 品类 narrow + 老算法定价）；`scan()` 保留为读默认模板的兼容 shim。

**Tech Stack:** Python 3 / Flask / SQLite（`models/database.py`，单连接 `check_same_thread=False`）/ pytest（`tests/`，临时库、纯逻辑不触网，MagicMock 桩 API）/ Polymarket CLOB 公共接口（`requests`）。

**关键执行顺序原则（务必遵守）：** `get_settings()` 收窄为引擎级 + 策略键数据迁移放在**最后一个任务**。在那之前 `get_settings()` 仍返回完整 `DEFAULTS`（引擎+策略合并），这样「尚未切到模板的旧读取点」与「已切到模板的新读取点」在全新库上取值一致（模板默认值 == DEFAULTS 策略值），保证每次提交后测试树都是绿的。

---

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `config.py` | 默认参数；拆 `ENGINE_DEFAULTS` / `TEMPLATE_DEFAULTS` | 修改 |
| `models/database.py` | 建表、迁移、模板 CRUD、`get_settings`/`get_template_for`、候选池读写 | 修改 |
| `engine/categories.py` | 品类排除的纯函数（交集/并集/相减/打标签） | **新建** |
| `api/polymarket_api.py` | `get_rewards_markets` 增加 `tag_slug` 参数 | 修改 |
| `engine/scanner.py` | 拆成 `fetch_candidates` + `filter_for_template`，`scan` 改 shim | 修改 |
| `engine/manager.py` | 采集一次、每钱包按模板精筛；`place_orders` 读模板 | 修改 |
| `engine/monitor.py` | 策略键改读模板，引擎键仍读 `get_settings` | 修改 |
| `web/routes.py` | 止损端点按钱包取模板；`/api/settings` 收窄为引擎级 | 修改 |
| `tests/test_database.py` | 模板 CRUD + 迁移 + `get_settings` 收窄断言 | 修改 |
| `tests/test_categories.py` | 品类纯函数单测 | **新建** |
| `tests/test_rewards_tag_param.py` | `tag_slug` 参数构造单测 | **新建** |
| `tests/test_scanner.py` | `_make_scanner` 桩补模板 + `filter_for_template` 单测 | 修改 |
| `tests/test_manager.py` | 扫描流改测 `fetch_candidates`/`filter_for_template` | 修改 |

---

## Task 1: 拆分 config.py 的 DEFAULTS

**Files:**
- Modify: `config.py:6-20`
- Test: `tests/test_database.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_database.py` import 区下方新增：

```python
def test_config_split_engine_and_template_defaults():
    from config import ENGINE_DEFAULTS, TEMPLATE_DEFAULTS, DEFAULTS
    assert set(ENGINE_DEFAULTS) == {
        "scan_interval_sec",
        "fill_check_interval_sec",
        "cooldown_minutes",
        "rewards_cache_ttl_sec",
    }
    assert TEMPLATE_DEFAULTS["excluded_categories"] == ["sports", "esports", "weather"]
    assert TEMPLATE_DEFAULTS["min_reward_usd"] == 100.0
    assert TEMPLATE_DEFAULTS["stop_loss_pct"] == 15.0
    # 向后兼容:DEFAULTS 仍是两者合并(get_settings 在最后一个任务前仍用它)
    assert DEFAULTS["scan_interval_sec"] == 30
    assert DEFAULTS["min_reward_usd"] == 100.0
    assert set(ENGINE_DEFAULTS) & set(TEMPLATE_DEFAULTS) == set()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_database.py::test_config_split_engine_and_template_defaults -v`
Expected: FAIL，`ImportError: cannot import name 'ENGINE_DEFAULTS'`

- [ ] **Step 3: 修改 config.py**

把 `config.py:6-20` 的 `DEFAULTS = {...}` 整块替换为：

```python
# 引擎级参数:全局单值,所有钱包共用,存 settings 表。
ENGINE_DEFAULTS = {
    "scan_interval_sec": 30,
    "fill_check_interval_sec": 5,
    "cooldown_minutes": 20,
    "rewards_cache_ttl_sec": 600,
}

# 策略级参数:每钱包/每模板取值,存 template_settings 表。
TEMPLATE_DEFAULTS = {
    "min_reward_usd": 100.0,
    "max_spread_cents": 3.0,
    "min_price_cents": 10.0,
    "max_price_cents": 50.0,
    "min_settlement_days": 4,
    "stop_loss_pct": 15.0,
    "max_buy_orders_per_wallet": 5,
    "order_size_mode": "min",
    "order_size_custom_usd": 0.0,
    "excluded_categories": ["sports", "esports", "weather"],
}

# 向后兼容:仍暴露合并后的 DEFAULTS。get_settings() 在最后一个任务前仍以此为基准。
DEFAULTS = {**ENGINE_DEFAULTS, **TEMPLATE_DEFAULTS}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_database.py::test_config_split_engine_and_template_defaults -v`
Expected: PASS

- [ ] **Step 5: 跑全库 DB 测试确认无回归**（`get_settings` 此刻仍返回完整 DEFAULTS）

Run: `pytest tests/test_database.py -v`
Expected: PASS（含旧 `TestSettings`，因 `DEFAULTS` 仍合并两套）

- [ ] **Step 6: 提交**

```bash
git add config.py tests/test_database.py
git commit -m "refactor(config): 拆分 DEFAULTS 为引擎级/策略级两套默认参数"
```

---

## Task 2: 建模板表 + wallets.template_id 列（schema 迁移）

**Files:**
- Modify: `models/database.py`（`_create_tables` executescript 末尾追加两表；`_migrate` 追加列）
- Test: `tests/test_database.py`

- [ ] **Step 1: 写失败测试**

```python
class TestTemplateSchema:
    def test_templates_table_exists(self, db):
        c = db.conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='templates'")
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_database.py::TestTemplateSchema -v`
Expected: FAIL（templates 表不存在）

- [ ] **Step 3: 建表 + 加列**

在 `models/database.py` 的 `_create_tables` executescript 字符串里，`blacklist` 表 `);` 之后、闭合 `"""` 之前，追加：

```sql
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
```

在 `_migrate`（`models/database.py:136`）末尾追加 `wallets.template_id` 列迁移：

```python
        c.execute("PRAGMA table_info(wallets)")
        wcols = {row[1] for row in c.fetchall()}
        if "template_id" not in wcols:
            c.execute("ALTER TABLE wallets ADD COLUMN template_id INTEGER")
            self.conn.commit()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_database.py::TestTemplateSchema -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add models/database.py tests/test_database.py
git commit -m "feat(db): 新增 templates/template_settings 表与 wallets.template_id 列"
```

---

## Task 3: 模板 CRUD + get_template_for（默认模板在 init 时自动建）

**Files:**
- Modify: `models/database.py:6`（import 加 `ENGINE_DEFAULTS, TEMPLATE_DEFAULTS`）
- Modify: `models/database.py`（`save_settings` 后新增 `# --- Templates ---` 区块）
- Modify: `models/database.py:222-228`（`list_wallets` SELECT 加 `template_id`）
- Modify: `models/database.py`（`_migrate` 末尾确保默认模板存在）
- Test: `tests/test_database.py`

- [ ] **Step 1: 写失败测试**

```python
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
        db.save_template(tid, {"max_spread_cents": 6.0, "stop_loss_pct": 8.0})
        t = db.get_template(tid)
        assert t["max_spread_cents"] == 6.0
        assert t["stop_loss_pct"] == 8.0
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_database.py::TestTemplateCRUD -v`
Expected: FAIL（`create_template` 不存在）

- [ ] **Step 3: 实现 CRUD + 默认模板**

`models/database.py:6` 改为：

```python
from config import DEFAULTS, ENGINE_DEFAULTS, TEMPLATE_DEFAULTS
```

在 `save_settings`（约 173 行）之后新增：

```python
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
        c.execute("SELECT id FROM templates WHERE name = ?", (self.DEFAULT_TEMPLATE_NAME,))
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
        c.execute("UPDATE wallets SET template_id = NULL WHERE template_id = ?", (template_id,))
        c.execute("DELETE FROM template_settings WHERE template_id = ?", (template_id,))
        c.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        self.conn.commit()
```

`list_wallets`（222-228 行）SELECT 加 `template_id`：

```python
    def list_wallets(self) -> list[dict]:
        c = self.conn.cursor()
        c.execute(
            "SELECT address, encrypted_key, funder, signature_type, enabled, "
            "created_at, template_id FROM wallets"
        )
        return [dict(row) for row in c.fetchall()]
```

在 `_migrate` 末尾追加（确保任何库 init 后都有默认模板）：

```python
        c.execute("SELECT COUNT(*) AS n FROM templates")
        if c.fetchone()["n"] == 0:
            c.execute(
                "INSERT INTO templates (name) VALUES (?)", (self.DEFAULT_TEMPLATE_NAME,)
            )
            self.conn.commit()
```

> 数据迁移（搬策略键）留到最后一个任务，此处仅保证默认模板存在。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_database.py::TestTemplateCRUD -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add models/database.py tests/test_database.py
git commit -m "feat(db): 模板 CRUD + get_template_for + init 自动建默认模板"
```

---

## Task 4: get_rewards_markets 增加 tag_slug 参数

**Files:**
- Modify: `api/polymarket_api.py:409-455`（`get_rewards_markets`）
- Test: `tests/test_rewards_tag_param.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_rewards_tag_param.py`：

```python
"""tests/test_rewards_tag_param.py — get_rewards_markets 的 tag_slug 参数构造(不触网)。"""

from unittest.mock import patch, MagicMock
from api.polymarket_api import PolymarketAPI


def _fake_resp(data):
    m = MagicMock()
    m.json.return_value = {"data": data, "next_cursor": "LTE="}
    m.raise_for_status.return_value = None
    return m


def test_tag_slug_single_passed_as_param():
    with patch("api.polymarket_api.requests.get", return_value=_fake_resp([])) as g:
        PolymarketAPI.get_rewards_markets(tag_slug="sports")
        _, kwargs = g.call_args
        assert kwargs["params"]["tag_slug"] == "sports"


def test_tag_slug_none_absent_from_params():
    with patch("api.polymarket_api.requests.get", return_value=_fake_resp([])) as g:
        PolymarketAPI.get_rewards_markets()
        _, kwargs = g.call_args
        assert "tag_slug" not in kwargs["params"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_rewards_tag_param.py -v`
Expected: FAIL（`TypeError: ... unexpected keyword argument 'tag_slug'`）

- [ ] **Step 3: 加参数**

`api/polymarket_api.py:410-416` 签名追加 `tag_slug`：

```python
    @staticmethod
    def get_rewards_markets(
        min_price: float = None,
        max_price: float = None,
        max_spread: float = None,
        order_by: str = "rate_per_day",
        position: str = "DESC",
        max_pages: int = 5,
        tag_slug: str = None,
    ) -> list[dict]:
```

在 `params = {"page_size": 100}` 之后、`if min_price is not None:` 之前插入：

```python
            if tag_slug is not None:
                params["tag_slug"] = tag_slug
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_rewards_tag_param.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add api/polymarket_api.py tests/test_rewards_tag_param.py
git commit -m "feat(api): get_rewards_markets 支持 tag_slug 服务端品类过滤"
```

---

## Task 5: 品类纯函数（engine/categories.py）

**Files:**
- Create: `engine/categories.py`
- Create: `tests/test_categories.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_categories.py`：

```python
"""tests/test_categories.py — 品类排除纯函数(不触网)。"""

from engine.categories import (
    excluded_intersection,
    queried_categories,
    partition_candidates,
)


def test_queried_categories_is_union():
    templates = [
        {"excluded_categories": ["sports", "esports"]},
        {"excluded_categories": ["weather"]},
    ]
    assert queried_categories(templates) == {"sports", "esports", "weather"}


def test_excluded_intersection_is_common():
    templates = [
        {"excluded_categories": ["sports", "esports", "weather"]},
        {"excluded_categories": ["sports", "weather"]},
    ]
    assert excluded_intersection(templates) == {"sports", "weather"}


def test_excluded_intersection_empty_when_no_templates():
    assert excluded_intersection([]) == set()


def test_partition_subtracts_intersection_and_tags():
    full = [{"condition_id": "A"}, {"condition_id": "B"}, {"condition_id": "C"}]
    category_ids = {"sports": {"A"}, "weather": {"B"}, "esports": {"A"}}
    pool = partition_candidates(full, category_ids, {"sports", "weather"})
    assert {m["condition_id"] for m in pool} == {"C"}


def test_partition_tags_attached():
    full = [{"condition_id": "A"}, {"condition_id": "C"}]
    category_ids = {"sports": {"A"}, "esports": {"A"}, "weather": set()}
    pool = partition_candidates(full, category_ids, set())
    by_id = {m["condition_id"]: m for m in pool}
    assert set(by_id["A"]["tags"]) == {"sports", "esports"}
    assert by_id["C"]["tags"] == []


def test_partition_empty_intersection_keeps_all():
    full = [{"condition_id": "A"}, {"condition_id": "B"}]
    pool = partition_candidates(full, {"sports": {"A"}}, set())
    assert len(pool) == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_categories.py -v`
Expected: FAIL（`engine.categories` 不存在）

- [ ] **Step 3: 实现纯函数**

新建 `engine/categories.py`：

```python
"""engine/categories.py — 品类排除纯函数(不触网)。

采集器按品类黑名单在采集阶段排除市场:
- queried_categories: 所有模板排除集的并集 = 需向服务端查询的品类(打标签 + 相减)。
- excluded_intersection: 所有模板共同排除的品类 = 可在采集阶段安全删除的品类
  (某模板独有的排除项留到每钱包精筛 narrow,避免误删别的模板需要的市场)。
- partition_candidates: 全量市场减去交集品类命中者,并给每条候选打 tags。
"""


def queried_categories(templates: list[dict]) -> set:
    out = set()
    for t in templates:
        out.update(t.get("excluded_categories", []) or [])
    return out


def excluded_intersection(templates: list[dict]) -> set:
    sets = [set(t.get("excluded_categories", []) or []) for t in templates]
    if not sets:
        return set()
    out = sets[0]
    for s in sets[1:]:
        out = out & s
    return out


def partition_candidates(
    full_markets: list[dict],
    category_ids: dict,
    intersection_slugs: set,
) -> list[dict]:
    """全量市场 - 交集品类命中者,并给每条候选打 tags。

    Args:
        full_markets: 全量奖励市场(每条含 condition_id)。
        category_ids: {品类 slug: set(condition_id)},采集器逐品类查询所得。
        intersection_slugs: 采集阶段要删的品类(= 所有模板共同排除集)。

    Returns:
        候选池:移除属于任一交集品类的市场,并为每条加 tags =
        它命中的(被查询过的)品类 slug 列表。
    """
    removed = set()
    for slug in intersection_slugs:
        removed |= category_ids.get(slug, set())

    pool = []
    for m in full_markets:
        cid = m.get("condition_id", "")
        if cid in removed:
            continue
        tags = [slug for slug, ids in category_ids.items() if cid in ids]
        entry = dict(m)
        entry["tags"] = tags
        pool.append(entry)
    return pool
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_categories.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add engine/categories.py tests/test_categories.py
git commit -m "feat(engine): 品类排除纯函数(交集/并集/相减/打标签)"
```

---

## Task 6: 候选池表加 tags 列 + 读写

**Files:**
- Modify: `models/database.py:98-118`（`eligible_markets` 建表加 `tags`）
- Modify: `models/database.py:136-153`（`_migrate` 加 `tags` 列）
- Modify: `models/database.py:452-493`（`save_eligible_markets`/`get_eligible_markets`）
- Test: `tests/test_database.py`

- [ ] **Step 1: 写失败测试**

```python
class TestCandidatePoolTags:
    def test_save_and_get_tags(self, db):
        db.save_eligible_markets([{
            "market_id": "A", "token_id": "t1", "market_name": "M",
            "outcome": "Yes", "daily_reward": 50, "order_price": 0,
            "order_size": 0, "tags": ["sports"],
        }])
        assert db.get_eligible_markets()[0]["tags"] == ["sports"]

    def test_tags_default_empty_list(self, db):
        db.save_eligible_markets([{
            "market_id": "B", "token_id": "t2", "market_name": "M2",
            "outcome": "No", "daily_reward": 50, "order_price": 0, "order_size": 0,
        }])
        assert db.get_eligible_markets()[0]["tags"] == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_database.py::TestCandidatePoolTags -v`
Expected: FAIL

- [ ] **Step 3: 加列 + JSON 读写**

`eligible_markets` 建表里 `end_date` 行之后、`scanned_at` 之前加：

```sql
                tags TEXT DEFAULT '[]',
```

`_migrate` 的 `eligible_markets` 列迁移块后追加：

```python
        c.execute("PRAGMA table_info(eligible_markets)")
        em_cols2 = {row[1] for row in c.fetchall()}
        if em_cols2 and "tags" not in em_cols2:
            c.execute("ALTER TABLE eligible_markets ADD COLUMN tags TEXT DEFAULT '[]'")
            self.conn.commit()
```

`save_eligible_markets` 的 INSERT：列清单 `scanned_at` 前加 `tags`，VALUES 多一个 `?`，值元组 `now` 之前插入：

```python
                    json.dumps(m.get("tags", []) or []),
```

`get_eligible_markets` 改为解 JSON：

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_database.py::TestCandidatePoolTags -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add models/database.py tests/test_database.py
git commit -m "feat(db): 候选池 eligible_markets 增加 tags 列(JSON)"
```

---

## Task 7: 拆分 scanner —— fetch_candidates + filter_for_template + scan shim

**Files:**
- Modify: `engine/scanner.py`（新增两方法 + `scan` 改 shim；顶部 import categories）
- Modify: `tests/test_scanner.py:10-36`（`_make_scanner` 桩补模板）
- Test: `tests/test_scanner.py`

**说明：** `fetch_candidates(templates, on_progress, on_found, skip_orderbook)` 做钱包无关的网络工作：按 `excluded_intersection` 在采集阶段排品类、按 `queried_categories` 查询打标签、用最宽松奖励下限预筛、补**精确每市场奖励**（`get_rewards_for_market`，保持与旧 `scan` 行为一致）、抓 spread+订单簿缓存。**不算价**。`filter_for_template(pool, template, wallet_address)` 做每钱包 CPU 工作：模板门槛过滤 + 品类 narrow + 老 `determine_order_price` 定价。`scan()` 保留为兼容 shim：用默认模板组合上述两者，使现有 `scan()` 测试继续有效。

- [ ] **Step 1: 改 `_make_scanner` 桩补模板（保持现有 scan 测试可用）**

把 `tests/test_scanner.py` 的 `_make_scanner`（10-36 行）中 `db.get_settings.return_value = default_settings` 一行下方追加（让 shim 能读默认模板）：

```python
    db.get_settings.return_value = default_settings
    db.get_default_template_id.return_value = 1
    db.get_template.return_value = default_settings
    db.get_template_for.return_value = default_settings
```

> `default_settings` 已含 `min_reward_usd/max_spread_cents/min_price_cents/max_price_cents/min_settlement_days`。shim 经 `get_template(get_default_template_id())` 取到它，filter 用它，定价逻辑不变 → 现有断言（min_cost 等）仍成立。

- [ ] **Step 2: 为新方法写失败测试**

在 `tests/test_scanner.py` 追加：

```python
class TestFetchCandidatesCategoryWiring:
    def test_queries_full_plus_each_category_and_subtracts(self):
        def fake_rewards(tag_slug=None, **kw):
            if tag_slug is None:
                return [
                    {"condition_id": "A", "tokens": [], "rewards_config": []},
                    {"condition_id": "B", "tokens": [], "rewards_config": []},
                    {"condition_id": "C", "tokens": [], "rewards_config": []},
                ]
            return {"sports": [{"condition_id": "A"}], "weather": [{"condition_id": "B"}],
                    "esports": []}.get(tag_slug, [])
        api = MagicMock(); api.get_rewards_markets.side_effect = fake_rewards
        db = MagicMock(); db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        templates = [
            {"excluded_categories": ["sports", "weather"], "min_reward_usd": 0},
            {"excluded_categories": ["sports", "weather", "esports"], "min_reward_usd": 0},
        ]
        pool = scanner.fetch_candidates(templates, skip_orderbook=True)
        assert {m["condition_id"] for m in pool} == {"C"}

    def test_no_price_computed(self):
        api = MagicMock()
        api.get_rewards_markets.return_value = [
            {"condition_id": "C", "tokens": [], "rewards_config": []}
        ]
        db = MagicMock(); db.get_blacklist_ids.return_value = set()
        scanner = MarketScanner(api, db, "")
        pool = scanner.fetch_candidates([{"excluded_categories": [], "min_reward_usd": 0}],
                                        skip_orderbook=True)
        assert all("order_price" not in m for m in pool)


class TestFilterForTemplate:
    def _candidate(self, cid, tags, daily_reward=50, bid=0.30, ask=0.32):
        return {
            "condition_id": cid, "question": "M", "market_slug": "", "event_slug": "",
            "market_competitiveness": 0, "end_date": "", "neg_risk": False,
            "rewards_max_spread": 3, "rewards_min_size": 100, "tags": tags,
            "market_reward": daily_reward,
            "tokens": [{"token_id": cid + "-y", "outcome": "Yes", "price": bid}],
            "rewards_config": [{"rate_per_day": daily_reward}],
            "_orderbooks": {cid + "-y": {
                "bids": [{"price": str(bid), "size": "5000"}],
                "asks": [{"price": str(ask), "size": "5000"}],
                "tick_size": "0.01", "spread": ask - bid}},
        }

    def _template(self, **over):
        t = {"min_reward_usd": 6, "min_price_cents": 10, "max_price_cents": 90,
             "max_spread_cents": 6, "min_settlement_days": 0, "excluded_categories": []}
        t.update(over)
        return t

    def _scanner(self):
        db = MagicMock()
        db.is_in_cooldown.return_value = False  # filter_for_template 会查冷却
        return MarketScanner(MagicMock(), db, "")

    def test_category_narrow_drops_excluded_tag(self):
        scanner = self._scanner()
        pool = [self._candidate("A", ["esports"]), self._candidate("B", [])]
        out = scanner.filter_for_template(pool, self._template(excluded_categories=["esports"]), "0xW")
        ids = {e["market_id"] for e in out}
        assert "A" not in ids and "B" in ids

    def test_reward_floor_filters(self):
        scanner = self._scanner()
        pool = [self._candidate("C", [], daily_reward=3)]
        assert scanner.filter_for_template(pool, self._template(), "0xW") == []

    def test_cooldown_market_skipped(self):
        db = MagicMock()
        db.is_in_cooldown.return_value = True
        scanner = MarketScanner(MagicMock(), db, "")
        pool = [self._candidate("B", [])]
        assert scanner.filter_for_template(pool, self._template(), "0xW") == []

    def test_two_templates_yield_different_lists(self):
        scanner = self._scanner()
        pool = [self._candidate("A", ["esports"]), self._candidate("B", [])]
        strict = {e["market_id"] for e in scanner.filter_for_template(
            pool, self._template(excluded_categories=["esports"]), "0xW")}
        loose = {e["market_id"] for e in scanner.filter_for_template(
            pool, self._template(excluded_categories=[]), "0xW")}
        assert strict != loose and "A" in loose and "A" not in strict
```

- [ ] **Step 3: 运行测试确认失败**

Run: `pytest tests/test_scanner.py::TestFetchCandidatesCategoryWiring tests/test_scanner.py::TestFilterForTemplate -v`
Expected: FAIL（方法不存在）

- [ ] **Step 4: 实现两方法 + scan shim**

`engine/scanner.py` 顶部 import 区加：

```python
from engine.categories import (
    excluded_intersection,
    queried_categories,
    partition_candidates,
)
```

在 `MarketScanner` 内新增（放在现有 `scan` 之前）：

```python
    def fetch_candidates(
        self, templates, on_progress=None, on_found=None, skip_orderbook=False
    ) -> list[dict]:
        """共享采集:抓全量奖励市场,按品类交集采集阶段排除,打 tags,补精确奖励,
        缓存订单簿。钱包无关、网络密集、不算价。skip_orderbook 仅供单测。"""
        inter = excluded_intersection(templates)
        queried = queried_categories(templates)
        floors = [t.get("min_reward_usd", 0) for t in templates]
        min_floor = min(floors) if floors else 0

        full = self.api.get_rewards_markets()
        category_ids = {}
        for slug in queried:
            rows = self.api.get_rewards_markets(tag_slug=slug)
            category_ids[slug] = {m.get("condition_id", "") for m in rows}

        pool = partition_candidates(full, category_ids, inter)
        blacklist = self.db.get_blacklist_ids()

        out = []
        checked = 0
        for market in pool:
            cid = market.get("condition_id", "")
            if cid in blacklist:
                continue
            total_rate = sum(
                rc.get("rate_per_day", 0) for rc in market.get("rewards_config", [])
            )
            if total_rate < min_floor:
                continue  # 比最宽松模板还低,任何模板都不会要
            self.db.upsert_market_meta(
                cid, market.get("question", ""),
                market.get("market_slug", ""), market.get("event_slug", ""),
            )
            # 精确每市场奖励(与旧 scan 一致:/rewards/markets/{cid})
            market_reward = total_rate
            try:
                raw = self.api.get_rewards_for_market(cid)
                if raw:
                    market_reward = sum(
                        rc.get("rate_per_day", 0)
                        for rd in raw for rc in rd.get("rewards_config", [])
                    )
            except Exception as e:
                logger.warning("Precise reward fetch failed for %s: %s", cid, e)
            market["market_reward"] = market_reward
            if not skip_orderbook:
                market["_orderbooks"] = self._fetch_orderbooks(market)
            checked += 1
            if on_progress:
                on_progress(checked, len(pool), f"Checking: {market.get('question','')}")
            if on_found:
                on_found(market)
            out.append(market)
        return out

    def _fetch_orderbooks(self, market: dict) -> dict:
        """抓该市场每 token 的订单簿快照(钱包无关)。抓不到的略过。"""
        books = {}
        for token in market.get("tokens", []):
            token_id = token.get("token_id", "")
            if not token_id:
                continue
            try:
                spread_val = self.api.get_spread(token_id)
                ob = self.api.get_orderbook(token_id)
            except Exception as e:
                logger.warning("Orderbook fetch failed for %s: %s", token_id, e)
                continue
            books[token_id] = {
                "bids": ob.get("bids", []), "asks": ob.get("asks", []),
                "tick_size": ob.get("tick_size", "0.01"), "spread": spread_val,
            }
        return books

    def filter_for_template(self, candidate_pool, template, wallet_address) -> list[dict]:
        """从候选池产出某模板的 eligible(门槛过滤 + 品类 narrow + 老算法定价)。"""
        min_reward = template["min_reward_usd"]
        min_price_cents = template["min_price_cents"]
        max_price_cents = template["max_price_cents"]
        max_spread_cents = template["max_spread_cents"]
        min_days = template["min_settlement_days"]
        excluded = set(template.get("excluded_categories", []) or [])

        eligible = []
        for market in candidate_pool:
            if excluded & set(market.get("tags", [])):
                continue
            total_rate = sum(
                rc.get("rate_per_day", 0) for rc in market.get("rewards_config", [])
            )
            market_reward = market.get("market_reward", total_rate)
            if total_rate < min_reward or market_reward < min_reward:
                continue
            end_date_str = market.get("end_date", "")
            end_ts = _parse_end_date(end_date_str)
            days_left = (end_ts - time.time()) / 86400 if end_ts else -1
            if 0 <= days_left < min_days:
                continue

            condition_id = market.get("condition_id", "")
            if self.db.is_in_cooldown(wallet_address, condition_id):
                continue  # 该钱包对此市场仍在冷却(与旧 scan 口径一致)
            max_spread_reward = float(market.get("rewards_max_spread", 2))
            min_size = int(market.get("rewards_min_size", 0))
            neg_risk = market.get("neg_risk", False)
            books = market.get("_orderbooks", {})
            valid_tokens = [
                t for t in market.get("tokens", [])
                if min_price_cents <= float(t.get("price", 0)) * 100 <= max_price_cents
            ]
            for token in valid_tokens:
                token_id = token.get("token_id", "")
                book = books.get(token_id)
                if not book:
                    continue
                spread_val = book.get("spread", -1)
                if spread_val < 0 or spread_val * 100 >= max_spread_cents:
                    continue
                bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
                asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
                if not bids or not asks:
                    continue
                best_bid = float(bids[0]["price"])
                best_ask = float(asks[0]["price"])
                if best_bid * 100 < min_price_cents or best_bid * 100 > max_price_cents:
                    continue
                tick_size_str = book.get("tick_size", "0.01")
                tick_size = float(tick_size_str)
                midpoint = (best_bid + best_ask) / 2
                reward_range_min, reward_range_max = reward_price_range(midpoint, max_spread_reward)
                min_cost = min_size * ceil_to_tick(max(reward_range_min, 0.0), tick_size)
                try:
                    order_price = determine_order_price(
                        bids=bids, max_spread=int(max_spread_reward), tick_size=tick_size,
                        reward_range_min=reward_range_min, reward_range_max=reward_range_max,
                    )
                except Exception as e:
                    logger.warning("Strategy error for %s: %s", condition_id, e)
                    continue
                if order_price is None:
                    continue
                eligible.append({
                    "market_id": condition_id, "token_id": token_id,
                    "market_name": market.get("question", ""),
                    "outcome": token.get("outcome", ""),
                    "market_competitiveness": market.get("market_competitiveness", 0),
                    "end_date": end_date_str, "daily_reward": market_reward,
                    "rewards_max_spread": max_spread_reward, "rewards_min_size": min_size,
                    "tick_size": tick_size, "tick_size_str": tick_size_str,
                    "neg_risk": neg_risk, "reward_range_min": reward_range_min,
                    "reward_range_max": reward_range_max, "order_price": order_price,
                    "order_size": min_size, "min_cost": min_cost,
                    "tags": market.get("tags", []),
                })
        return eligible
```

把现有 `scan` 方法体（48-280 行）整体替换为 shim：

```python
    def scan(self, on_progress=None, on_found=None) -> list[dict]:
        """兼容 shim:用默认模板采集 + 精筛(老入口,供单模板路径与既有测试)。"""
        tmpl = self.db.get_template(self.db.get_default_template_id())
        pool = self.fetch_candidates([tmpl], on_progress=on_progress, on_found=on_found)
        return self.filter_for_template(pool, tmpl, self.wallet_address)
```

> shim 保留 `on_progress`/`on_found` 透传,使现有进度断言不破。注意 fetch_candidates 的 on_found 现在传的是**候选 market**(非旧 eligible entry);依赖 on_found 入参字段的旧测试已在本任务 Step 1 桩调整范围内——若某测试断言 on_found 收到 eligible 字段,改断言 market_id 即可。

- [ ] **Step 5: 运行新测试确认通过**

Run: `pytest tests/test_scanner.py::TestFetchCandidatesCategoryWiring tests/test_scanner.py::TestFilterForTemplate -v`
Expected: PASS

- [ ] **Step 6: 跑全部 scanner 测试确认无回归**

Run: `pytest tests/test_scanner.py -v`
Expected: PASS。若某旧 `scan()` 测试断言 api 调用次数/顺序或 on_found 入参形状，按 Step 4 备注最小调整（数据语义未变，定价逻辑逐字保留）。

- [ ] **Step 7: 提交**

```bash
git add engine/scanner.py tests/test_scanner.py
git commit -m "feat(scanner): 拆 fetch_candidates+filter_for_template,scan 改默认模板 shim"
```

---

## Task 8: manager 接线 —— 采集一次、每钱包精筛

**Files:**
- Modify: `engine/manager.py:554-609`（`_scan_with_status`/`_do_scan`）
- Modify: `engine/manager.py:433-458`（`place_all_orders`）
- Modify: `engine/manager.py`（新增 `_active_templates`）
- Modify: `tests/test_manager.py:187-314`（扫描流测试）
- Test: `tests/test_manager.py`

- [ ] **Step 1a: 给 `_make_manager` 的 db 桩补模板返回值**

`_active_templates` 会迭代 `db.list_wallets()` 并对每个钱包调 `db.get_template_for(addr).get("excluded_categories", [])`，再 `sorted(...)`。MagicMock 的 `.get(...)` 默认返回 MagicMock，`sorted` 会 TypeError。在 `tests/test_manager.py` 的 `_make_manager`（约 28-45 行）里、`db.list_wallets.return_value = [...]` 之后追加真实字典桩：

```python
    db.get_template_for.return_value = {
        "excluded_categories": [], "min_reward_usd": 100.0,
        "max_buy_orders_per_wallet": 5, "order_size_mode": "min",
        "order_size_custom_usd": 0.0,
    }
    db.get_template.return_value = {"excluded_categories": [], "min_reward_usd": 100.0}
    db.get_default_template_id.return_value = 1
```

- [ ] **Step 1b: 重写 manager 扫描流测试**

把 `tests/test_manager.py` 的 `TestScanMarketsLastScanTime` / `TestSharedScanWithStatus` 两类（187-314 行）替换为以下（FakeScanner 提供 `fetch_candidates`/`filter_for_template`）：

```python
class TestScanMarketsLastScanTime:
    def test_last_scan_time_only_updates_at_round_completion(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        observed = []

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass
            def fetch_candidates(self, templates, on_progress=None, on_found=None, **kw):
                on_found({"market_id": "m1"})
                observed.append(manager.last_scan_time)
                on_found({"market_id": "m2"})
                observed.append(manager.last_scan_time)
                return [{"market_id": "m1"}, {"market_id": "m2"}]
            def filter_for_template(self, pool, tmpl, addr):
                return pool

        assert manager.last_scan_time == 0
        with patch("engine.manager.MarketScanner", FakeScanner):
            manager.scan_markets()
        assert observed == [0, 0]
        assert manager.last_scan_time > 0
        assert manager.scan_status == "done"
        assert manager.eligible_markets == [{"market_id": "m1"}, {"market_id": "m2"}]


class TestSharedScanWithStatus:
    def test_manual_scan_sets_scanning_then_done(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        seen = []

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass
            def fetch_candidates(self, templates, on_progress=None, on_found=None, **kw):
                on_progress(1, 2, "checking")
                seen.append(manager.scan_status)
                on_found({"market_id": "m1"})
                return [{"market_id": "m1"}]
            def filter_for_template(self, pool, tmpl, addr):
                return pool

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager.scan_markets()
        assert seen == ["scanning"]
        assert manager.scan_status == "done"
        assert manager.last_scan_time > 0
        assert manager.eligible_markets == [{"market_id": "m1"}]
        db.save_eligible_markets.assert_called_once_with([{"market_id": "m1"}])

    def test_auto_do_scan_filters_per_wallet_and_places(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock(); worker.running = True
        manager.engines = {"0xABC": worker}

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass
            def fetch_candidates(self, templates, on_progress=None, on_found=None, **kw):
                return [{"market_id": "m9", "tags": []}]
            def filter_for_template(self, pool, tmpl, addr):
                return pool  # 该钱包精筛后原样

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager._do_scan()
        assert manager.scan_status == "done"
        assert manager.last_scan_time > 0
        worker.place_orders.assert_called_once_with([{"market_id": "m9", "tags": []}])

    def test_auto_do_scan_distributes_sorted_by_competitiveness(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock(); worker.running = True
        manager.engines = {"0xABC": worker}

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass
            def fetch_candidates(self, templates, on_progress=None, on_found=None, **kw):
                return [
                    {"market_id": "hi", "market_competitiveness": 0.9},
                    {"market_id": "lo", "market_competitiveness": 0.1},
                    {"market_id": "mid", "market_competitiveness": 0.5},
                ]
            def filter_for_template(self, pool, tmpl, addr):
                return list(pool)

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager._do_scan()
        distributed = worker.place_orders.call_args[0][0]
        assert [m["market_id"] for m in distributed] == ["lo", "mid", "hi"]

    def test_scan_failure_resets_status_and_keeps_last_scan_time(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager.last_scan_time = 12345.0
        manager.eligible_markets = [{"market_id": "prev"}]

        class BoomScanner:
            def __init__(self, api, db, addr):
                pass
            def fetch_candidates(self, templates, on_progress=None, on_found=None, **kw):
                raise RuntimeError("scanner blew up")

        with patch("engine.manager.MarketScanner", BoomScanner):
            with pytest.raises(RuntimeError):
                manager._scan_with_status()
        assert manager.scan_status == "done"
        assert manager.last_scan_time == 12345.0
        assert manager.eligible_markets == [{"market_id": "prev"}]
```

> 注意：`_make_manager` 的 db 是 MagicMock；`get_template_for`/`get_template`/`get_default_template_id`/`list_wallets` 默认返回 MagicMock。`_active_templates` 需容忍——下面实现里对空/异常回落默认模板，且测试中 `list_wallets` 返回 MagicMock 不可迭代时回落到 `[get_template(...)]`。为稳妥，测试 `_make_manager` 应已让 `db.list_wallets.return_value = []`（若没有则在本步加上 `db.list_wallets.return_value = []`）。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_manager.py::TestSharedScanWithStatus -v`
Expected: FAIL（manager 仍调 `scanner.scan`）

- [ ] **Step 3: 改 manager**

新增 `_active_templates` 到 `EngineManager`（放在 `_scan_with_status` 前）：

```python
    def _active_templates(self) -> list[dict]:
        """所有启用钱包绑定模板(按 excluded_categories 去重),供采集器算并集/交集。"""
        try:
            wallets = self.db.list_wallets()
        except Exception:
            wallets = []
        seen = {}
        for w in wallets:
            if not w.get("enabled"):
                continue
            tmpl = self.db.get_template_for(w["address"])
            key = tuple(sorted(tmpl.get("excluded_categories", []) or []))
            seen[key] = tmpl
        if seen:
            return list(seen.values())
        return [self.db.get_template(self.db.get_default_template_id())]
```

把 `_scan_with_status`（554-589 行）中 `scanner.scan(...)` 调用段改为：

```python
        try:
            scanner = MarketScanner(self._scanner_api, self.db, "")
            templates = self._active_templates()
            candidate_pool = scanner.fetch_candidates(
                templates, on_progress=on_progress, on_found=on_found
            )
        except Exception:
            self.eligible_markets = prev_eligible
            self.scan_status = "done"
            raise
        self.eligible_markets = candidate_pool
        self.last_scan_time = _time.time()
        self.scan_status = "done"
        self.scan_progress = f"Done: {len(candidate_pool)} candidates"
        logger.info("Scanner found %d candidates", len(candidate_pool))
        return candidate_pool
```

把 `_do_scan`（591-609 行）改为每钱包精筛：

```python
    def _do_scan(self):
        """采集一次候选池,每钱包按自己模板精筛+下单。"""
        candidate_pool = self._scan_with_status()
        for address, worker in self.engines.items():
            if not worker.running:
                continue
            try:
                tmpl = self.db.get_template_for(address)
                scanner = MarketScanner(self._scanner_api, self.db, "")
                eligible = scanner.filter_for_template(candidate_pool, tmpl, address)
                eligible.sort(key=lambda m: float(m.get("market_competitiveness", 0) or 0))
                worker.place_orders(eligible)
            except Exception as e:
                logger.error("Error distributing to wallet %s: %s", address, e)
```

把 `place_all_orders`（433-458 行）改为每钱包精筛：

```python
    def place_all_orders(self):
        """每钱包按自己模板从候选池精筛后下单。"""
        if not self.eligible_markets:
            logger.warning("No candidate pool to place orders on")
            return
        for address, worker in self.engines.items():
            if not worker.running:
                continue
            try:
                tmpl = self.db.get_template_for(address)
                scanner = MarketScanner(self._scanner_api, self.db, "")
                eligible = scanner.filter_for_template(self.eligible_markets, tmpl, address)
                eligible.sort(key=lambda m: float(m.get("market_competitiveness", 0) or 0))
                worker.place_orders(eligible)
            except Exception as e:
                logger.error("Error placing orders for %s: %s", address, e)
```

> `scan_markets`（404-431 行）末尾 `self.db.save_eligible_markets(eligible)` 保留——`_scan_with_status` 现返回候选池，存的就是候选池。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_manager.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add engine/manager.py tests/test_manager.py
git commit -m "feat(manager): 采集一次候选池,每钱包按模板精筛后下单"
```

---

## Task 9: place_orders 读模板（max_buys / 份额模式）

**Files:**
- Modify: `engine/manager.py:125-128`（`place_orders` 内）
- Test: `tests/test_manager.py`（如有 place_orders 测试，确认仍绿）

- [ ] **Step 1: 改读取源**

`engine/manager.py:125-128`：

```python
        settings = self.db.get_settings()
        max_buys = int(settings.get("max_buy_orders_per_wallet", 5))
        order_size_mode = settings.get("order_size_mode", "min")
        order_size_custom_usd = float(settings.get("order_size_custom_usd", 0) or 0)
```
改为：
```python
        tmpl = self.db.get_template_for(self.wallet_address)
        max_buys = int(tmpl.get("max_buy_orders_per_wallet", 5))
        order_size_mode = tmpl.get("order_size_mode", "min")
        order_size_custom_usd = float(tmpl.get("order_size_custom_usd", 0) or 0)
```

> `get_settings()` 此刻仍返回完整 DEFAULTS（含这些键），但新代码改读模板。全新库上模板默认值 == DEFAULTS 值，行为不变。如有 place_orders 单测用 `db.get_settings` 喂这些键，改为喂 `db.get_template_for.return_value`。

- [ ] **Step 2: 跑相关测试**

Run: `pytest tests/test_manager.py -k "place or order or cap" -v`
Expected: PASS（必要时按上注调整桩）

- [ ] **Step 3: 提交**

```bash
git add engine/manager.py tests/test_manager.py
git commit -m "refactor(manager): place_orders 的挂单上限/份额模式改读钱包模板"
```

---

## Task 10: monitor 策略键改读模板

**Files:**
- Modify: `engine/monitor.py:347`（`check_stop_loss`）
- Modify: `engine/monitor.py:586`（Step3 路径）
- Test: `tests/test_monitor.py`

> 引擎键 `cooldown_minutes`（180 行）与 `rewards_cache_ttl_sec`（534 行）保持 `get_settings()`，不动。

- [ ] **Step 1: 写失败测试**

```python
class TestMonitorReadsTemplate:
    def test_stop_loss_pct_from_template(self):
        from engine.monitor import OrderMonitor
        from unittest.mock import MagicMock
        db = MagicMock()
        db.get_template_for.return_value = {
            "stop_loss_pct": 8.0, "min_price_cents": 10.0, "max_price_cents": 90.0,
        }
        db.get_settings.return_value = {"cooldown_minutes": 20, "rewards_cache_ttl_sec": 600}
        api = MagicMock()
        api.get_user_positions.return_value = []
        mon = OrderMonitor(api, db, "0xW")
        mon.check_stop_loss()
        db.get_template_for.assert_called_with("0xW")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_monitor.py::TestMonitorReadsTemplate -v`
Expected: FAIL（`check_stop_loss` 调的是 `get_settings`）

- [ ] **Step 3: 改读取点**

`engine/monitor.py:347`：`settings = self.db.get_settings()` → `settings = self.db.get_template_for(self.wallet_address)`
`engine/monitor.py:586`：`settings = self.db.get_settings()` → `settings = self.db.get_template_for(self.wallet_address)`

> 校验：347 行段后续读 `stop_loss_pct`（策略键）；586 行段后续读 `min_price_cents`/`max_price_cents`（策略键，694-695 行）。均应来自模板。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_monitor.py::TestMonitorReadsTemplate -v`
Expected: PASS

- [ ] **Step 5: 跑全部 monitor 测试确认无回归**

Run: `pytest tests/test_monitor.py tests/test_monitor_status.py -v`
Expected: PASS。若旧测试用 `db.get_settings` 喂 `stop_loss_pct`/`min_price_cents`/`max_price_cents`，改喂 `db.get_template_for.return_value`（含这些键）。

- [ ] **Step 6: 提交**

```bash
git add engine/monitor.py tests/test_monitor.py
git commit -m "refactor(monitor): 策略键改读钱包模板,引擎键仍读全局 settings"
```

---

## Task 11: web 接线 —— 止损端点取模板 + /api/settings 语义

**Files:**
- Modify: `web/routes.py:631-632`
- Test: 冒烟

- [ ] **Step 1: 止损端点改取模板**

`web/routes.py:631-632`：

```python
    wallet = request.args.get("wallet")
    sl = db.get_settings()["stop_loss_pct"] / 100.0
```
改为：
```python
    wallet = request.args.get("wallet")
    tmpl = db.get_template_for(wallet) if wallet else db.get_template(db.get_default_template_id())
    sl = tmpl["stop_loss_pct"] / 100.0
```

- [ ] **Step 2: 确认 /api/eligible 数据源**

定位 `web/routes.py` 中 `/api/eligible` 处理函数；确认其返回 `manager.eligible_markets`（扫描中）或 `db.get_eligible_markets()`（空闲），两者现在都是含 `tags` 的候选池，无需改数据源。无代码改动则记"已确认"。

- [ ] **Step 3: 冒烟 + 全量测试**

Run: `python -c "import app; print('import ok')"`
Expected: `import ok`

Run: `pytest -q`
Expected: 全绿

- [ ] **Step 4: 提交**

```bash
git add web/routes.py
git commit -m "refactor(web): 止损端点按钱包取模板"
```

---

## Task 12: 收窄 get_settings() 为引擎级 + 策略键数据迁移（最后一步）

**Files:**
- Modify: `models/database.py:157-163`（`get_settings`）
- Modify: `models/database.py`（`_migrate` 的默认模板段升级为「建默认模板 + 搬策略键」）
- Modify: `tests/test_database.py`（旧 `TestSettings` 断言更新 + 迁移测试）
- Test: `tests/test_database.py`

> 此刻所有策略键消费者（scanner/manager/monitor/web）已切到 `get_template_for`，故收窄 `get_settings()` 安全，测试树保持绿。

- [ ] **Step 1: 更新旧 TestSettings + 新增迁移测试**

把 `tests/test_database.py` 的 `TestSettings` 类（17-32 行，原断言含 `stop_loss_pct`）替换为：

```python
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
```

新增迁移测试类：

```python
class TestSettingsToTemplateMigration:
    def _make_legacy_state(self, db):
        """模拟老库:templates 空,settings 里有策略键+引擎键。"""
        import json as _json
        c = db.conn.cursor()
        c.execute("DELETE FROM template_settings")
        c.execute("DELETE FROM templates")
        c.execute("DELETE FROM settings")
        legacy = {"stop_loss_pct": 10.0, "min_reward_usd": 200.0,
                  "order_size_mode": "balance", "scan_interval_sec": 45}
        for k, v in legacy.items():
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, _json.dumps(v)))
        db.conn.commit()

    def test_strategy_keys_move_to_default_template(self, db):
        self._make_legacy_state(db)
        db._migrate()
        t = db.get_template(db.get_default_template_id())
        assert t["stop_loss_pct"] == 10.0
        assert t["min_reward_usd"] == 200.0
        assert t["order_size_mode"] == "balance"

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
        assert "stop_loss_pct" not in keys and "min_reward_usd" not in keys

    def test_migration_idempotent(self, db):
        self._make_legacy_state(db)
        db._migrate()
        db._migrate()
        assert len([t for t in db.list_templates() if t["name"] == "默认"]) == 1
        assert db.get_template(db.get_default_template_id())["stop_loss_pct"] == 10.0

    def test_fresh_install_no_copy(self, db):
        t = db.get_template(db.get_default_template_id())
        assert t["stop_loss_pct"] == 15.0  # 默认值,非迁移值
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_database.py::TestSettings tests/test_database.py::TestSettingsToTemplateMigration -v`
Expected: FAIL（`get_settings` 仍返回策略键；迁移未搬键）

- [ ] **Step 3: 收窄 get_settings + 升级迁移**

`get_settings`（157-163 行）改为：

```python
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
```

把 Task 3 在 `_migrate` 末尾加的「确保默认模板存在」块，升级为「建默认模板 + 搬策略键」：

```python
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
```

> 幂等：templates 非空即跳过。全新库 settings 空 → 只建默认模板、不搬。老库 → 搬策略键、删 settings 内策略键。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_database.py::TestSettings tests/test_database.py::TestSettingsToTemplateMigration -v`
Expected: PASS

- [ ] **Step 5: 跑整套测试确认全绿**

Run: `pytest -q`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add models/database.py tests/test_database.py
git commit -m "feat(db): get_settings 收窄为引擎级 + 策略键一次性迁移进默认模板(幂等)"
```

---

## 验收 checkpoint（对应 spec §六）

完成全部 Task 后逐项确认：

1. **默认模板行为基线**：全新库 `init()` 自动建默认模板（策略键 = `TEMPLATE_DEFAULTS`，`test_fresh_install_no_copy`）；老库升级时策略键搬入默认模板（`TestSettingsToTemplateMigration`）。用默认模板跑行为与升级前一致，唯一差异 = 体育/电竞/天气不再进候选池（`excluded_categories` 默认值，`TestFetchCandidatesCategoryWiring`）。
2. **多模板隔离**：`TestFilterForTemplate::test_two_templates_yield_different_lists` 证明同一候选池经两模板精筛得不同 eligible。
3. **共享采集 + 每钱包筛选**：`fetch_candidates` 网络调用 = `1 全量 + N 品类`（与钱包数无关）；`_do_scan` 每钱包各自 `filter_for_template`（`test_auto_do_scan_filters_per_wallet_and_places`）。
4. **单元测试**：Task 3/12（模板 CRUD + 回落 + 迁移 + 幂等）、Task 5（品类纯函数）、Task 7（采集+精筛）全绿；`pytest -q` 全绿。

## 范围之外（明确不做，留给后续子项目）

- 不重写 `determine_order_price`（SP2）。
- 不改离场/止损算法（SP3）。
- 不加单份奖励阈值/取档（SP4）。
- 不改扫描节奏为三档、不加观察名单、不细化采集进度回调（SP5）。
- 不做模板管理前端（SP6）；本计划只到 DB 层 + 后端最小接线，前端沿用现有"全局参数"表单。
