# 断层单档「判定不挂」的详细价格依据

日期：2026-07-05
状态：设计已确认，待写实现计划

## 背景

`placement_mode=gap_single` 的钱包，每轮下单对判成「不挂」的市场会记一条 `gap_skip` 动作到历史（`engine/manager.py` `_maybe_record_gap_skip`）。历史页两列里：

- **原因** 列已经是结论式的 `skip_reason`，例如 `规则1(宽断层,最大断层12¢):高位系数和 1.25 < 门槛 20 → 整市场不挂`、`规则3(最大断层3¢):无档系数 > 门槛 0 → 不挂`、`奖励区间内无买档`。
- **价格依据/来源** 列却是一句写死的通用话：`断层单档判定不挂（Yes）；奖励区间[0.1200,0.3000]；来源：CLOB get_orderbook`——完全没有把驱动「不挂」的**逐档盘口证据**（每档 价×量→系数、断层劈分点、高位系数和的加数、门槛比较）写出来。

而这些证据全都已在 `explain_gap_single_order` 返回的决策 dict 里（`levels` 逐档 `price/size/coeff/high_side`、`max_gap`、`high_sum`、`min_coeff`、`gate_passed`、`rule`）。挂单成功时 `gap_single_price_basis` 会展开选中档，但**跳过时这份数据被丢弃、只回退到通用串**。

用户要求：历史里「判定不挂」的价格依据/来源更详细，选定**完整逐档证据**形态——不点开代码就能看清"这个市场为什么没挂"。

## 范围

**做**：让 `gap_single_price_basis` 的跳过分支展开逐档证据；`_maybe_record_gap_skip` 改调它、不再手写通用串；对应测试。

**不做**：不动挂单成功（`action=="place"`）的价格依据；不动「原因」列（`gap_single_reason`/`skip_reason` 保持结论式）；不动 `gap_skip` 的按 token 去重口径；不改前端 `history.html`（它只是 `escapeHtml(a.price_basis)` 原样渲染、自动换行）；不为「区间内无买档」去捞最接近的区间外买档价（决策 dict 未携带，需另改 `explain_gap_single_order` 加字段，属另一件事）。

## 设计

### 1. `engine/laddering.py` — `gap_single_price_basis(d, reward_range_min, reward_range_max)` 跳过分支

签名不变。挂单分支（`action=="place"`）逐字节不动。改写**跳过分支**（当前只返回通用 `src`），按决策 dict 里已有数据分两形态：

**A. 区间内有买档但不挂**（`d["levels"]` 非空）：

1. 逐档枚举（价降序，即 `levels` 原序）：`{price:.4f}×{size:g}→系数{coeff:g}`，高位档（`high_side==True`）追加 `[高位]`，档间用 ` · ` 连接。**不截断**（10–31¢ 奖励带内区间档通常寥寥几档；"no silent caps"）。
2. 断层分级：`最大断层 {max_gap:g}¢ → {规则标签}`（复用 `_GAP_RULE_LABEL`）。
3. 具体闸门结果，按跳过子类：
   - 规则1 高位系数和不足（`rule==1 and not gate_passed`）：`高位系数和 {加数们相加}={high_sum:g} < 门槛{gap_high_coeff_sum_min}`——**注意**：`gap_high_coeff_sum_min` 门槛值不在决策 dict 里，需从 `d` 取或改由调用侧传。取舍见下「门槛值来源」。
   - 顺延无档过门槛（`gate_passed==True` 但 `action=="skip"`）：`各档系数均 ≤ 选档门槛{min_coeff:g}`；规则1 这支额外前置一句 `高位系数和{high_sum:g}(过闸)`。
4. 公共尾巴：`系数=挂量÷(最低份数×金额数值);奖励区间[{rmin:.4f},{rmax:.4f}];来源:CLOB get_orderbook`。

**B. 区间内无买档**（`d["levels"]` 为空——含「无买单簿/最低份数≤0」与「全在区间外」两类）：
退化成简洁版 `奖励区间[{rmin:.4f},{rmax:.4f}]内无可评估买档;来源:CLOB get_orderbook`。无档可枚举，两类的区别已在「原因」列，不在 basis 里重复。

**门槛值来源（已定）**：basis 要展示两个门槛——高位系数和门槛（`gap_high_coeff_sum_min`）与选档门槛（`min_coeff`）。`min_coeff` 已在决策 dict（`d["min_coeff"]`）；`gap_high_coeff_sum_min` **不在**。定为：**在 `explain_gap_single_order` 的决策 dict 里补记 `gate_min`（=`gap_high_coeff_sum_min`）**，仅规则1 有值、其余 `None`。理由：改动最小、`gap_single_price_basis` 保持只吃 `d`、规则1 决策自解释、basis 与「原因」列门槛数值一致互印。新增字段不改任何返回值，`plan_gap_single_order` 薄壳与既有测试不受影响。

示例（`min_size=20`；金额数值 ≤0.20→1 / ≤0.25→1.5 / ≤0.31→2；奖励区间[0.12,0.30]；规则1 门槛20）：

买档降序 `0.28×30 / 0.27×20 / 0.15×400`，`max_gap=12¢>10¢→规则1`，高位=｛0.28,0.27｝，`high_sum=0.75+0.50=1.25<20`：

```
区间内买档(价降序):0.2800×30→系数0.75[高位] · 0.2700×20→系数0.50[高位] · 0.1500×400→系数20;最大断层 12¢→规则1(宽断层);高位系数和 0.75+0.50=1.25 < 门槛20 → 整市场不挂;系数=挂量÷(最低份数×金额数值);奖励区间[0.1200,0.3000];来源:CLOB get_orderbook
```

### 2. `engine/manager.py` — `_maybe_record_gap_skip`

把手写的那段通用 `price_basis`（`断层单档判定不挂（outcome）；奖励区间…；来源…`）换成：

```python
price_basis=gap_single_price_basis(
    decision, side["reward_range_min"], side["reward_range_max"]
)
```

`gap_single_price_basis` 已在文件顶部 import（现供 place_buy 用）。`reason`（仍是 `skip_reason`）、按 token 去重（`_last_gap_skip`，判断变化才记）、`price=-1`/`size=0`、`side` 取 `outcome` 都**不变**。

**去重口径不变**：同一"不挂类别"（`skip_reason`）只记一条，展示的逐档证据是**首次命中该类别时**的盘口快照。这是刻意的——每轮盘口微动都刷新证据会回到历史洪水问题（`_maybe_record_gap_skip` 去重的初衷）。快照语义诚实（"此刻起、因这套盘口不挂"）。

### 3. `tests/test_gap_single.py`

- 现有 `test_price_basis_skip_is_minimal`（买档 `0.05/0.04` 全在区间外 → `levels` 空）：正名为区间内无买档子类、断言仍是简洁版（含"无可评估买档"、`get_orderbook`、不含"档价/系数"逐档）。
- 新增 `test_price_basis_skip_rule1_shows_per_level_evidence`：构造规则1 高位系数和不足的 `d`，断言 basis 含逐档 `价/量/系数`、`最大断层`、`高位系数和`、`门槛`、`整市场不挂`。
- 新增 `test_price_basis_skip_no_coeff_shows_evidence`：构造顺延无档过门槛的 `d`（如 `x` 设高使无档 `>门槛`），断言 basis 含逐档系数、`各档系数均 ≤`、门槛。
- `gate_min` 字段：加一条断言 `explain_gap_single_order` 规则1 决策带 `gate_min`（=`gap_high_coeff_sum_min`）、非规则1 为 `None`（并确认既有 `plan_gap_single_order` 薄壳与其余 gap_single 测试全绿——新增字段不影响返回值）。
- 若存在 manager 层 `gap_skip` 记账测试断言了 basis 文案，同步更新（实现时核对 `tests/test_place_orders.py` 等）。

## 验收标准

1. `placement_mode=gap_single` 的钱包跑一轮，历史 `gap_skip` 行「价格依据/来源」：区间内有买档的市场展开逐档系数 + 断层分级 + 门槛判定；区间内无买档的市场显示简洁版。
2. 挂单成功行的价格依据与「原因」列文案与现状逐字节一致（回归）。
3. `pytest` 全绿（新增用例通过，既有 gap_single/laddering/manager 测试不回归）。
