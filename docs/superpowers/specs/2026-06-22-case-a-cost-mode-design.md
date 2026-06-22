# Case A 新增「按成本价挂单」离场模式 设计 / spec

> 日期：2026-06-22
> 状态：待用户评审
> 背景见 [[take-profit-position-driven]]、SP3 三段式离场 `2026-06-16-sp3-three-tier-exit-design.md`。

## 零、背景

SP3 三段式离场里，Case A（`cost ≤ best_bid`，处于保本/盈利区）现有两种 `case_a_mode`：

- `ask`（默认）：挂 maker 卖单在卖一 `best_ask`，吃满价差，但可能不成交；
- `market`：贴买一市价（FAK）立即清掉。

用户要再加第三种：把卖单挂在**成本价**。

## 一、关键不变量

Case A 的前提是 `cost ≤ best_bid`，而盘口价格总是对齐 tick 的，所以 `best_bid` 落在某个 tick 上，于是：

> `ceil_to_tick(cost) ≤ best_bid`

即一笔挂在 `ceil_to_tick(cost)` 的限价卖单**一定 marketable**（≤ 买一），会立即吃买盘、以 ≥ 成本的买一价成交（价格改善）；同时限价下限 = `ceil_to_tick(cost) ≥ cost`，**绝不卖穿成本**。能立即成交的部分按买一成交，若有剩余则停在成本价当保本单等买盘回补。这正是用户选定的行为（GTC 限价单挂成本价）。

## 二、改动

**1. `engine/take_profit.py` `plan_exit`（核心，纯函数）**：Case A 分支扩成三选一，新增 `case_a_mode == "cost"`：

```python
if cost <= best_bid:
    if case_a_mode == "market":
        return {"tier": "A", "action": "market", "price": None, "size": size}
    if case_a_mode == "cost":
        return {"tier": "A", "action": "rest", "price": ceil_to_tick(cost, tick), "size": size}
    return {"tier": "A", "action": "rest", "price": _rest_price(), "size": size}  # ask
```

复用既有 `rest` 动作；执行链（`_exit_position` 的 `action == "rest"` → `plan_take_profit` 维护恰好一笔 GTC `place_limit_sell`）**零改动**。marketable 限价 GTC 由 CLOB 立即撮合、剩余挂住。

**2. `web/templates/config.html`**：`case_a_mode` 下拉新增第三项

```html
<option value="cost">按成本价（保本挂单：立即吃买盘，剩余停在成本）</option>
```

**3. 文档**：`README.md` 的 `case_a_mode` 行 + `CLAUDE.md` 的 Tier A 描述各补一句新模式。

## 三、测试（`tests/test_exit_plan.py`）

- `test_case_a_cost_mode`：`cost=0.30, bid=0.31, ask=0.33, mode="cost"` → `tier="A", action="rest", price=0.30`（挂成本价，非卖一 0.33）。
- `ceil_to_tick` 生效：`cost=0.305, bid=0.31, ask=0.33, mode="cost"` → `price=0.31`，并验证 `price ≤ bid`。

## 四、范围之外

- 不动 underwater 三段（B0 / B_sweep / B_park）与 `ask` / `market` 既有行为；
- 默认值保持 `ask`（老用户行为不变）；
- 不引入新配置键；`case_a_mode` 在 settings 路由 / DB 无取值白名单校验，`"cost"` 直接 round-trip（实现时复核确认）。
