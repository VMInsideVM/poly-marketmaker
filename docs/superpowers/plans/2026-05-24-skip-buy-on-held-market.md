# 已持仓市场跳过下单 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 下单时，只要某市场（按 `condition_id`）名下已有持仓（任一方向 size>0），就跳过、不再为它挂任何买单。

**Architecture:** 新增纯函数 `held_condition_ids(positions)`（`engine/positions.py`）算出已持仓的 condition_id 集合；`WalletWorker.place_orders`（`engine/manager.py`）在循环前读一次该钱包持仓（取不到则该轮不下单），循环里 `market_id in held` 即跳过。

**Tech Stack:** Python 3.12, pytest。

参考 spec：`docs/superpowers/specs/2026-05-24-skip-buy-on-held-market-design.md`

---

## 文件结构

- `engine/positions.py` — 新增，持仓派生的纯函数 `held_condition_ids`
- `engine/manager.py` — `WalletWorker.place_orders` 接入持仓闸
- 测试：`tests/test_positions.py`（新）、`tests/test_place_orders.py`（已有，补默认 mock + 新用例）

---

### Task 1: `held_condition_ids` 纯函数

**Files:**
- Create: `engine/positions.py`
- Test: `tests/test_positions.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_positions.py`：

```python
"""tests/test_positions.py — pure position-derived helpers."""

from engine.positions import held_condition_ids


def test_includes_condition_with_positive_size():
    assert held_condition_ids([{"conditionId": "c1", "size": 100.0}]) == {"c1"}


def test_excludes_zero_and_negative_size():
    pos = [{"conditionId": "c1", "size": 0}, {"conditionId": "c2", "size": -5}]
    assert held_condition_ids(pos) == set()


def test_excludes_missing_or_empty_condition_id():
    assert held_condition_ids([{"size": 100.0}]) == set()
    assert held_condition_ids([{"conditionId": "", "size": 100.0}]) == set()


def test_empty_list_empty_set():
    assert held_condition_ids([]) == set()


def test_yes_and_no_of_same_condition_collapse_to_one():
    pos = [
        {"conditionId": "c1", "size": 100.0, "asset": "yes"},
        {"conditionId": "c1", "size": 50.0, "asset": "no"},
    ]
    assert held_condition_ids(pos) == {"c1"}


def test_none_and_string_size_are_handled():
    pos = [
        {"conditionId": "c1", "size": None},   # None -> treated as 0 -> excluded
        {"conditionId": "c2", "size": "30"},   # numeric string -> 30 -> included
    ]
    assert held_condition_ids(pos) == {"c2"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_positions.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'engine.positions'`）

- [ ] **Step 3: 实现**

新建 `engine/positions.py`：

```python
# engine/positions.py
"""Pure helpers derived from Polymarket Data API /positions (no network/IO)."""


def held_condition_ids(positions: list[dict]) -> set[str]:
    """返回当前持有仓位(size>0)的 market(condition_id)集合。

    positions 为 Data API /positions 的返回(每项含 conditionId / size)。YES/NO
    任一方向有仓都算该 market 已持仓;缺 conditionId 或 size<=0 的项忽略。size 做
    None/字符串安全转换。
    """
    out: set[str] = set()
    for p in positions:
        cid = p.get("conditionId", "")
        if cid and float(p.get("size", 0) or 0) > 0:
            out.add(cid)
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_positions.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add engine/positions.py tests/test_positions.py
git commit -m "feat: held_condition_ids 纯函数(已持仓的 condition_id 集合)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `place_orders` 接入持仓闸

**Files:**
- Modify: `engine/manager.py`（imports + `WalletWorker.place_orders`）
- Test: `tests/test_place_orders.py`

- [ ] **Step 1: 改测试（先补默认 mock，再加新用例）**

`place_orders` 接入后会调用 `api.get_user_positions(...)`。现有测试未 mock 它，MagicMock 默认返回不可迭代，会让 `held_condition_ids` 迭代时报错。几乎所有用例走 `_worker`/`_worker_capped`，因此在这两个 helper 里加一行默认空持仓即可覆盖；唯一直接构造 `WalletWorker` 的 `test_cap_read_live_from_db_not_constructor_snapshot` 单独补一行。

(a) 在 `_worker` 的 `return` 之前加一行：
```python
    api.get_user_positions.return_value = []  # default: no held positions
```
使其变为：
```python
def _worker(api, db, cap=5):
    # The buy-order cap is read live from db.get_settings() at placement time
    # (not the constructor snapshot), so tests seed it on the db mock.
    db.get_settings.return_value = {
        "max_buy_orders_per_wallet": cap,
        "cooldown_minutes": 20,
    }
    db.get_blacklist_ids.return_value = set()
    api.get_user_positions.return_value = []  # default: no held positions
    return WalletWorker(api, db, "0xWALLET", {"fill_check_interval_sec": 5})
```

(b) 在 `_worker_capped` 的 `return` 之前同样加一行，使其变为：
```python
def _worker_capped(api, db, cap):
    db.get_settings.return_value = {
        "max_buy_orders_per_wallet": cap,
        "cooldown_minutes": 20,
    }
    db.get_blacklist_ids.return_value = set()
    api.get_user_positions.return_value = []  # default: no held positions
    return WalletWorker(
        api,
        db,
        "0xWALLET",
        {"fill_check_interval_sec": 5, "max_buy_orders_per_wallet": cap},
    )
```

(c) 在 `test_cap_read_live_from_db_not_constructor_snapshot` 里，`api.get_balance.return_value = 1000.0` 这一行之后加：
```python
    api.get_user_positions.return_value = []
```

(d) 在文件末尾追加两个新用例：
```python
def test_skips_market_with_existing_position():
    # 该 market(condition_id)已有持仓 -> 不再为它挂买单;非持仓的合格市场照常挂。
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    api.get_user_positions.return_value = [
        {"conditionId": "m0", "size": 100.0, "asset": "t0"}
    ]
    markets = [_market(0), _market(1)]
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders(markets)
    placed_tokens = [c.args[0] for c in api.place_limit_buy.call_args_list]
    assert "t0" not in placed_tokens  # m0 已持仓 -> 跳过
    assert "t1" in placed_tokens      # m1 未持仓 -> 照常挂


def test_no_placement_when_positions_unavailable():
    # 取不到持仓(Data API 报错) -> 该轮不挂任何单(保守兜底)。
    api = MagicMock()
    db = MagicMock()
    db.is_in_cooldown.return_value = False
    api.get_open_orders.return_value = []
    api.get_orderbook.return_value = _ok_orderbook()
    api.get_balance.return_value = 1000.0
    worker = _worker(api, db)
    api.get_user_positions.side_effect = Exception("Data API down")
    with patch("engine.strategy.determine_order_price", return_value=0.40):
        worker.place_orders([_market(0)])  # must not raise
    api.place_limit_buy.assert_not_called()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_place_orders.py -v -k "existing_position or positions_unavailable"`
Expected: FAIL — `test_skips_market_with_existing_position`（m0 仍被挂，`"t0" in placed_tokens`）与 `test_no_placement_when_positions_unavailable`（未接入，仍挂单）都失败。

- [ ] **Step 3: 实现接入**

`engine/manager.py` 顶部 import 区（已有 `from engine.monitor import OrderMonitor` 等）加一行：

```python
from engine.positions import held_condition_ids
```

在 `WalletWorker.place_orders` 中，找到方法开头读取 open_orders 的这段：

```python
        placed = 0
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed for %s: %s", self.wallet_address, e)
            return
        buy_orders = [o for o in open_orders if o.get("side") == "BUY"]
```

在 `buy_orders = ...` 这一行之前插入持仓读取（失败即该轮不下单）：

```python
        try:
            positions = self.api.get_user_positions(self.api.get_funder())
        except Exception as e:
            logger.error(
                "get_user_positions failed for %s, skip placement: %s",
                self.wallet_address,
                e,
            )
            return
        held = held_condition_ids(positions)
```

使该段变为：

```python
        placed = 0
        try:
            open_orders = self.api.get_open_orders()
        except Exception as e:
            logger.error("get_open_orders failed for %s: %s", self.wallet_address, e)
            return
        try:
            positions = self.api.get_user_positions(self.api.get_funder())
        except Exception as e:
            logger.error(
                "get_user_positions failed for %s, skip placement: %s",
                self.wallet_address,
                e,
            )
            return
        held = held_condition_ids(positions)
        buy_orders = [o for o in open_orders if o.get("side") == "BUY"]
```

然后在逐市场循环里，黑名单判断之后、冷却判断之前插入持仓闸。找到：

```python
        for market in eligible_markets:
            if market["market_id"] in blacklist:
                continue
            if self.db.is_in_cooldown(self.wallet_address, market["market_id"]):
                continue
```

改为：

```python
        for market in eligible_markets:
            if market["market_id"] in blacklist:
                continue
            if market["market_id"] in held:
                continue
            if self.db.is_in_cooldown(self.wallet_address, market["market_id"]):
                continue
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_place_orders.py -v`
Expected: PASS（原有用例全绿 + 2 个新用例通过）。

- [ ] **Step 5: 全量回归**

Run: `python -m pytest -q`
Expected: 全绿（原 236 + 本计划新增用例）。

- [ ] **Step 6: 提交**

```bash
git add engine/manager.py tests/test_place_orders.py
git commit -m "feat: 下单跳过已持仓市场(按 condition_id;取不到持仓则该轮不下单)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

## 守住的既有行为（不改）

- 黑名单 / 冷却 / `token_id in open_buy_assets` / 余额 / 最小份额 各闸不变、顺序不变（持仓闸插在黑名单之后）。
- 持仓读取放在 open_orders 之后、cap 逻辑之前：持仓接口失败那一轮连 cap 清理也一起跳过（可接受，spec 已说明）。
- 只跳"新挂"，不撤"已挂"；监控/止盈/止损不碰。
- 每钱包独立（持仓按 funder 查）。

---

## 自查（写完计划后对照 spec）

- **spec 覆盖**：纯函数 `held_condition_ids`(T1)；`place_orders` 接入 + market 级别跳过(T2 Step 3 循环闸)；失败该轮不下单(T2 Step 3 持仓读取 try/except return + 用例 `test_no_placement_when_positions_unavailable`)；market 级别(用例 `test_skips_market_with_existing_position` 用 condition_id `m0`)。✓
- **占位符**：无 TBD/TODO，每个改码步骤含完整代码。✓
- **类型/签名一致**：`held_condition_ids(positions) -> set[str]` 在 T1 定义、T2 import 并以 `market["market_id"] in held` 使用，类型一致（`market_id` 即 condition_id）。✓
- **既有测试保绿**：T2 Step 1 先给 `_worker`/`_worker_capped`/`test_cap_read_live` 补默认空持仓，再接入生产代码，避免现有用例因新调用 `get_user_positions` 报错。✓
