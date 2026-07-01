# 可配置区间变量 + 风险系数 设计

日期:2026-07-01

## 目标

用户提出一套做市策略,要求**通过配置参数复现,而不在代码里写死**。核心是给多档做市加两个通用旋钮:

1. **奖励最低份额范围筛选**(`rewards_min_size` 下限/上限)。
2. **每档区间匹配变量可选**:`累计厚度`(现状)或 `风险系数`(新增),配一张可编辑的**金额数值表**。

设计原则:一切皆旋钮,讨论中的具体策略只是「一组配置值」,系统里零处写死具体数字。

## 待复现的目标策略(= 一组配置)

| 策略点 | 对应旋钮 | 填的值 |
|---|---|---|
| 奖励 ≥ $30 | `min_reward_usd` | 30 |
| 结算 ≥ 1 天 | `min_settlement_days` | 1 |
| 只做最低份额正好=20 | `rewards_min_size_min` / `_max` | 20 / 20 |
| 挂单最低份额 | tier_rules 动作 `min_size` | — |
| 档位选择 | tier_rules 每档区间表 | 按需 |
| 风险系数按档分区间设策略 | `tier_match_var=风险系数` + `amount_value_table` + tier_rules | 见下 |

风险系数模式下,`amount_value_table` 填 `10-20¢→1 / 20-25¢→1.5 / 25-30¢→2`,各档 tier_rules 设「风险系数 ≥ 阈值 → 挂 min_size,其余 → 不挂」。

## 非目标(YAGNI 线)

- 不做「无限通用的策略脚本引擎」。只加下述几个旋钮。
- `tier_match_var` 与 `amount_value_table` 都是**每模板一份**,不做每档一份。
- 不改 `per_share_reward_thresholds`(单份奖励门槛,另一独立旗)、不改离场逻辑。

## 风险系数定义

对某一档(某个买价档位):

```
风险系数 = 该档盘口挂单量 / (rewards_min_size × 金额数值(该档价))
         = 本档厚度 / 金额数值(该档价)
```

- **「该档盘口挂单量」= 该买价档位上盘口已有的挂单深度**(`build_ladder` 读的 `level.size`),不是我们要挂的量。用于逐档筛选/定策略。
- 「本档厚度」= `level.size / rewards_min_size`(与现有 thickness 同义,但**逐档、非累计**)。
- 「金额数值(价)」= 查 `amount_value_table` 得到的价格加权系数;**查不到(价超表)→ 该档不挂**。

### 金额数值表

存储:升序 `upper` 阈值列表,例:

```json
[{"upper": 0.20, "value": 1}, {"upper": 0.25, "value": 1.5}, {"upper": 0.30, "value": 2}]
```

查表 `amount_value(price, table)`:返回第一个满足 `price ≤ upper` 的 `value`;都不满足(`price` 大于最大 `upper`)→ `None`。低端下界由价格区间旗 `min_price_cents` 兜(奖励单都在价格区间内下,故 `price < 0.10` 不会出现);`None` 即该档不挂。

（存储用小数价 0.20 而非美分 20,与引擎内部价格单位一致;配置页展示用美分,前端转换。）

## 配置层改动(`config.py` `TEMPLATE_DEFAULTS`)

新增键(存 `template_settings`,`get_template` 自动合并默认值、`routes.py` 保存白名单自动收):

```python
"rewards_min_size_min": 1,      # 奖励最低份额下限筛选
"rewards_min_size_max": 250,    # 上限(硬顶 250,取档所需)
"tier_match_var": "cumulative_thickness",   # "cumulative_thickness" | "risk_coefficient"
"amount_value_table": [         # 仅风险系数模式使用;默认给出三档便于开箱
    {"upper": 0.20, "value": 1},
    {"upper": 0.25, "value": 1.5},
    {"upper": 0.30, "value": 2},
],
```

- 默认 `tier_match_var=累计厚度` → 金额表休眠,**现有行为完全不变**。
- `template_settings` 是 key/value JSON 表,新键无需 schema 迁移。

## 引擎改动

### `engine/scanner.py`

把写死的 `if not (0 < min_size <= 250): continue` 改为按旗筛选:

```python
lo = int(template.get("rewards_min_size_min", 1))
hi = int(template.get("rewards_min_size_max", 250))
if not (max(1, lo) <= min_size <= min(250, hi)):
    continue
```

`per_share_reward_thresholds`、`reward_bracket` 逻辑不动。

### `engine/laddering.py`

**匹配变量抽象**。`build_ladder` 增参 `tier_match_var`、`amount_value_table`,每档算出「匹配值」`match_value`:

- `累计厚度`:`match_value = 累计厚度`(现状);合格档 = 在奖励区间内 **且 thickness ≥ 1**(现状不变)。
- `风险系数`:`match_value = (level.size / min_size) / amount_value(price)`;合格档 = 在奖励区间内 **且 `amount_value(price)` 有值**(价在表内)。厚度门槛交由 tier_rules 区间表兜(薄档 → 风险系数低 → 命中「其余→不挂」)。

`build_ladder` 返回 `[{price, match_value}, ...]`(替代原 `cumulative_thickness` 字段名,统一叫 `match_value`)。

`resolve_tier_share` 首参由 `cumulative_thickness` 更名为 `match_value`(语义泛化,`_interval_action(tier_rule, match_value)` 不变)。`compute_market_ladders` / `preview_market_ladders` / `_verbose_levels` 透传 `tier_match_var`、`amount_value_table`。tier 分配、预算/敞口封顶、双边地板等逻辑不变。

预演(`_verbose_levels`)输出**保留现有字段**(`thickness` / `cumulative_thickness` 前端在用,不改名),风险系数模式下**增列** `match_value`(风险系数值)供展示,并把 `skip_reason` 补上「价超金额表」一档。

## 前端改动(`web/templates/config.html`)

- tier_rules 编辑器上方加「区间变量」下拉(累计厚度 / 风险系数);标签随选择变(「累计厚度阈值」/「风险系数阈值」)。
- 选风险系数时显示**金额数值表编辑器**(价格档[美分]→数值,可增删行;提交转小数价)。
- 加 `rewards_min_size_min` / `_max` 两个数字输入。
- 保存:number 输入沿用现有收集;下拉、金额表单独收(参照 `stop_loss_mode`、`tier_rules` 的单独收集)。
- 含中文,由主 agent 直接 Write,写后 `node --check` + 查中文别字/BOM。

## 测试

- `tests/test_laddering.py`:
  - 累计厚度模式回归(现状不变)。
  - 风险系数:`match_value` 计算、金额表查表命中/超范围→不挂、逐档区间命中动作。
  - `amount_value(price, table)` 边界(端点 `price == upper`、超上界、空表)。
- `tests/test_scanner.py`:`rewards_min_size` 范围筛选(正好=20、区间、默认放行)。
- 契约测试:`TEMPLATE_DEFAULTS` 含四个新键;`/api/templates` 保存/读取新键往返。

## 复现目标策略的配置示例

建模板填:`min_reward_usd=30`、`min_settlement_days=1`、`rewards_min_size_min=rewards_min_size_max=20`、`tier_match_var=risk_coefficient`、`amount_value_table=[{0.20,1},{0.25,1.5},{0.30,2}]`、各档 tier_rules = `[{upper:阈值, action:min_size}, {upper:null, action:skip}]`。系统零处写死。

## 待确认假设

- 「该档盘口挂单量」= 盘口该价位的已有挂单深度(`level.size`)。若指别的,风险系数计算处需改。
