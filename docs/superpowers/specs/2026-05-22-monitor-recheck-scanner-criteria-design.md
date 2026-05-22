# 已挂买单复查 Market Scanner 条件 — 设计

日期:2026-05-22
状态:已与用户确认设计,待审阅本 spec

## 背景与问题

监控环节(`engine/monitor.py` 的 Step 3 = `check_sell_orders → _check_compliance`)目前对每个 resting BUY 单**只复查 Order Strategy**:用实时盘口 + 实时 `rewards_max_spread` 重跑 `determine_order_price`,经 `needs_replace` 判定 keep / replace / cancel(`engine/strategy_check.py`)。

它**不复查** Market Scanner 的初筛条件。也就是说,一个市场在挂单之后即便退化(奖励掉到阈值下、临近结算、价格跑出区间、盘口价差变宽),只要奖励区间里还存在一个合规挂单价,旧买单就会一直挂着。`place_orders` 也只负责挂新单,从不为"市场已不在 eligible 列表"撤旧单。Scanner 的初筛只在扫描时生效,且只影响"新单挂到哪些市场"。

需求:让 monitor 对每个已挂买单也复查 Market Scanner 的初筛条件,与最开始扫描时的条件保持一致。

## 需求(已与用户确认)

1. **复查维度**:四项全查,与 scanner 一致——奖励金额、结算天数、价格区间、买一卖一价差。叠加在 Step 3 已有的"挂单价合规"之上。
2. **确认不合格的动作**:**只撤该 resting BUY 单**。不进冷却、不平已有持仓。
3. **数据拉不到时**:**保守保留,本轮跳过**(按维度,逐项判断)。
4. **作用范围**:仅完全未成交的 resting BUY 单。

## 方案

采用「方案 A」:把四个 scanner 闸门并入 Step 3 的 `_check_compliance`,复用它已经做的盘口与 rewards 取数,闸门逻辑抽成纯函数便于单测。

(放弃方案 B「独立步骤」——会重复取盘口/奖励,每 tick 每单多一轮网络往返;放弃方案 C「用扫描结果做差集撤单」——空/失败扫描会一次性误撤全部买单,与"数据缺失保守保留"冲突,且反应慢、语义偏宽。)

## 组件与改动

### 1. 新增纯函数模块 `engine/eligibility.py`

无 IO,仿 `engine/strategy_check.py` 的风格,完全可单测。

```python
# engine/eligibility.py
"""Pure re-check of scanner eligibility for a resting buy (no network/IO)."""
from typing import Optional, Tuple


def recheck_resting_buy(
    reward_total: Optional[float],   # sum rate_per_day; None = 未知
    days_left: Optional[float],      # 距结算剩余天数; None = 未知
    best_bid_cents: Optional[float], # best_bid * 100; None = 未知
    spread_cents: Optional[float],   # (best_ask - best_bid) * 100; None = 未知
    settings: dict,
) -> Tuple[bool, Optional[str]]:
    """复查一个 resting BUY 是否仍满足 Market Scanner 初筛。

    返回 (cancel, reason):
      cancel=True  -> 该买单已不合格,应撤单;reason 为中文原因(写入操作记录)
      cancel=False -> 保留;reason 为 None
    任一输入为 None(数据未知)= 跳过该维度(保守保留)。
    检查顺序:奖励金额 -> 结算天数 -> 价格区间 -> 买一卖一价差;命中第一个即返回。
    """
```

闸门语义(必须与 scanner 完全一致):

| 维度 | 撤单条件 | scanner 对应 | settings 键 |
|---|---|---|---|
| 奖励金额 | `reward_total is not None and reward_total < min_reward` | scanner.py:128 | `min_reward_usd` |
| 结算天数 | `days_left is not None and 0 <= days_left < min_days` | scanner.py:92 | `min_settlement_days` |
| 价格区间 | `best_bid_cents is not None and not (min_price_cents <= best_bid_cents <= max_price_cents)` | scanner.py:194 | `min_price_cents` / `max_price_cents` |
| 买一卖一价差 | `spread_cents is not None and spread_cents >= max_spread_cents` | scanner.py:163 | `max_spread_cents` |

注意结算天数与 scanner 一致:`days_left` 为负(无结算日 / 已过)**不撤**(scanner 只排除 `0 <= days_left < min_days` 这个窗口)。

reason 文案(示例):
- 奖励:`"市场日奖励 $X 跌破阈值 $Y,撤买单"`
- 结算:`"距结算仅 X 天 < 阈值 Y 天,撤买单避结算风险"`
- 价格:`"最优买价 Xc 跑出区间 [Yc, Zc],撤买单"`
- 价差:`"买一卖一价差 Xc ≥ 阈值 Yc,撤买单"`

### 2. `api/polymarket_api.py` 新增 `get_market_end_ts(condition_id)`

```python
def get_market_end_ts(self, condition_id: str) -> float | None:
    """市场结算时间(Unix 秒);取不到返回 None(降级为结算维度未知)。

    用 CLOB get_market(condition_id) 取 end_date_iso。
    """
```

- 实现优先用 CLOB `self.client.get_market(condition_id)` 读 `end_date_iso`(ISO 字符串)解析为 Unix 秒。
- 解析复用/对齐 scanner 的 `_parse_end_date` 思路。
- 任何异常 / 字段缺失 → 返回 `None`(`_check_compliance` 把 None 当作"结算维度未知 → 跳过",不阻断其它维度)。
- **实现期需手测确认** `get_market` 的可用性与字段名(`end_date_iso` vs `end_date`)。确认不可用时按"返回 None 降级"处理,功能不被阻断(仅结算维度失效)。

### 3. `engine/monitor.py` 改动

(a) 把 `_market_max_spread(condition_id)`(只返回 max_spread)升级为 `_market_rewards_info(condition_id)`,**始终返回一个 dict**,各字段独立、未知时为 `None`:

```python
def _market_rewards_info(self, condition_id: str) -> dict:
    """{"max_spread": int|None, "reward_total": float|None, "end_ts": float|None}.
    各字段独立取数、独立 TTL 缓存;某字段取不到则为 None(由上层当作该维度未知)。"""
```

- `reward_total` = 对 `get_rewards_for_market(condition_id)` 返回的每个 item 的 `rewards_config` 求和 `rate_per_day`(与 scanner.py:121-128 一致)。
- `max_spread` 复用 `extract_max_spread`(`engine/rewards.py`)。`max_spread` 与 `reward_total` 同源于一次 `get_rewards_for_market`,共用一个 rewards 缓存项。
- `end_ts` 来自 `get_market_end_ts(condition_id)`,**独立**缓存。
- **缓存策略(沿用现有 `_market_max_spread` 的做法)**:两个缓存(rewards 项、end_ts 项)各自 TTL=`rewards_cache_ttl_sec`;**仅在该次取数成功时写缓存**;失败时返回 `None` 且**不缓存 None**,以便下一轮重试(避免一次接口抖动导致整个 TTL 窗口内停查)。
- 始终返回 dict(从不返回 None),所以即便 rewards 失败,`end_ts` 仍可独立返回,反之亦然——契合"逐维度保守保留"。

(b) 在 `_check_compliance` 价格合规判定**之前**插入复查:

数据流:
1. `get_orderbook` → best_bid / best_ask / midpoint / tick(已有)。算 `best_bid_cents = best_bid*100`、`spread_cents = (best_ask-best_bid)*100`。
2. `info = self._market_rewards_info(cid)`;由此得 `reward_total`、`max_spread`、`end_ts`;`days_left = (end_ts - now)/86400 if end_ts else None`。
3. `cancel, reason = recheck_resting_buy(reward_total, days_left, best_bid_cents, spread_cents, settings)`。
   - **cancel=True** → `self.api.cancel_orders([o["id"]])`;`_record_action(action_type="eligibility_cancel", side="-", price=-1, size=osize, reason=reason, price_basis=...)`;`_status_add(stage="Step3", action=f"复查撤单:{reason}")`;`return`(不再跑价格合规)。
   - **cancel=False** → 继续现有 `needs_replace` 价格合规,复用 `info["max_spread"]`(若为 None,价格合规照旧因无 max_spread 而跳过,保留——与今天一致)。

注意盘口为空(无 bids/asks)时:`best_bid_cents`/`spread_cents` 视为 None(该维度跳过),且与今天一样跳过价格合规。

(c) 范围保证:复查处于 `_check_compliance` 内,沿用 `check_sell_orders` 已有的前置过滤——`side != "BUY"` 的(止盈 SELL)和 `size_matched > 0` 的(部分成交)都不会进入 `_check_compliance`,因此不会被复查撤单。

### 4. 操作记录

新增 `action_type="eligibility_cancel"`:`side="-"`、`price=-1`、`size=osize`、`reason`=失败维度中文原因、`price_basis` 注明来源(如 `"奖励/结算/盘口复查;来源:CLOB get_orderbook + get_rewards_for_market + get_market"`)。复用现有 `_record_action`(不抛异常)。

## 错误处理

- 各维度取数失败 → 对应输入为 `None` → 跳过该维度(保留)。
- `cancel_orders` 失败 → 记 warning、保留订单,下轮幂等重试(同现有 Step 3)。
- `_market_rewards_info` 某字段为 None(rewards 或 get_market 取数失败)→ 对应维度(奖励 / 结算)跳过;价格/价差仍可用盘口判定。
- 全程不抛出到 `check_sell_orders` 之外(已有 try/except 包裹)。

## 测试计划

### 纯函数 `tests/test_eligibility.py`(新增,TDD)
- 奖励:`reward_total < min_reward` → (True, 含"奖励"原因);`>=` → (False, None);`None` → (False, None)。
- 结算:`0 <= days_left < min_days` → True;`days_left >= min_days` → False;`days_left < 0` → False;`None` → False。
- 价格:`best_bid_cents` 出界(高/低两侧)→ True;界内 → False;`None` → False。
- 价差:`spread_cents >= max_spread_cents` → True;`<` → False;`None` → False。
- 组合:多个维度同时不合格,按"奖励→结算→价格→价差"顺序返回第一个原因。
- 全过 → (False, None)。

### 监控集成 `tests/test_monitor.py`(新增用例)
- 奖励不足 → `cancel_orders([id])` 调用 + 记 `eligibility_cancel` 动作 + 不跑 `place_limit_buy`(价格合规未执行)。
- 临近结算(0<=days<min)→ 撤单。
- best_bid 出界 → 撤单。
- 价差超阈 → 撤单。
- 数据全缺(rewards 取不到、盘口为空)→ 不撤,且不误判(保留)。
- 全合格 → 不触发 `eligibility_cancel`,进入既有价格合规(`needs_replace` 路径不变)。
- SELL 单 / 部分成交单 → 不进入 `_check_compliance`,不被撤(沿用既有前置过滤)。
- 缓存:同一 condition_id 一个 TTL 窗口内只取一次 rewards/get_market(校验 `_market_rewards_info` 缓存命中)。

### 新 api 方法
- `get_market_end_ts`:轻量单测(mock client.get_market 返回 end_date_iso → 期望 ts;异常/缺字段 → None)。实现期手测确认真实字段名。

## 不在本次范围

- 不改 `place_orders` 端的挂单逻辑(scanner 初筛在挂单前已通过)。
- 不改止盈(Step 1b)/止损(Step 2)/成交检测(Step 1)。
- 不引入冷却联动、不平已有持仓。
- 不做"撤了又挂"的额外去抖(`place_orders` 只在扫描时用刷新后的 eligible 列表,churn 受限,用户已确认接受)。

## 兼容性 / 风险

- 新增网络调用:每个 resting BUY 每 tick 多一个 `get_market`(TTL 缓存后按市场去重);rewards 调用本就存在(复用)。
- `get_market` 字段不确定性:已设计为"取不到 → None → 跳过结算维度",不会因此误撤或报错。
- 行为变化:此前不会被撤的退化市场买单,现在会被主动撤掉——符合需求。
