# 单份奖励阈值开关 + 策略概念说明翻新 设计 / spec

> 日期：2026-07-02
> 状态：待用户评审

## 零、背景与目标

三件事，一个 spec：

1. **单份奖励阈值可全局不启用**：现在"单份奖励 < 取档阈值 → 不做该市场"这道筛选永远生效。加一个每模板的全局开关，可整体关掉（关掉后其余筛选照常）。
2. **前端内联小字**：在配置页给"单份奖励阈值""区间变量"两处各加一行小字，解释三个概念（单份奖励怎么算、累计厚度、风险系数）。
3. **翻新使用说明 + README**：`help.html` 有 v3.0.0 后的过时内容（仍写"品类黑名单/排除品类"），一并订正并补齐 v4 参数与三概念说明；README 参数表补新键 + 带例子的三概念简述。

设计原则：只加一个开关，不动 `per_share_reward_thresholds` 的档结构，不动风险系数/离场计算本身。默认开关=启用 → 行为零回归。

## 一、三个概念的权威定义（文档口径统一用这套）

- **单份奖励** = 市场每日 LP 奖励 ÷ 该市场最低下单份数(`rewards_min_size`)。"每一手最低单每天能拿多少奖励"。阈值按最低份数所在档(20/50/100/200/250，向上取档)分别设。
  - 例：日奖励池 $60、最低份数 20 → 单份奖励 = 60/20 = 3.0；该档(20)阈值 0.30 → 3.0 ≥ 0.30 通过。
- **累计厚度** = 从买一往下累加到该档的**厚度**之和；单档厚度 = 盘口该价位挂单量 ÷ 最低份数。
  - 例：最低份数 20；买一 0.30 挂 60 张(厚度 3)、0.29 挂 40 张(厚度 2) → 0.29 档累计厚度 = 3+2 = 5。
- **风险系数** = 本档厚度 ÷ 金额数值(该档价)（逐档、非累计）。金额数值表按价格档配系数(价越高系数越大)；价超表则该档不挂。
  - 例：金额表 20¢→1 / 25¢→1.5 / 30¢→2；某档价 0.25、厚度 3 → 金额数值=1.5 → 风险系数 = 3/1.5 = 2.0。价越高分母越大，同厚度算出的系数越低（越贵的价位要更厚盘口才够格）。

（源：`engine/laddering.py` `build_ladder`/`amount_value`；`engine/scanner.py` `filter_for_template` per_share 段；spec `2026-07-01-configurable-tier-match-and-risk-coefficient-design.md`。）

## 二、功能：单份奖励阈值全局开关

### 2.1 数据模型（`config.py`）
`TEMPLATE_DEFAULTS` 加：
```python
"per_share_reward_enabled": True,   # 单份奖励阈值筛选总开关;False=整段跳过
```
key/value merge 自动持久化，`web/routes.py` 保存白名单(`k in TEMPLATE_DEFAULTS`)自动收，**路由不改**。

### 2.2 引擎（`engine/scanner.py` `filter_for_template`，当前第 257-262 行）
把 per_share 段用开关包住：
```python
            # v4 §3:单份奖励(每日LP奖励÷最低份数) >= 该取档阈值(向上取档) -> 通过
            if template.get("per_share_reward_enabled", True):
                bracket = reward_bracket(min_size)
                per_share = market_reward / min_size
                thresholds = template.get("per_share_reward_thresholds", {})
                if per_share < float(thresholds.get(str(bracket), 0.30)):
                    continue
```
关掉时整段跳过；`rewards_min_size_min/max` 范围筛选、价差/单价区间/结算/冷却/品类白名单等**其余筛选照常**。

### 2.3 配置页（`config.html`）
「单份奖励阈值」标题下加一个勾选框「☑ 启用单份奖励阈值筛选」（`id="per-share-enabled"`）；取消勾选时把下面 5 个档 `input[data-bracket]` 置灰(`disabled`)。
- 加载：`per-share-enabled` 勾选 = `data.per_share_reward_enabled`（默认 true）；随之刷新档输入框 disabled 态。
- 保存：`data.per_share_reward_enabled = document.getElementById('per-share-enabled').checked`（布尔）。
- `onchange` 联动档输入框灰化。

### 2.4 测试
- `tests/test_scanner.py`：`per_share_reward_enabled=False` 时，单份奖励低于阈值的市场**放行**；`=True`（或缺省）时照旧剔除。
- `tests/test_settings_routes.py` 或 `test_database.py`：新键在 `TEMPLATE_DEFAULTS`、GET/POST 往返。

## 三、配置页内联小字（`config.html`）

在对应控件下各加一行 `<p class="hint">`（沿用现有灰字 12px 样式）：
- 单份奖励阈值段下：
  `单份奖励 = 市场每日奖励 ÷ 该市场最低下单份数；按最低份数所在档(20/50/100/200/250)分别设阈值，低于阈值不做该市场。`
- 区间变量段下：
  `累计厚度＝从买一往下累加的盘口深度(以最低份数为单位)；风险系数＝本档厚度 ÷ 金额数值(该档价)，价越高系数越低、价超金额表则该档不挂。`

## 四、使用说明翻新（`help.html`）

### 4.1 订正过时（v3.0.0 品类白名单）
- 第 20 行「先按品类黑名单排掉不做的类别（默认排体育、电竞、天气）」→ 改述为白名单：「先按你在配置里勾选的**做市品类白名单**过滤（默认做除体育/电竞/天气外的全部，含"其他/未分类"）」。
- 第 74 行参数表「排除品类 / 体育电竞天气 / 勾掉的品类完全不做」→ 改为两行：「做市品类（除体育/电竞/天气外全部）/ 只做勾中的品类」+「其他/未分类（true）/ 是否做不属于任何品类的市场」。

### 4.2 补全 v4 参数与概念
- 第 23 行「单份奖励要够高」细化为带算式：`单份奖励 =「市场日奖励 ÷ 最低挂单份数」`，并注明"此筛选可在策略参数里关闭"。
- 第 24 行「最低挂单份数 0~250」改述为可配的**奖励最低份额范围**(`rewards_min_size_min/max`，默认 1/250)。
- 「多档挂单规则」段（第 79-87 行）的"厚度/累计厚度"补上**风险系数**模式说明 + **金额数值表**（用第一节的定义与例子）。
- 策略参数表（第 62-76 行）补齐缺行：奖励最低份额范围、区间变量(累计厚度/风险系数)、金额数值表、**单份奖励阈值开关（默认启用）**。

## 五、README 翻新

`README.md` 策略参数表补行：`per_share_reward_enabled`、`rewards_min_size_min/max`、`tier_match_var`、`amount_value_table`（若尚缺）。并加一小节「几个概念怎么算」，用第一节**带例子**的口径写单份奖励/累计厚度/风险系数三条，与 help.html 一致。

## 六、版本

向后兼容新增功能（默认零回归）→ MINOR：v3.0.0 → **v3.1.0**。

## 七、不做（YAGNI）
- 不做每档单独开关（用户已定全局一个）。
- 不改 `per_share_reward_thresholds` 档结构、不改风险系数/离场计算。
- 不新增概念，仅解释既有概念。
