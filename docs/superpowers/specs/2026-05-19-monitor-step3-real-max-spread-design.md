# 监控 Step 3 使用每个市场真实 rewards_max_spread 设计

日期：2026-05-19

## 背景与问题

监控 Step 3 策略合规检查 `OrderMonitor._check_compliance`（`engine/monitor.py`）在重算"现在应挂价"时，把 `max_spread` **写死为 2**（`engine/monitor.py:189`，注释说明订单对象上没有 `rewards_max_spread`，只能用默认值）。

挂单端 `manager.place_orders` 用的是扫描得到的该市场**真实** `rewards_max_spread`（可能是 3、4、99…）。对真实 `max_spread≠2` 的市场，Step 3 用 `max_spread=2`（更严格的 `bid1>2000→bid2，否则不挂`规则）重算，`determine_order_price` 很容易返回 `None` → `needs_replace` 返回 `"cancel"` → 单子在启动监控后第一个 tick（默认 5 秒）就被撤掉且不重挂。这是用户实测"挂 3 单、启动监控后 2 单被撤"的根因。

## 目标

Step 3 检查每个在挂买单时，使用该单所属市场的**真实** `rewards_max_spread`（来自 `/rewards/markets/{condition_id}`），不再写死 2。取不到时安全跳过，绝不因取数失败而误撤。

## 数据来源（已实测确认）

在挂订单对象含 `market` 字段 = condition_id（实测样例 `"market": "0x…01"`）。

`PolymarketAPI.get_rewards_for_market(condition_id)`（已存在，静态方法，`GET /rewards/markets/{condition_id}`）返回形如：

```json
{ "data": [ { "condition_id": "0x…",
              "rewards_max_spread": 99,
              "rewards_min_size": 10,
              "rewards_config": [ {"rate_per_day": 0.25, ...}, ... ],
              ... } ] }
```

`PolymarketAPI.get_rewards_for_market` 把 `data` 列表展开返回（`return all_data`，即返回 `data` 里那些 item 组成的 list）。**`rewards_max_spread` 在每个 item 的顶层**（实测确认），不在 `rewards_config` 内。

## 组件设计

### 1. 纯函数 `extract_max_spread`（新文件 `engine/rewards.py`）

与 `engine/fills.py` / `engine/risk.py` 同风格的纯逻辑，无网络/IO，可独立单测。

```python
from typing import Optional

def extract_max_spread(rewards_items: list) -> Optional[int]:
    """从 get_rewards_for_market 的返回（item 列表）解析 rewards_max_spread。

    取第一个含有效 rewards_max_spread 的 item 顶层值并 int() 化。
    列表为空 / 无该字段 / 无法转 int → 返回 None（调用方据此安全跳过）。
    """
    for it in rewards_items or []:
        if not isinstance(it, dict):
            continue
        v = it.get("rewards_max_spread")
        if v is None:
            continue
        try:
            return int(float(v))
        except (TypeError, ValueError):
            continue
    return None
```

### 2. `OrderMonitor` 内的 TTL 缓存

`OrderMonitor.__init__` 新增：

```python
self._max_spread_cache: dict[str, tuple[int, float]] = {}  # cid -> (max_spread, fetched_at)
```

新增私有方法：

```python
def _market_max_spread(self, condition_id: str) -> Optional[int]:
    """该市场真实 rewards_max_spread；带 TTL 缓存；取不到返回 None。"""
    if not condition_id:
        return None
    ttl = self.db.get_settings()["rewards_cache_ttl_sec"]
    now = time.time()
    hit = self._max_spread_cache.get(condition_id)
    if hit and (now - hit[1]) < ttl:
        return hit[0]
    try:
        items = self.api.get_rewards_for_market(condition_id)
    except Exception as e:
        logger.warning("get_rewards_for_market(%s) failed: %s", condition_id, e)
        return None
    ms = extract_max_spread(items)
    if ms is None:
        return None
    self._max_spread_cache[condition_id] = (ms, now)
    return ms
```

- 仅在成功解析出 `ms` 时写缓存；失败不缓存（下个 tick 会重试）。
- `time` 模块需在 `engine/monitor.py` 顶部 `import time`。

### 3. 修改 `_check_compliance`

把：

```python
        # rewards_max_spread is not on the order; recover from settings default
        max_spread = 2
```

改为：

```python
        max_spread = self._market_max_spread(o.get("market", ""))
        if max_spread is None:
            return  # 取不到真实 max_spread：本轮跳过，绝不误撤/误重挂
```

其后 `rmin/rmax = midpoint ∓ max_spread*tick`、`determine_order_price`、`needs_replace`、keep/replace/cancel 逻辑**完全不变**。

### 4. 配置项

- `config.py` `DEFAULTS` 新增 `"rewards_cache_ttl_sec": 600`。
- `web/templates/config.html` 「运行参数」区新增一个输入框：
  ```html
  <div class="form-group">
      <label>奖励参数缓存 (秒)</label>
      <input type="number" name="rewards_cache_ttl_sec" step="1">
  </div>
  ```
  `loadSettings`/提交逻辑是通用的（按 name 填充、`FormData`→`parseFloat`→POST），无需改 JS。
- `db.get_settings()` 合并 `DEFAULTS`，该键恒有值；监控 `self.db.get_settings()["rewards_cache_ttl_sec"]` 读取（与 `stop_loss_pct`/`cooldown_minutes` 同方式）。

## 行为定义

- Step 3 对每个 `side=="BUY"` 且 `size_matched==0` 的在挂单：取该单 `market` 的真实 `rewards_max_spread`。
- 取到 → 用真实值算 `rmin/rmax`，其余判定不变（可能 keep/replace/cancel）。
- 取不到（`get_rewards_for_market` 异常 / 解析 `None` / `market` 为空）→ `_check_compliance` 直接 `return`：本轮**不撤、不重挂**该单。
- TTL 内同一 `condition_id` 复用缓存，不重复请求。

## 作用域 / 不改动项

- 只改 `engine/monitor.py`（`_check_compliance`、`OrderMonitor.__init__`、新增 `_market_max_spread`、`import time`）、新增 `engine/rewards.py`、`config.py`、`web/templates/config.html`。
- **不改** `manager.place_orders`（挂单端已用扫描的真实 `rewards_max_spread`）。
- **不改** `engine/strategy.py`（`determine_order_price`）、`engine/strategy_check.py`（`needs_replace`）——核心 IP，逻辑不变，仅喂入的 `max_spread` 变真实值。
- **不改** Step 1 成交检测、Step 2 止损、`are_orders_scoring`（只读展示）。

## 错误处理

- `get_rewards_for_market` 异常被 `_market_max_spread` 捕获并记 `warning`，返回 `None` → 安全跳过。
- 单个订单 `_check_compliance` 异常仍由 `check_sell_orders` 现有 `try/except` 兜住（不影响其他订单）。

## 测试

新增 `tests/test_rewards.py`（纯函数）：

- 顶层有 `rewards_max_spread`（如实测样例 99）→ 返回 99。
- 字符串/浮点数值（如 `"3"`、`3.0`）→ 正确 `int()` 化为 3。
- 空列表 `[]` → `None`。
- item 无 `rewards_max_spread` 字段 → `None`。
- 多个 item，第一个无该字段、第二个有 → 返回第二个的值。

扩 `tests/test_monitor.py`：

- Step 3 用真实 max_spread：mock `api.get_rewards_for_market` 返回 `[{"rewards_max_spread": 3}]`，patch `determine_order_price`，断言其入参 `max_spread==3`（不是 2）。
- 缓存命中：同一 `condition_id` 连续两次 `_check_compliance`，`api.get_rewards_for_market` 只被调用一次（TTL 内）。
- 取不到即跳过：`api.get_rewards_for_market` 抛异常 → 该单 **不** `cancel_orders`、**不** `place_limit_buy`。
- 解析为 None 即跳过：`api.get_rewards_for_market` 返回 `[{}]` → 同上跳过。
- 现有 `tests/test_monitor.py` 中原假设 `max_spread=2` 的 Step 3 用例：改为 mock `get_rewards_for_market` 提供显式 max_spread，保持语义。

## 不做（YAGNI）

- 不做缓存持久化（进程内存即可；重启后重新拉一次，TTL 内自然收敛）。
- 不做缓存主动失效/清理（条目数 = 在挂市场数，量极小）。
- 不改挂单端、不改策略纯函数。
- 不引入除 `rewards_cache_ttl_sec` 外的新配置。
