# SP5a-1 跌出 eligible 整仓撤单 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在每轮真正下单时,把「我们有在挂买单、但已不在该钱包本轮 eligible(且不在冷却)」的市场识别为跌出,撤掉这些市场的全部买单(持仓/卖单不动)。

**Architecture:** 给 `WalletWorker.place_orders` 加一个 `cancel_dropouts=False` 开关;开时在已算好的 `markets_with_open` − `eligible_mids` − 冷却市场上批量撤买单、记 `dropout_cancel`。仅 `_do_scan`(自动)与 `place_all_orders`(手动)两个真正下单轮传 `True`;`test_place_orders`(测试按钮)保持默认关。

**Tech Stack:** Python 3.12 / pytest(MagicMock 桩 API)。

**执行顺序:** 单任务 TDD。基线:SP5b 合并后 `391 passed`。

---

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `engine/manager.py` | `place_orders` 加 `cancel_dropouts` 开关 + 跌出撤单段;两处真正下单调用点传 `True` | 修改 |
| `tests/test_place_orders.py` | 跌出撤单 / 冷却豁免 / 默认关 / 只撤买单 四个测试 | 修改 |

---

## Task 1: place_orders 跌出 eligible 整仓撤单

**Files:**
- Modify: `engine/manager.py`(`place_orders` 签名第 94 行、跌出段插入、调用点第 418/597 行)
- Test: `tests/test_place_orders.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_place_orders.py` 末尾追加四个测试(复用现有 `_make_worker` / `_ob` / `_elig`):

```python
def test_dropout_cancels_ineligible_market_buys():
    # 在市场 B 有买单,本轮 eligible 只有 A,cancel_dropouts=True -> B 买单被撤、A 照常挂。
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {"side": "BUY", "market": "B", "asset_id": "B-y", "price": "0.40",
         "original_size": "100", "id": "o-b"}
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")], cancel_dropouts=True)
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-b" in cancelled
    placed_tokens = [c.args[0] for c in api.place_limit_buy.call_args_list]
    assert "A-y" in placed_tokens


def test_dropout_skips_cooldown_but_cancels_others():
    # C 冷却 -> 保留;D 非冷却且跌出 -> 撤。证明冷却豁免是有选择的(非空判定)。
    worker, api, db = _make_worker()
    db.is_in_cooldown.side_effect = lambda w, m: (m == "C")
    api.get_open_orders.return_value = [
        {"side": "BUY", "market": "C", "asset_id": "C-y", "price": "0.40",
         "original_size": "100", "id": "o-c"},
        {"side": "BUY", "market": "D", "asset_id": "D-y", "price": "0.40",
         "original_size": "100", "id": "o-d"},
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")], cancel_dropouts=True)
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-d" in cancelled       # 跌出(非冷却) -> 撤
    assert "o-c" not in cancelled   # 冷却 -> 保留


def test_dropout_off_by_default():
    # cancel_dropouts 默认 False(测试挂单路径):跌出市场买单不被撤。
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {"side": "BUY", "market": "B", "asset_id": "B-y", "price": "0.40",
         "original_size": "100", "id": "o-b"}
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")])
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-b" not in cancelled


def test_dropout_cancels_only_buys_not_sells():
    # 跌出市场若有卖单,只撤买单、卖单保留。
    worker, api, db = _make_worker()
    api.get_open_orders.return_value = [
        {"side": "BUY", "market": "B", "asset_id": "B-y", "price": "0.40",
         "original_size": "100", "id": "o-b-buy"},
        {"side": "SELL", "market": "B", "asset_id": "B-y", "price": "0.60",
         "original_size": "100", "id": "o-b-sell"},
    ]
    api.get_orderbook.return_value = _ob([(0.30, 1000)], [(0.31, 1000)])
    worker.place_orders([_elig("A", "A-y", "Yes")], cancel_dropouts=True)
    cancelled = [oid for c in api.cancel_orders.call_args_list for oid in c.args[0]]
    assert "o-b-buy" in cancelled and "o-b-sell" not in cancelled
```

- [ ] **Step 2: 运行确认 FAIL**

Run: `python -m pytest tests/test_place_orders.py::test_dropout_cancels_ineligible_market_buys tests/test_place_orders.py::test_dropout_cancels_only_buys_not_sells -v`
Expected: FAIL(`place_orders` 还没有 `cancel_dropouts` 形参 → `TypeError: unexpected keyword argument 'cancel_dropouts'`)

- [ ] **Step 3: 改 `place_orders` 签名(第 94 行)**

把
```python
    def place_orders(self, eligible_markets: list[dict], limit: int | None = None):
```
改为
```python
    def place_orders(
        self,
        eligible_markets: list[dict],
        limit: int | None = None,
        cancel_dropouts: bool = False,
    ):
```

- [ ] **Step 4: 插入跌出撤单段**

在 `grouped, order` 构建循环之后、`placed = 0` 之前插入。当前代码:
```python
        grouped, order = {}, []
        for e in eligible_markets:
            mid = e["market_id"]
            if mid not in grouped:
                grouped[mid] = []
                order.append(mid)
            grouped[mid].append(e)

        placed = 0
```
改为(在 `grouped` 循环和 `placed = 0` 之间插入跌出段):
```python
        grouped, order = {}, []
        for e in eligible_markets:
            mid = e["market_id"]
            if mid not in grouped:
                grouped[mid] = []
                order.append(mid)
            grouped[mid].append(e)

        # SP5a-1 跌出 eligible 整仓撤买单:有在挂买单、不在本轮 eligible、且不在
        # 冷却的市场 -> 撤掉该市场全部 BUY(持仓/卖单不动,仍由 check_exit 卖出)。
        # 只在真正下单轮(_do_scan / place_all_orders)开启;冷却市场只是「暂不挂新单」
        # 故豁免,避免撤掉正赚奖励的旧买单、并不与 SP5b「另一侧照常运行」冲突。
        if cancel_dropouts:
            eligible_mids = set(grouped.keys())
            dropped = {
                o.get("market", "")
                for o in buy_orders
                if o.get("market")
                and o.get("market") not in eligible_mids
                and not self.db.is_in_cooldown(self.wallet_address, o.get("market"))
            }
            drop_ids = [
                o["id"]
                for o in buy_orders
                if o.get("market") in dropped and o.get("id")
            ]
            if drop_ids:
                try:
                    self.api.cancel_orders(drop_ids)
                    markets_with_open -= dropped
                    for mkt in dropped:
                        self.db.record_action(
                            wallet=self.wallet_address,
                            market_id=mkt,
                            action_type="dropout_cancel",
                            side="-",
                            price=-1,
                            size=0,
                            reason="市场跌出 eligible(不再满足筛选门槛),撤掉该市场全部买单;持仓仍由离场卖出",
                            price_basis="跌出 eligible;来源:CLOB get_open_orders + filter_for_template",
                        )
                except Exception as ex:
                    logger.warning("Dropout cancel failed: %s", ex)

        placed = 0
```

- [ ] **Step 5: 两处真正下单调用点传 `cancel_dropouts=True`**

`engine/manager.py` 有两处 `worker.place_orders(eligible)`(`place_all_orders` 第 418 行、`_do_scan` 第 597 行,字符串完全相同)。把它们都改成传开关。用精确替换(两处都改):

把(出现 2 次)
```python
                worker.place_orders(eligible)
```
改为
```python
                worker.place_orders(eligible, cancel_dropouts=True)
```

> `test_place_orders` 第 468 行的 `worker.place_orders(sorted_markets, limit=3)` **不改**(保持默认 `cancel_dropouts=False`,测试按钮不撤单)。

- [ ] **Step 6: 运行确认 PASS**

Run: `python -m pytest tests/test_place_orders.py -v`
Expected: PASS(新增 4 个 + 既有用例。既有用例 `cancel_dropouts` 默认关、且 positions/open_orders 桩不构成跌出,行为不变。)

- [ ] **Step 7: 全套测试无回归**

Run: `python -m pytest -q`
Expected: ALL PASS(基线 391 + 4 = `395 passed`)。若 `test_manager.py` 有断言 `place_orders` 调用参数的用例因新增 `cancel_dropouts=True` 而失败,按新签名最小修正并报告。

- [ ] **Step 8: Commit(不 stage `.claude/settings.local.json`)**

```bash
git add engine/manager.py tests/test_place_orders.py
git commit -m "feat(manager): 跌出 eligible 整仓撤买单(cancel_dropouts,冷却豁免)"
```

---

## 验收 checkpoint(对应 spec §五)

1. 跌出 eligible(门槛不合格)市场全部买单被撤、记 `dropout_cancel`:`test_dropout_cancels_ineligible_market_buys`。
2. 冷却市场不被当跌出(旧买单保留):`test_dropout_skips_cooldown_but_cancels_others`。
3. `cancel_dropouts` 默认关(测试按钮不误撤):`test_dropout_off_by_default` + `test_place_orders` 调用点不改。
4. 只撤买单、不动卖单/持仓:`test_dropout_cancels_only_buys_not_sells`。
5. eligible 正常挂单 / SP5b / SP5c / 离场不受影响:只动 `place_orders` 跌出段 + 调用点,`pytest -q` 全绿即证。
6. `pytest` 全绿:Step 7。

## 范围之外

SP5a-2 节奏拆分(发现 4h / 下单快)· SP6 模板 UI。
