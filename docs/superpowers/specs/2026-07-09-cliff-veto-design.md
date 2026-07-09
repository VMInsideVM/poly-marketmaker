# 奖励区间下方空档否决(悬崖 → 该侧不挂)

日期:2026-07-09
状态:设计定稿,待实现

## 背景

现状:`explain_gap_single_order`(`engine/laddering.py`)的断层分级**只看奖励区间内**的买档(`in_range` = `[reward_range_min, reward_range_max]`)。奖励区间**正下方**的悬崖(如某 NBA 市场买档 19¢/18¢ 之下直接砸到 2¢)被完全忽略——这类市场奖励区间内支撑极薄、下方是真空,一旦薄档被吃穿价格瞬间坍塌,买单深度套牢。

需求:把「支撑检测」向奖励区间下沿再往下延伸 N¢;若那段没有支撑(空档),判为悬崖,**该 token 侧不挂**。

## 判定口径(用户已定)

- **空档判定(1 参 N):** 奖励区间下沿 `reward_range_min` 往下 N¢ 的带 `[reward_range_min − N·0.01, reward_range_min)` 内,若**没有任何买档** → 悬崖 → 该侧 `skip`。用**原始 `bids`**(这些档在 `in_range` 之下,不在 `in_range` 里)。
- **范围:** 每 token 侧独立(gap_single 本就 per-token);YES 侧悬崖只跳 YES,NO 侧照判。
- 只看价格(有没有档),不看深度——深度已由现有风险系数门槛管。

## 两个「默认值」(关键区别)

| | 值 | 作用 |
|---|---|---|
| **配置默认** `TEMPLATE_DEFAULTS["cliff_probe_cents"]` | **2** | 产品默认:所有模板(含用户/朋友的号)ship 后默认启用 N=2¢ 悬崖否决 |
| **纯函数签名默认** `cliff_probe_cents=0` | **0** | 仅为让大量用**位置参数**调 `explain/plan/compute/preview` 的现有单测原样绿(它们测归级/选档子逻辑,不该被悬崖误伤);真实模板走 merge 拿到 2 |

这**是一次有意的行为改变**(非零回归):启用后,奖励区间正下方 2¢ 内无支撑的市场,该侧不再挂单。`N=0` 关闭该检查。

## 方案

### 核心判定(`engine/laddering.py` `explain_gap_single_order`)

签名末尾追加 `cliff_probe_cents=0`。在 `in_range` 确认**非空之后**、系数计算/归级**之前**插入:

```python
if cliff_probe_cents and cliff_probe_cents > 0:
    lo = reward_range_min - cliff_probe_cents * 0.01
    if not any(lo <= float(b["price"]) < reward_range_min for b in bids):
        d["skip_reason"] = (
            f"奖励区间下方 {cliff_probe_cents:g}¢ 内无买档支撑(悬崖)→ 不挂"
        )
        return d
```

`d` 保持 init 态(`action="skip"`、`rule=None`、`levels=[]`),仅置 `skip_reason`。`N=0` → 整段跳过 → 逐字节等价现状。

### 透传链(全部新参数追加末尾、默认 0)

- `plan_gap_single_order(..., cliff_probe_cents=0)` → 传给 `explain_gap_single_order`
- `compute_market_single_orders(..., cliff_probe_cents=0)` → 传给 `plan_gap_single_order`
- `preview_gap_single_market(..., cliff_probe_cents=0)` → 传给 `explain_gap_single_order`

### 调用点(读模板传入)

- `engine/manager.py` place_orders:近 `rule3_min_coeff` 读取处加 `cliff_probe_cents = float(tmpl.get("cliff_probe_cents", 2))`;传给 `compute_market_single_orders`(~295)与 `explain_gap_single_order`(~310)两处调用(各追加末位实参)。
- `web/routes.py` ladder 预览路由(~1077):`preview_gap_single_market(...)` 末位追加 `float(tmpl.get("cliff_probe_cents", 2))`。

### 配置

- `config.py` `TEMPLATE_DEFAULTS` 加 `"cliff_probe_cents": 2`。
- `web/templates/config.html` 策略表单(「挂单参数」区)加数字输入框 `name="cliff_probe_cents"`,走现有 `/api/templates/<id>` 通用序列化(策略表单 submit 收所有 `input[type=number][name]`)。含中文,由**主 agent** 直接改、校验无 BOM/中文。

### 展示串

`gap_single_price_basis(d, ...)` 对 `rule=None`+`skip_reason` 的跳过要能渲染(不崩、显悬崖原因)。实现时核对其 `rule=None`/空 `levels` 分支;不足则加一小段兜底(渲染 `d["skip_reason"]`)。「原因」列 `gap_single_reason` 同理。

## 测试

### 纯函数(`tests/test_gap_single.py`,显式传 `cliff_probe_cents`)

1. **悬崖跳过:** `in_range` 有档(如 19¢/18¢)、`[floor−2¢, floor)` 内无档(下方直接 2¢)→ `cliff_probe_cents=2` → `action=="skip"`、`skip_reason` 含「悬崖」。
2. **有支撑照常:** 同上但 `[floor−2¢, floor)` 内有档(如 floor 下 1¢ 有买档)→ `cliff_probe_cents=2` → 照常归级选档(`action=="place"`,价/量与关闭时一致)。
3. **N=0 不检查:** 下方真空但 `cliff_probe_cents=0` → 照常挂(证明关闭=零回归)。
4. **区间内无档时不误判:** `in_range` 空 → 仍走原「奖励区间内无买档」跳过(悬崖检查在其后,不改该路径)。
5. **透传:** `plan_gap_single_order`/`compute_market_single_orders`/`preview_gap_single_market` 传 `cliff_probe_cents=2` 时对悬崖盘口跳过(证明链路通)。

### 集成(`tests/test_place_orders.py`)

6. `_make_worker` 的共享 `tmpl` 加 `"cliff_probe_cents": 0`(**一行**),保现有集成测试悬崖关闭、原样绿。
7. 新增一例:`_make_worker(template={"cliff_probe_cents": 2})` + 悬崖盘口 → place_orders 不挂该侧。

### 配置契约

8. `test_database.py` 现有 TEMPLATE_DEFAULTS 断言是**逐键**(非精确集),加键不破;补一条 `TEMPLATE_DEFAULTS["cliff_probe_cents"] == 2`。

## 不做

- 不动 rule1/2/3 归级、系数选档、rule1 高位和门槛——悬崖是它们**之前**的独立否决。
- 不做深度版悬崖(有档但薄也算)——YAGNI,按用户选的空档口径。
- 不改 `in_range`(区间内)断层的任何计算。
- N 从 `reward_range_min`(下沿)量,不从「最低 in_range 买档」量(按用户原话「下沿往下」)。

## 权衡

- 配置默认 2 = 行为改变:启用后部分薄市场该侧不再挂,候选下单量下降——这正是用户要的风险控制。想关设 N=0。
- 纯函数默认 0 与配置默认 2 不一致,是刻意的:库层保守(不破坏无参调用/测试),应用层开(产品默认)。spec/代码注释写明。

## 验收

- 全部单测绿(现有 + 新增)。
- 悬崖复现用例:`N=2` 红→(修后)按预期跳过;`N=0` 照常挂。
- 人工:配置页可编辑「悬崖探测(¢)」;设 2 后对区间正下方空档的市场该侧不挂,历史/预演「原因」显示悬崖。
