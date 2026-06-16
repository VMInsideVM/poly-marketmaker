# SP5a-1：跌出 eligible 整仓撤单（drop-out cancel）设计 / spec

> 日期：2026-06-16
> 状态：待用户评审
> SP5a（三档节奏，观察名单已按 YAGNI 去掉）的第一个子块。父背景见记忆 [[v4-strategy-integration-roadmap]]。

## 零、背景与定位

v4 §6 实时档：「看守已有挂单；一旦挂单条件不再满足，**立即撤销**该市场对应挂单，并重新进入判断」。

**现状缺口**：现有「实时看守」是 monitor Step3 `check_sell_orders`，逐**买单**复查 live 价差 / 单价区间 / 奖励区间（cancel-only）。但它**不复查完整 §3 门槛**——当一个市场的**奖励整个没了**（`get_rewards_for_market` 返回空 → `max_spread=None`）Step3 反而「跳过(取不到max_spread)」不撤；结算太近、min_size 超档、单份奖励跌破阈值、被拉黑等门槛跌出也不在 Step3 覆盖内。于是这些市场我们的买单会**滞留**（不再赚奖励还占着）。

**SP5a-1 范围**：在每轮下单时，把「我们有在挂买单、但已不在该钱包本轮 eligible（`filter_for_template` 结果）」的市场识别为**跌出**，**撤掉该市场全部买单**。持仓不动（仍由 `check_exit` 卖出），卖单不动。

**不在 SP5a-1**：节奏拆分（发现 4h / 下单快）→ SP5a-2；观察名单 / 奖励激活 → 已按 YAGNI 去掉。

## 一、已确认决策

| | 决策 |
| --- | --- |
| 跌出后动作 | **整仓撤买单、仍卖仓**：撤该市场全部 BUY，持仓由 `check_exit` 正常卖出，卖单不动 |
| 冷却市场 | **不算跌出**：冷却（成交后 `cooldown`）只是「暂时不挂新单」，保留该市场旧买单（避免撤掉正赚奖励的旧单、且不与 SP5b「另一侧照常运行」打架）。跌出只针对**因门槛**真正不合格的市场 |
| 触发范围 | `cancel_dropouts` 开关，**默认关**；只在真正下单轮（自动 `_do_scan`、手动 `place_all_orders`）传 `True`；测试挂单按钮（`test_place_orders`）不传（默认关），避免点测试就撤一堆单、并防「扫描失败/空池」误撤 |

## 二、`place_orders` 接线（`engine/manager.py`）

`place_orders` 已有：`open_orders` → `buy_orders` / `markets_with_open`；本轮 `eligible_markets` → `grouped`（按市场分组）。新增一个签名形参 + 一段跌出撤单。

**(a) 签名加开关**：
```python
def place_orders(self, eligible_markets, limit=None, cancel_dropouts=False):
```

**(b) 跌出撤单段**：放在 `grouped, order` 构建之后、`placed = 0` 之前：
```python
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
                o["id"] for o in buy_orders
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
```

要点：
- `dropped` = 有在挂买单、不在 eligible、且不在冷却的市场（condition_id）。
- 一次批量 `cancel_orders(drop_ids)` 撤掉所有跌出市场的 BUY；成功后从 `markets_with_open` 移除（释放并发名额、计数准确），并逐市场记 `dropout_cancel`。
- 只撤 BUY（`drop_ids` 取自 `buy_orders`，已是 `side=="BUY"`）；卖单 / 持仓不碰。
- 撤单失败只告警、不抛（与现有 reconcile/pause 撤单一致）。

**(c) 调用点**（`engine/manager.py`）：
- `_do_scan` 内 `worker.place_orders(eligible)` → `worker.place_orders(eligible, cancel_dropouts=True)`。
- `place_all_orders` 内 `worker.place_orders(eligible)` → `worker.place_orders(eligible, cancel_dropouts=True)`。
- `test_place_orders` 内 `worker.place_orders(sorted_markets, limit=3)` → **不改**（默认 `cancel_dropouts=False`）。

## 三、与既有机制的关系（都不动）

- **SP5b 单侧暂停**：仍 eligible 的持仓市场（held）在 `eligible_mids` 内 → 不算跌出，由 SP5b 处理（暂停侧撤、活跃侧跑）。若某持仓市场**同时跌出**（奖励没了），它不在 eligible_mids → 跌出撤光两侧买单，持仓仍由 `check_exit` 卖出。两者一致不冲突。
- **SP5c 撤改收敛**：eligible 市场的逐档撤改在下单循环里，不变。
- **Step3 实时复查**：逐买单 live 价差/价区/奖励区间，仍是补充安全网，不变。
- **冷却**：仍由 `filter_for_template` 排除（不挂新单），跌出撤单显式跳过冷却市场（保留旧买单）。

## 四、测试（`tests/test_place_orders.py`，复用现有桩）

- **跌出撤单**：钱包在市场 B 有买单，本轮 eligible 只有市场 A，`cancel_dropouts=True` → B 的买单被撤、A 正常多档挂上。
- **冷却不算跌出**：钱包在市场 C 有买单，C 不在 eligible 但 `is_in_cooldown(C)=True` → C 的买单**不**被撤。
- **默认关**：`cancel_dropouts` 默认 `False`（测试挂单路径）→ 即使有不在 eligible 的在挂买单也不撤。
- **只撤 BUY**：跌出市场若有卖单，卖单不进 `drop_ids`（`buy_orders` 只含 BUY）——用「跌出市场有买单 + 全局有卖单，撤的 id 只含买单 id」断言。

## 五、验收 checkpoint

1. 有在挂买单但跌出 eligible（门槛不合格）的市场 → 该市场全部买单被撤、记 `dropout_cancel`。
2. 冷却中的市场不被当跌出（旧买单保留）。
3. `cancel_dropouts` 默认关：测试挂单按钮不会误撤；真正下单轮传 `True` 才撤。
4. 只撤买单；持仓与卖单不动（持仓仍由 `check_exit` 卖出）。
5. eligible 市场的正常多档挂单 / SP5b 暂停 / SP5c 收敛不受影响。
6. `pytest` 全绿。

## 六、范围之外

SP5a-2 节奏拆分（发现 4h / 下单快，需重构 `fetch_candidates` 分离奖励发现与订单簿刷新）· SP6 模板 UI。
