# Gamma 兜底补市场名称 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 当本地 `market_meta` 表解析不到某 condition_id 的名称时,实时调 Polymarket Gamma API 用 condition_id 换取名称+slug 并持久化(带负缓存),让各页几乎总能显示市场名而非截断 condition_id。

**Architecture:** 在已有的"本地映射 + enrich"之上加一层兜底解析。新增 `PolymarketAPI.gamma_markets_by_condition()`(批量 GET /markets?condition_ids=...,已验证可用,返回 {cid: {name, market_slug, event_slug}})。新增纯逻辑 `ensure_market_meta(condition_ids, db, fetch)`(找出不在 `market_meta` 的 id → 调 fetch → 命中 upsert 真名、未命中 upsert 空行做负缓存、fetch 失败则不缓存留待下次)。各 enrich 接口改为先 `ensure_market_meta` 再 `enrich_with_market_meta`。

**Tech Stack:** Python 3, Flask, requests, SQLite, pytest + unittest.mock。

**已验证的 Gamma 事实**(实地探测):`GET https://gamma-api.polymarket.com/markets` 支持重复参数 `condition_ids=a&condition_ids=b` 批量过滤;每个 market 返回 `conditionId`、`question`(名称)、`slug`(market_slug)、`events[0].slug`(event_slug)。

承接:`docs/superpowers/specs/2026-05-23-display-optimization-market-names-design.md`(本次是其"本地映射"方案的兜底升级)。

---

## File Structure

- Modify `api/polymarket_api.py`:新增静态方法 `gamma_markets_by_condition`(放在已有 Gamma 静态方法 `get_market_by_id`/`list_markets` 附近)。
- Modify `engine/market_links.py`:新增 `ensure_market_meta(condition_ids, db, fetch, max_lookup=50)`(保持模块零外部依赖——db 与 fetch 由调用方注入)。
- Modify `web/routes.py`:新增 `_gamma_fetch` + `_enrich_rows` 两个模块级 helper,把 5 个接口里现有的 `enrich_with_market_meta(rows, db.get_market_meta(), key)` 调用替换为 `_enrich_rows(rows, key)`(`/api/eligible` 有两个分支)。
- Test:`tests/test_gamma_resolve.py`(新建,测 `gamma_markets_by_condition`);`tests/test_market_links.py`(追加,测 `ensure_market_meta`)。

依赖顺序:Task 1、Task 2 互相独立;Task 3 依赖 1+2。

---

### Task 1: `gamma_markets_by_condition` 批量解析

**Files:**
- Modify: `api/polymarket_api.py`
- Test: `tests/test_gamma_resolve.py`(新建)

说明:`api/polymarket_api.py` 顶部已 `import requests` 和 `logger`。该方法是 `@staticmethod`(无需鉴权,Gamma 公共接口),调用时不构造 `PolymarketAPI` 对象——测试可直接 `PolymarketAPI.gamma_markets_by_condition(...)` 并 patch `api.polymarket_api.requests.get`。

- [ ] **Step 1: 写失败测试** — 新建 `tests/test_gamma_resolve.py`

```python
"""tests/test_gamma_resolve.py"""

from unittest.mock import patch, MagicMock
from api.polymarket_api import PolymarketAPI


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def test_maps_gamma_fields():
    fake = _resp([
        {"conditionId": "0xc1", "question": "Q1", "slug": "s1",
         "events": [{"slug": "e1"}]},
        {"conditionId": "0xc2", "question": "Q2", "slug": "s2", "events": []},
    ])
    with patch("api.polymarket_api.requests.get", return_value=fake) as g:
        out = PolymarketAPI.gamma_markets_by_condition(["0xc1", "0xc2"])
    assert out["0xc1"] == {"name": "Q1", "market_slug": "s1", "event_slug": "e1"}
    assert out["0xc2"] == {"name": "Q2", "market_slug": "s2", "event_slug": ""}
    params = g.call_args.kwargs["params"]
    assert ("condition_ids", "0xc1") in params
    assert ("condition_ids", "0xc2") in params


def test_empty_input_no_request():
    with patch("api.polymarket_api.requests.get") as g:
        out = PolymarketAPI.gamma_markets_by_condition([])
    assert out == {}
    g.assert_not_called()


def test_dedups_and_drops_empty():
    fake = _resp([])
    with patch("api.polymarket_api.requests.get", return_value=fake) as g:
        PolymarketAPI.gamma_markets_by_condition(["0xc1", "0xc1", "", None])
    ids = [v for (k, v) in g.call_args.kwargs["params"] if k == "condition_ids"]
    assert ids == ["0xc1"]
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_gamma_resolve.py -v`
Expected: FAIL(`AttributeError: ... has no attribute 'gamma_markets_by_condition'`)。

- [ ] **Step 3: 实现** — 在 `api/polymarket_api.py` 的 Gamma 区块(`get_market_by_id` / `list_markets` 附近)新增静态方法:

```python
    @staticmethod
    def gamma_markets_by_condition(condition_ids: list) -> dict:
        """按 condition_id 批量解析市场名+slug(Gamma 公共接口)。

        GET /markets?condition_ids=a&condition_ids=b... 返回 Gamma 已知的市场。
        返回 {condition_id: {"name", "market_slug", "event_slug"}}。
        HTTP/网络失败时抛出(由调用方决定如何处理)。
        """
        ids = [c for c in dict.fromkeys(condition_ids) if c]
        if not ids:
            return {}
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params=[("condition_ids", c) for c in ids],
            timeout=10,
        )
        resp.raise_for_status()
        out = {}
        for m in resp.json():
            cid = m.get("conditionId", "")
            if not cid:
                continue
            evs = m.get("events") or []
            out[cid] = {
                "name": m.get("question", ""),
                "market_slug": m.get("slug", ""),
                "event_slug": (evs[0].get("slug", "") if evs else ""),
            }
        return out
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_gamma_resolve.py -v`
Expected: 3 个 PASS。

- [ ] **Step 5: 提交**(只 stage 这两个文件)

```bash
git add api/polymarket_api.py tests/test_gamma_resolve.py
git commit -m "feat: Gamma 按 condition_id 批量解析市场名+slug

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `ensure_market_meta` 兜底解析器

**Files:**
- Modify: `engine/market_links.py`
- Test: `tests/test_market_links.py`(追加)

说明:`engine/market_links.py` 目前是零依赖纯模块(只有 `market_url`/`enrich_with_market_meta`)。`ensure_market_meta` 保持这一点——`db` 与 `fetch` 都由调用方注入,便于用 MagicMock 测,无网络。

- [ ] **Step 1: 写失败测试** — 追加到 `tests/test_market_links.py` 末尾(文件顶部已 `from engine.market_links import market_url, enrich_with_market_meta`;改成同时导入 `ensure_market_meta`):

把文件顶部的导入行
```python
from engine.market_links import market_url, enrich_with_market_meta
```
改为
```python
from engine.market_links import market_url, enrich_with_market_meta, ensure_market_meta
```

并追加(需要 `from unittest.mock import MagicMock` —— 若文件未导入则在顶部加上):

```python
def test_ensure_only_queries_missing_ids():
    db = MagicMock()
    db.get_market_meta.return_value = {
        "0xKNOWN": {"name": "K", "market_slug": "", "event_slug": ""}
    }
    fetch = MagicMock(return_value={})
    ensure_market_meta(["0xKNOWN", "0xNEW"], db, fetch)
    fetch.assert_called_once()
    called_ids = list(fetch.call_args.args[0])
    assert "0xNEW" in called_ids and "0xKNOWN" not in called_ids


def test_ensure_upserts_resolved_name():
    db = MagicMock()
    db.get_market_meta.return_value = {}
    fetch = MagicMock(
        return_value={"0xNEW": {"name": "Q", "market_slug": "s", "event_slug": "e"}}
    )
    ensure_market_meta(["0xNEW"], db, fetch)
    db.upsert_market_meta.assert_any_call("0xNEW", "Q", "s", "e")


def test_ensure_negative_caches_true_miss():
    db = MagicMock()
    db.get_market_meta.return_value = {}
    fetch = MagicMock(return_value={})  # Gamma 应答了但不含该 id
    ensure_market_meta(["0xMISS"], db, fetch)
    db.upsert_market_meta.assert_any_call("0xMISS", "", "", "")


def test_ensure_no_upsert_on_fetch_failure():
    db = MagicMock()
    db.get_market_meta.return_value = {}
    fetch = MagicMock(side_effect=Exception("gamma down"))
    ensure_market_meta(["0xNEW"], db, fetch)  # 不得抛出
    db.upsert_market_meta.assert_not_called()


def test_ensure_skips_fetch_when_all_present():
    db = MagicMock()
    db.get_market_meta.return_value = {
        "0xA": {"name": "A", "market_slug": "", "event_slug": ""}
    }
    fetch = MagicMock()
    ensure_market_meta(["0xA"], db, fetch)
    fetch.assert_not_called()


def test_ensure_ignores_empty_ids():
    db = MagicMock()
    db.get_market_meta.return_value = {}
    fetch = MagicMock(return_value={})
    ensure_market_meta(["", None], db, fetch)
    fetch.assert_not_called()
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_market_links.py -k ensure -v`
Expected: FAIL(`ImportError: cannot import name 'ensure_market_meta'` 或 `AttributeError`)。

- [ ] **Step 3: 实现** — 在 `engine/market_links.py` 末尾追加:

```python
def ensure_market_meta(condition_ids, db, fetch, max_lookup: int = 50):
    """确保 market_meta 含给定 condition_ids 的条目,缺的用 fetch(Gamma)解析并落库。

    - 只解析不在 market_meta 里的 id(本地命中/已负缓存的不再查)。
    - fetch 返回的命中项 upsert 真名+slug;Gamma 应答但不含的 id upsert 空行
      做负缓存(避免每次轮询重打 Gamma)。
    - fetch 抛异常(Gamma 临时不可用)则不写负缓存,留待下次轮询重试。
    - 单次最多解析 max_lookup 个,其余下次轮询再补。
    返回(可能已更新的)condition_id -> 元信息 映射。永不抛出。
    """
    meta = db.get_market_meta()
    missing = [c for c in dict.fromkeys(condition_ids) if c and c not in meta]
    if not missing:
        return meta
    missing = missing[:max_lookup]
    try:
        resolved = fetch(missing)
    except Exception:
        return meta  # 临时失败:不负缓存,下次重试
    for c in missing:
        r = resolved.get(c)
        if r:
            db.upsert_market_meta(
                c, r.get("name", ""), r.get("market_slug", ""), r.get("event_slug", "")
            )
        else:
            db.upsert_market_meta(c, "", "", "")  # 负缓存:Gamma 无此市场
    return db.get_market_meta()
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_market_links.py -v`
Expected: 全部 PASS(原有 11 + 新增 6 = 17)。

- [ ] **Step 5: 提交**(只 stage 这两个文件)

```bash
git add engine/market_links.py tests/test_market_links.py
git commit -m "feat: ensure_market_meta 兜底解析器(缺名调 Gamma+负缓存)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 5 个接口接入兜底解析

**Files:**
- Modify: `web/routes.py`

无路由级单测(项目无此基建);核心逻辑已被 Task 1/2 单测覆盖。验证:导入冒烟 + 全套 pytest。

- [ ] **Step 1: 改导入**

把 `web/routes.py` 里这行:

```python
from engine.market_links import enrich_with_market_meta
```

改为:

```python
from engine.market_links import enrich_with_market_meta, ensure_market_meta
```

- [ ] **Step 2: 加两个 helper**

在 `web/routes.py` 的 helper 区(`_wallet_apis` 函数定义之后)加入:

```python
def _gamma_fetch(condition_ids):
    """Gamma 解析回调(惰性导入 PolymarketAPI,沿用本文件其它处的惰性导入约定)。"""
    from api.polymarket_api import PolymarketAPI

    return PolymarketAPI.gamma_markets_by_condition(condition_ids)


def _enrich_rows(rows, id_key):
    """给 rows 补市场名+链接:先用 Gamma 兜底补全 market_meta,再 enrich。"""
    meta = ensure_market_meta([r.get(id_key, "") for r in rows], db, _gamma_fetch)
    enrich_with_market_meta(rows, meta, id_key)
```

- [ ] **Step 3: 替换 5 个接口的 enrich 调用(共 6 处)**

将下列每一处替换:

`/api/orders`(`api_get_orders`):
```python
    enrich_with_market_meta(result, db.get_market_meta(), "market")
```
→
```python
    _enrich_rows(result, "market")
```

`/api/positions`(`api_get_positions`):
```python
    enrich_with_market_meta(out, db.get_market_meta(), "condition_id")
```
→
```python
    _enrich_rows(out, "condition_id")
```

`/api/actions`(`api_get_actions`):
```python
    enrich_with_market_meta(rows, db.get_market_meta(), "market_id")
```
→
```python
    _enrich_rows(rows, "market_id")
```

`/api/monitor-status`(`api_monitor_status`):
```python
    enrich_with_market_meta(snap.get("rows", []), db.get_market_meta(), "market")
```
→
```python
    _enrich_rows(snap.get("rows", []), "market")
```

`/api/eligible`(`api_eligible_markets`)——**两个分支各一处**,均为:
```python
        enrich_with_market_meta(markets, db.get_market_meta(), "market_id")
```
→
```python
        _enrich_rows(markets, "market_id")
```
(no-manager 分支缩进可能不同,按各自现有缩进替换;两处都要改。)

- [ ] **Step 4: 导入冒烟 + 全套测试**

Run: `python -c "import web.routes; print('import ok')"`
Expected: `import ok`。

Run: `pytest -q`
Expected: 全部 PASS(原 191 + Task1 的 3 + Task2 的 6 = 200)。

- [ ] **Step 5: 提交**(只 stage `web/routes.py`)

```bash
git add web/routes.py
git commit -m "feat: 各接口缺名时调 Gamma 兜底补市场名(经 ensure_market_meta)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec/设计覆盖:**
- Gamma 批量解析(字段映射、按 condition_id) → Task 1 + `test_maps_gamma_fields` ✓
- 兜底解析器:只查缺失 / 命中 upsert 真名 / 未命中负缓存 / 失败不缓存 / 全命中不查 → Task 2 + 6 个测试逐条覆盖 ✓
- 5 接口接入 → Task 3(orders/positions/actions/monitor-status/eligible×2) ✓
- 永不因 Gamma 异常中断接口 → `ensure_market_meta` try/except + `test_ensure_no_upsert_on_fetch_failure` ✓
- 负缓存避免反复打 Gamma → `ensure_market_meta` 对未命中 upsert 空行 + 下次"不在 meta"判定跳过 ✓

**2. 占位符扫描:** 无 TBD/TODO;每步给完整代码+确切命令+预期。✓

**3. 类型/签名一致:**
- `gamma_markets_by_condition(condition_ids) -> {cid: {name, market_slug, event_slug}}` — Task 1 定义、Task 2 测试的 fetch mock 返回同形 dict、`ensure_market_meta` 按 `r.get("name"/"market_slug"/"event_slug")` 读取,一致。✓
- `ensure_market_meta(condition_ids, db, fetch, max_lookup=50)` — Task 2 定义、Task 3 `_enrich_rows` 以 `(ids, db, _gamma_fetch)` 三参调用(用默认 max_lookup),一致。✓
- `db.upsert_market_meta(cid, name, market_slug, event_slug)` — 与既有 Task(展示优化)定义的 4 位置参数一致。✓
- `_enrich_rows(rows, id_key)` 调用的 id_key 与各接口的 condition_id 字段名一致(orders `market`/positions `condition_id`/actions `market_id`/monitor `market`/eligible `market_id`),沿用展示优化里已验证的对应关系。✓
