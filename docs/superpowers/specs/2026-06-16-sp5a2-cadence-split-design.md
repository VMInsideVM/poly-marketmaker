# SP5a-2：节奏拆分（发现慢 / 下单快）设计 / spec

> 日期：2026-06-16
> 状态：待用户评审
> SP5a 的第二个子块（SP5a-1 跌出撤单已合并）。父背景见记忆 [[v4-strategy-integration-roadmap]]。

## 零、背景与定位

v4 §6 三档节奏（观察名单已按 YAGNI 去掉，余「4h 全量发现 + 实时看守」）：**每 4h** 扫描发现新市场；下单/重判按快节奏跑。

**现状**：自动循环 `_scanner_loop`（`scan_interval_sec` 默认 30s）每轮 `_do_scan()` = `_scan_with_status()`（昂贵的全量奖励发现 + 订单簿）+ 每钱包 `filter_for_template` + `place_orders`。即「全量奖励市场发现」每 30s 跑一次（`get_rewards_markets` 全量 + 品类 + 逐市场 `get_rewards_for_market` N 次），网络密集、有限流风险。

**SP5a-2 范围（方案 A，已与用户确认）**：把 `fetch_candidates` 里的**奖励发现**（rewards 端点）与**订单簿刷新**拆开——奖励发现挪到慢节奏（4h，可配），订单簿刷新 + 选品 + 下单仍按快节奏（30s）从缓存候选池跑。`filter_for_template` 的价差/价区门槛读缓存 `_orderbooks`，故每个下单轮先刷新订单簿再选品，保证不基于过期盘口。

**不在 SP5a-2**：把价差/价区门槛挪进 `place_orders`、订单簿只取奖励通过子集（方案 B，更省 API 但重构大）——留作以后。SP6 模板 UI。

## 一、已确认决策

| | 决策 |
| --- | --- |
| 拆分形态 | **方案 A**：`fetch_candidates` 拆成 `discover_candidates`（奖励发现，不抓簿）+ `refresh_orderbooks(pool)`（刷 `_orderbooks`）；`fetch_candidates` = 两者合一（手动/测试不变） |
| 发现间隔 | **4h，可配**：`ENGINE_DEFAULTS["discovery_interval_sec"] = 14400` |
| 订单簿 | 仍**每个下单轮给全池刷**（2N 调用，与今天相同）；省的是每轮 N 次奖励调用 + 全量奖励列表调用 |
| scan-age | `last_scan_time` = 发现时间（候选表本就只在发现时变，语义正确）；**前端不改** |
| `filter_for_template` / `place_orders` | **不动** |

## 二、Scanner 拆分（`engine/scanner.py`）

现有 `fetch_candidates(templates, on_progress, on_found, skip_orderbook=False)`：循环 pool，每市场做 min_floor 过滤、`upsert_market_meta`、`get_rewards_for_market`、置 market 字段、`if not skip_orderbook: market["_orderbooks"] = self._fetch_orderbooks(market)`、`on_progress`、`on_found`、append。

**(a) `discover_candidates(self, templates, on_progress=None, on_found=None)`（新）**：把现有 `fetch_candidates` 的循环体**整体搬过来，但去掉订单簿抓取那一行**（`market["_orderbooks"] = ...`）。返回带全部奖励/meta 字段、但**不含 `_orderbooks`** 的候选池。`on_found(market)` 仍在循环内逐市场回调（供 `/api/eligible` 扫描中流式显示——显示不需要订单簿）。

**(b) `refresh_orderbooks(self, pool)`（新）**：
```python
def refresh_orderbooks(self, pool):
    """给候选池每个市场刷新订单簿快照(覆盖写)。钱包无关、可重复调。"""
    for market in pool:
        market["_orderbooks"] = self._fetch_orderbooks(market)
```
覆盖写（非合并）：某市场/ token 抓不到时 `_fetch_orderbooks` 返回缺该 token 的 dict，`filter_for_template` 现有逻辑 `if not book: continue` 自然跳过，**不会留用上一轮的陈旧簿**。

**(c) `fetch_candidates(...)` 重构**为：
```python
def fetch_candidates(self, templates, on_progress=None, on_found=None, skip_orderbook=False):
    pool = self.discover_candidates(templates, on_progress=on_progress, on_found=on_found)
    if not skip_orderbook:
        self.refresh_orderbooks(pool)
    return pool
```
手动扫描（`scan_markets`）与既有单测走默认 `skip_orderbook=False` → 行为不变（带订单簿）。

`_fetch_orderbooks` / `filter_for_template` / `place_orders` **不改**。

## 三、EngineManager 循环（`engine/manager.py`）

**(a) 发现决策助手（纯逻辑，可单测）**：
```python
def _should_discover(self, now: float) -> bool:
    """无缓存池、或距上次发现 >= discovery_interval -> 该重新发现。"""
    interval = self.db.get_settings()["discovery_interval_sec"]
    return (not self.eligible_markets) or (now - self.last_scan_time) >= interval
```

**(b) `_scanner_loop` 重构**（快节奏每轮：按需发现 + 必下单轮）：
```python
def _scanner_loop(self):
    settings = self.db.get_settings()
    place_interval = settings["scan_interval_sec"]
    while not self._stop_event.is_set():
        if self._scanner_api and self.engines:
            try:
                if self._should_discover(time.time()):
                    self._discover()
            except Exception as e:
                logger.error("Discovery error: %s", e)
            try:
                self._place_round()
            except Exception as e:
                logger.error("Place round error: %s", e)
        self._stop_event.wait(timeout=place_interval)
```
（`import time` 已在模块顶部。**发现与下单各自独立 try**:发现失败不应拖垮下单——`_scan_with_status` 失败时保留 `prev_eligible` 并重抛,catch 后 `_place_round` 仍用上一份缓存池继续跑;首次启动若发现失败则池子空,`_place_round` 的空池 guard 自然跳过。)

**(c) `_discover()`（新）** = 发现-only：
```python
def _discover(self):
    """慢节奏:全量奖励发现(不抓订单簿),刷新缓存候选池 + 持久化。"""
    self._scan_with_status(skip_orderbook=True)
```
给 `_scan_with_status` 加 `skip_orderbook=False` 形参，内部 `scanner.fetch_candidates(..., skip_orderbook=skip_orderbook)`。`_scan_with_status` 仍设 `eligible_markets`/`last_scan_time`/`save_eligible_markets`/状态报告/失败保留 `prev_eligible` 不变。发现-only 时池子无 `_orderbooks`（持久化本就不存订单簿，无影响）。

**(d) `_place_round()`（新，替代旧 `_do_scan` 的下单部分）**：
```python
def _place_round(self):
    """快节奏:刷新订单簿 -> 每钱包精筛 + 下单(跌出撤单)。空池跳过。"""
    if not self.eligible_markets:
        return
    scanner = MarketScanner(self._scanner_api, self.db, "")
    scanner.refresh_orderbooks(self.eligible_markets)
    for address, worker in self.engines.items():
        if not worker.running:
            continue
        try:
            tmpl = self.db.get_template_for(address)
            eligible = scanner.filter_for_template(
                self.eligible_markets, tmpl, address
            )
            eligible.sort(
                key=lambda m: float(m.get("market_competitiveness", 0) or 0)
            )
            worker.place_orders(eligible, cancel_dropouts=True)
        except Exception as e:
            logger.error("Error distributing to wallet %s: %s", address, e)
```
`refresh_orderbooks` 原地给 `self.eligible_markets` 的市场 dict 填 `_orderbooks`（`/api/eligible` 显示忽略该字段，routes 用 `dict(m)` 浅拷贝防迭代期变更，安全）。

**(e) 删 `_do_scan`**（仅被 loop 用）。其测试改指向 `_place_round`（见 §五）。空候选池跳过下单的 SP5a-1 防误撤 guard 从 `_do_scan` 迁到 `_place_round`。

## 四、配置（`config.py`）

`ENGINE_DEFAULTS` 加 `"discovery_interval_sec": 14400`（4h）。引擎级、全局单值（与 `scan_interval_sec` 并列）。

## 五、手动路径与既有机制（保留不变）

- **手动扫描** `scan_markets()` → `_scan_with_status()`（默认 `skip_orderbook=False`，带订单簿）→ 显示 + 后续手动 `place_all_orders()`。两者均不变。
- `place_all_orders()` 仍有 `if not self.eligible_markets: return` guard、传 `cancel_dropouts=True`（SP5a-1），不变。
- SP5b 单侧暂停 / SP5c 撤改收敛 / Step3 实时复查 / 离场 / 跌出撤单逻辑都在 `place_orders` / monitor 内，**不动**。

## 六、边界与数据流

发现(4h)：`discover_candidates` → `eligible_markets`(无簿) + 持久化 + `last_scan_time`。
下单轮(30s)：`refresh_orderbooks(eligible_markets)`(填簿) → 每钱包 `filter_for_template`(读新簿)+ `place_orders`。
- 启动首轮：`eligible_markets` 空 → `_should_discover` 真 → 先发现再下单。
- 发现失败：`_scan_with_status` 保留 `prev_eligible` 并重抛 → `_scanner_loop` catch；本轮 `_place_round` 仍会用上一份缓存池跑（订单簿照刷),不空转。
- 订单簿全池刷与今天同量（无回归）；省每轮 N 次奖励调用。

## 七、测试

- **Scanner**（`tests/test_scanner.py`）：
  - `discover_candidates` 产出的市场**不含 `_orderbooks`** 键（用 stub API：`get_rewards_markets`/`get_rewards_for_market` 桩，断言结果无 `_orderbooks`）。
  - `refresh_orderbooks(pool)` 给每市场填 `_orderbooks`（覆盖写：先塞一个假 `_orderbooks`，调用后被新值覆盖）。
  - `fetch_candidates` 默认仍 = discover + refresh（结果含 `_orderbooks`），既有用例不回归。
- **EngineManager**（`tests/test_manager.py`）：
  - `_should_discover`：`eligible_markets=[]` → True；刚发现（`last_scan_time≈now`）→ False；`last_scan_time` 早于 `now-interval` → True。
  - `_place_round`：空 `eligible_markets` → 不调 `place_orders`（迁移 SP5a-1 空池 guard 测试）；非空 → 调 `refresh_orderbooks` + 每钱包 `place_orders(..., cancel_dropouts=True)`（迁移 `test_auto_do_scan_filters_per_wallet_and_places` / `..._distributes_sorted...`）。
  - `_discover`：调 `_scan_with_status(skip_orderbook=True)`、设 `last_scan_time`（可用 FakeScanner，断言其 `fetch_candidates` 收到 `skip_orderbook=True` 或结果无簿）。
- **config**（`tests/test_database.py`）：`ENGINE_DEFAULTS["discovery_interval_sec"] == 14400`、合并生效。

## 八、验收 checkpoint

1. 自动循环：奖励发现按 `discovery_interval_sec`（4h）跑；下单/重判按 `scan_interval_sec`（30s）跑（`_should_discover` + `_scanner_loop`）。
2. 每个下单轮先 `refresh_orderbooks` 再 `filter_for_template` → 选品基于新鲜盘口（非 4h 陈旧）。
3. 空候选池跳过下单（防 `cancel_dropouts` 误撤）。
4. 手动扫描/下单、SP5b/SP5c/Step3/离场/跌出撤单不受影响。
5. `last_scan_time` = 发现时间；前端不改。
6. `pytest` 全绿。

## 九、范围之外

方案 B（价差/价区门槛挪进 `place_orders`、订单簿只取子集，更省 API）· SP6 模板 UI。
