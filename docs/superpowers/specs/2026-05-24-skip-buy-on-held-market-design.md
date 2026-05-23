# 已持仓市场跳过下单 — 设计

日期：2026-05-24

## 背景

`WalletWorker.place_orders`（`engine/manager.py`）决定一个钱包在哪些合格市场挂买单。当前的去重只看"该 token 上有没有在挂的**买单**"（`open_buy_assets`，仅统计 `side=="BUY"` 的在挂订单），加上黑名单、冷却、余额、最小份额几道闸。

它**不看持仓**。后果（此前调查已确认）：

- 某市场已有持仓（机器人买出来的、或用户手动建的），只要当前该 token 没有在挂买单、且不在冷却内，下一轮扫描就会再挂买单 → 持续加仓。
- 冷却只在机器人自己成交时设置、且有时限（默认 20 分钟），冷却一过或手动仓位（从未设过冷却）都拦不住。

用户要求：**下单时，只要某个市场（按 `condition_id` / market ID）名下已经有持仓（任一 YES/NO 方向，size>0），就跳过这个市场、不再为它挂任何买单。**

## 目标

- `place_orders` 增加一道"该 market 已持仓则跳过"的闸，按 `condition_id`（market ID）判断。
- 持仓按钱包（funder）查，逐钱包独立；自动与手动两种下单路径都生效（都走 `place_orders`）。

## 非目标（本次不做）

- 不撤销该市场已经挂着的在挂买单（只管"新挂"入口）。
- 不改监控/止盈/止损逻辑。
- 不改黑名单 / 冷却 / 余额 / 最小份额 / "该 token 已有在挂买单" 等现有闸。

## 设计

### 1. 纯函数（无 IO，单测）

新增 `engine/positions.py`：

```python
def held_condition_ids(positions: list[dict]) -> set[str]:
    """返回当前持有仓位(size>0)的 market(condition_id)集合。

    positions 为 Polymarket Data API /positions 的返回(每项含 conditionId / size)。
    YES/NO 任一方向有仓都算该 market 已持仓;缺 conditionId 或 size<=0 的项忽略。
    """
```

实现要点：遍历 positions，取 `float(p.get("size", 0) or 0) > 0` 且 `p.get("conditionId")` 非空的项，收集其 `conditionId` 成 `set`。

### 2. 接入 `place_orders`（`engine/manager.py`）

- 在方法顶部读 `open_orders` 之后，紧接着读一次持仓并构造已持仓集合：

```python
try:
    positions = self.api.get_user_positions(self.api.get_funder())
except Exception as e:
    logger.error(
        "get_user_positions failed for %s, skip placement: %s",
        self.wallet_address, e,
    )
    return
held = held_condition_ids(positions)
```

  这是**失败兜底=该轮不下单**（保守）：取不到持仓就无法判断哪些市场该跳，直接 `return`，下一轮再试。

- 在逐市场循环里，紧跟黑名单判断之后加一道：

```python
if market["market_id"] in held:
    continue
```

- 其余判断（黑名单 / 冷却 / `token_id in open_buy_assets` / 余额 / 最小份额）保持不变、顺序不变。

### 3. 关键边界

- **market（condition_id）级别**：该市场任一方向（YES 或 NO）有持仓即跳过整个市场的买单。`eligible_markets` 每项的 `market_id` 即 condition_id，与持仓的 `conditionId` 同口径。
- **只跳"新挂"，不撤"已挂"**：已挂在该市场的在挂买单不动。
- **每钱包独立**：持仓按 funder 查，`place_orders` 每钱包各跑一次。

### 4. 失败兜底的小副作用

持仓读取放在方法早期（`open_orders` 之后），失败即 `return`。这意味着持仓接口报错那一轮，连"超每钱包上限撤多余买单"的清理也会一起跳过。下一轮成功取到持仓时照常进行——可接受（与 Data API 偶发抖动同级的容错）。

## 错误处理

- `get_user_positions` 抛异常 → 记 error 日志并 `return`，该轮不挂任何单。
- `held_condition_ids` 纯函数对缺字段/异常值做 None 安全处理（`p.get(...)`、`float(... or 0)`），不抛异常。

## 测试计划（TDD）

- `tests/test_positions.py`（新）：`held_condition_ids`
  - size>0 计入；size 0 / 负数 排除；缺 conditionId 排除；空列表 → 空集；同一 condition 的 YES+NO 两项合成一个 id；size 为字符串/None 的 None 安全。
- `tests/test_place_orders.py`（已有）：
  - 持仓市场不挂、非持仓的合格市场照常挂（mock `get_user_positions` 返回某 market 的持仓，断言 `place_limit_buy` 不对该 market 调用、对另一合格 market 调用）。
  - `get_user_positions` 抛错 → 整轮不挂任何单（断言 `place_limit_buy` 未被调用）。

## 守住的既有行为（不改）

- 黑名单 / 冷却 / 余额 / 最小份额 / "该 token 已有在挂买单" 各闸不变。
- 自动 `_do_scan→place_orders` 与手动 `place_all_orders→place_orders`、`test_place_orders` 同样生效（共用 `place_orders`）。
- 监控、止盈、止损不变。
