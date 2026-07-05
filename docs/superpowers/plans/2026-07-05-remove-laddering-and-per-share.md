# 删除多档做市 / 单份奖励阈值 / 累积厚度 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 laddering（多档做市）、单份奖励阈值筛选、`tier_match_var`（累计厚度/风险系数模式选择器）从生产代码、配置默认值、配置页 UI、测试里彻底删除，让 `gap_single` 成为唯一下单路径。

**Architecture:** 分两个可独立回绿的删除单元——先删 per_share（Task 1），再删 laddering（Task 2-6，按「先断依赖、再删函数、再删默认值、最后删 UI」的顺序）。每个 Task 结束时 `pytest` 全绿。gap_single 链路一字不动。

**Tech Stack:** Python 3 / Flask / pytest；前端为 Jinja 模板内嵌原生 JS。

## Global Constraints

- gap_single 下单输出逐字节不变：`compute_market_single_orders` 及其依赖链（`plan_gap_single_order`→`explain_gap_single_order`→`amount_value`）、`gap_single_reason`、`gap_single_price_basis`、`preview_gap_single_market`、`reconcile_buy_orders` 全部保留、不改。
- `amount_value_table`（金额数值表）保留，`gap_wide_cents`/`gap_mid_cents`/`gap_high_coeff_sum_min`/`rule1~3_min_coeff` 保留。
- 每个 Task 末尾 `pytest` 必须全绿。
- **含中文的前端文件（`web/templates/config.html`、`web/templates/markets.html`）由主 agent 直接用 Write/Edit 修改，不派 subagent**（subagent 易把中文写成别字并加 BOM）；改后 grep 确认无残留标识符、无 BOM。
- DB 已存的死键不迁移清理（`database.py:228` 与 `routes.py:339` 的 `TEMPLATE_DEFAULTS` 白名单会自动忽略它们）。
- 提交只 stage 本任务涉及的文件，不卷入仓库既有的其它 WIP。
- commit message 结尾附：`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。

---

### Task 1: 删除单份奖励阈值（per_share）

**Files:**
- Modify: `engine/scanner.py`（删 `reward_bracket()`；删 `filter_for_template` 内 per_share 检查段 ~386-393）
- Modify: `config.py`（`TEMPLATE_DEFAULTS` 删 `per_share_reward_enabled`、`per_share_reward_thresholds`，~77-85）
- Modify: `web/routes.py`（删模块顶 `from engine.scanner import reward_bracket` ~26；删 `_derive_per_share` 内 per_share 字段与阈值取值 ~145-159，保留奖励范围/盘口价差计算）
- Modify: `web/templates/markets.html`（删「单份奖励」列头 ~22、对应 `<td>`、per_share 单元格渲染 JS ~63-66；指标说明 ~31 去掉「单份奖励」字样）
- Test: `tests/test_scanner.py`（删 `reward_bracket` 用例 ~449-465、per_share 筛选用例 ~355/428 一带）、`tests/test_database.py`（删 51 行 `per_share_reward_enabled` 断言、475 行 `per_share_reward_thresholds` 断言）、`tests/test_eligible_fields.py`（删模板里 `per_share_reward_thresholds` ~47）、`tests/test_markets_route.py`（删 `per_share_reward_thresholds` ~34）、`tests/test_settings_routes.py`（删 `per_share_reward_thresholds` 相关 ~30/49/68）

**Interfaces:**
- Produces: `filter_for_template` 不再做 per_share 门槛；`reward_bracket` 不再存在；`_derive_per_share` 只产出 `reward_range_min`/`reward_range_max`/`spread_cents`/`outcome`，不再产出 `per_share*`。

- [ ] **Step 1: 删 scanner 的 per_share 段与 `reward_bracket`**

`engine/scanner.py` 删除整个 `reward_bracket` 函数（当前 ~106-116）：
```python
def reward_bracket(min_size):
    """向上取档(更保守):返回 20/50/100/200/250;超 250 或 <=0 返回 None。..."""
    if min_size <= 0:
        return None
    for b in (20, 50, 100, 200, 250):
        if min_size <= b:
            return b
    return None
```
并在 `filter_for_template` 里删除这一段（当前 ~386-393）：
```python
            # v4 §3:单份奖励(每日LP奖励÷最低份数) >= 该取档阈值(向上取档) -> 通过。
            # 可整体关闭(per_share_reward_enabled=False):跳过本门槛,其余筛选照常。
            if template.get("per_share_reward_enabled", True):
                bracket = reward_bracket(min_size)
                per_share = market_reward / min_size
                thresholds = template.get("per_share_reward_thresholds", {})
                if per_share < float(thresholds.get(str(bracket), 0.30)):
                    continue
```
删除后其上的 `min_size`（份额范围筛选）与 `market_reward`（min_reward 筛选）仍在使用，保持不动。

- [ ] **Step 2: 删 config 默认键**

`config.py::TEMPLATE_DEFAULTS` 删除这两个键及其上方注释：
```python
    # 单份奖励阈值筛选总开关(每模板);False=整段跳过,其余筛选不受影响。
    "per_share_reward_enabled": True,
    # 单份奖励阈值档位(SP4)
    "per_share_reward_thresholds": {
        "20": 0.30, "50": 0.30, "100": 0.30, "200": 0.30, "250": 0.30,
    },
```

- [ ] **Step 3: 删 routes 的 per_share 展示**

`web/routes.py` 删模块顶导入 `from engine.scanner import reward_bracket`（~26）。
在 `_derive_per_share` 中删除 per_share 相关（当前 ~145-159）：
```python
    try:
        thr = db.get_template(db.get_default_template_id()).get(
            "per_share_reward_thresholds", {}
        )
    except Exception:
        thr = {}
    for m in markets:
        ms = float(m.get("rewards_min_size", 0) or 0)
        ps = (float(m.get("daily_reward", 0) or 0) / ms) if ms > 0 else None
        bracket = reward_bracket(int(ms)) if ms > 0 else None
        threshold = float(thr.get(str(bracket), 0.30)) if bracket else None
        m["per_share"] = ps
        m["per_share_bracket"] = bracket
        m["per_share_threshold"] = threshold
        m["per_share_ok"] = ps is not None and threshold is not None and ps >= threshold
```
改为直接 `for m in markets:` 起头，保留其后 `rr_min = rr_max = sp = None` 起的奖励范围/盘口价差/方向计算（~161-184）不动。函数名 `_derive_per_share` 保留（避免改调用点）。

- [ ] **Step 4: 删 markets.html 的单份奖励列（主 agent 直接改）**

删除列头 `<th>单份奖励</th>`（~22）、行模板里对应的 `<td>`（调用 per_share 渲染那格）、以及 per_share 单元格渲染 JS（~63-66 的函数体）。指标说明（~31）把「最低份数 / 单份奖励 / 奖励范围 / 盘口价差」里的「单份奖励 / 」去掉。
改后：`grep -n "per_share\|单份奖励" web/templates/markets.html` 应无输出。

- [ ] **Step 5: 删相关测试片段**

删除/调整下列测试中的 per_share 引用（按 Files 所列）：`test_scanner.py` 的 `reward_bracket` 两个用例与 per_share 筛选用例；`test_database.py:51`、`:475` 断言；`test_eligible_fields.py`、`test_markets_route.py`、`test_settings_routes.py` 模板里的 `per_share_reward_thresholds` 键（连同依赖它的断言）。

- [ ] **Step 6: 跑全套测试**

Run: `pytest -q`
Expected: PASS（全绿；无 `reward_bracket` / `per_share_reward` 相关 collection 错误）。

- [ ] **Step 7: grep 确认生产代码零残留**

Run: `grep -rn "per_share_reward\|reward_bracket" engine web config.py models`
Expected: 无输出（docs/ 不计）。

- [ ] **Step 8: Commit**

```bash
git add engine/scanner.py config.py web/routes.py web/templates/markets.html tests/test_scanner.py tests/test_database.py tests/test_eligible_fields.py tests/test_markets_route.py tests/test_settings_routes.py
git commit -m "refactor: 删除单份奖励阈值(per_share)筛选"
```

---

### Task 2: `place_orders` 中 gap_single 变无条件路径（断 manager 对 laddering 的依赖）

**Files:**
- Modify: `engine/manager.py::place_orders`（删 laddering 分支与相关读取；删 import）
- Test: `tests/test_place_orders.py`（`_make_worker` 默认模板改成 gap_single 形态；删 laddering 专用用例，保留 gap_single 用例）

**Interfaces:**
- Consumes: `compute_market_single_orders`、`explain_gap_single_order`、`gap_single_reason`、`gap_single_price_basis`、`reconcile_buy_orders`（均保留）。
- Produces: `place_orders` 不再读取 `placement_mode`/`tier_rules`/`tier_match_var`/`min_price_double_cents`，恒走 gap_single。

- [ ] **Step 1: 删 import**

`engine/manager.py` 的 `from engine.laddering import (...)` 块删掉 `compute_market_ladders`、`apply_double_sided_floor`，保留 `compute_market_single_orders`、`explain_gap_single_order`、`gap_single_reason`、`gap_single_price_basis`、`reconcile_buy_orders`。

- [ ] **Step 2: 删 laddering 相关读取与空 tier_rules 闸**

删除这些 `tmpl.get` 读取（~157-181 一带）：`tier_rules`、`tier_match_var`、`placement_mode`、`gap_*`/`rule*` 保留，`min_price_double_cents`（~181）删除。删除空 tier_rules 告警块（~167-174）：
```python
        if placement_mode == "laddering" and not tier_rules:
            logger.warning("place_orders skipped for %s: empty tier_rules ...", ...)
            return
```

- [ ] **Step 3: gap_single 无条件化**

把下单主体里的 `if placement_mode == "gap_single":` / `else:`（~309-348）改为无条件执行 gap_single 分支：保留 `compute_market_single_orders(...)` 与其后的 `explain_gap_single_order` 循环，删除 `else` 支（`compute_market_ladders` + `apply_double_sided_floor`）。把 ~387、~421 处 `if placement_mode == "gap_single"` 条件判断改为恒真（`gap_d = gap_explains.get(key)`；记账分支直接走 gap_single）。

- [ ] **Step 4: 改造 test_place_orders 的 `_make_worker`**

`tests/test_place_orders.py::_make_worker` 默认 `tmpl` 去掉 `tier_rules`、`min_price_double_cents`，改为 gap_single 形态（保留 `max_exposure_usd`/`max_exposure_shares`/`max_concurrent_markets`；gap 参数走代码 `.get` 默认值即可）：
```python
    tmpl = {
        "max_exposure_usd": 250,
        "max_exposure_shares": 500,
        "max_concurrent_markets": 10,
    }
```

- [ ] **Step 5: 删 laddering 专用用例、保留 gap_single 用例**

删除 `test_place_orders.py` 里断言多档梯队行为的用例（如 `test_places_multi_tier_min_size_on_one_side`、`test_empty_tier_rules_places_nothing` 及其它依赖 `tier_rules`/多档结果的用例）。保留并确保通过 gap_single 用例（~433、~458 一带以及未依赖 laddering 的通用用例）。判定标准以 Step 6 全绿为准。

- [ ] **Step 6: 跑测试**

Run: `pytest tests/test_place_orders.py -q && pytest -q`
Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add engine/manager.py tests/test_place_orders.py
git commit -m "refactor: place_orders 恒走 gap_single,移除 laddering 分支"
```

---

### Task 3: `_ladder_payload` 预演只留 gap_single（断 routes 对 laddering 的依赖）

**Files:**
- Modify: `web/routes.py::_ladder_payload`（删 laddering 分支与 import）
- Test: 若有针对 laddering 预演的路由测试则删除；`tests/test_markets_route.py` 调整模板

**Interfaces:**
- Consumes: `preview_gap_single_market`（保留）。
- Produces: `/api/markets/<id>/ladder` 恒返回 gap_single 预演。

- [ ] **Step 1: 删 laddering 预演分支**

`web/routes.py::_ladder_payload` 删除函数顶 `from engine.laddering import preview_market_ladders`（~1012），删除 `tier_rules`/`tier_match_var` 读取（~1022-1023），删除 `placement_mode` 判断与 laddering 尾段（~1083 的 `if placement_mode == "gap_single":` 判断改为无条件走 gap_single 预演，删除 ~1110-1124 的 `preview_market_ladders` 分支与其 `return`）。`amount_value_table`、`gap_*`/`rule*` 读取保留（gap_single 预演要用）。

- [ ] **Step 2: 调整 test_markets_route**

`tests/test_markets_route.py` 的假模板去掉 `tier_rules`（~31）；若该测试断言 `placement_mode=='laddering'` 相关，改为 gap_single。

- [ ] **Step 3: 跑测试**

Run: `pytest -q`
Expected: PASS。

- [ ] **Step 4: grep 确认 routes 不再引用 laddering 预演**

Run: `grep -n "preview_market_ladders\|tier_match_var\|placement_mode" web/routes.py`
Expected: 无输出（`_derive_per_share` 与其它已在前序任务清理）。

- [ ] **Step 5: Commit**

```bash
git add web/routes.py tests/test_markets_route.py
git commit -m "refactor: _ladder_payload 预演恒走 gap_single"
```

---

### Task 4: 删除 laddering 专用函数与其测试文件

**Files:**
- Modify: `engine/laddering.py`（删 7 个 laddering 专用函数）
- Delete: `tests/test_laddering.py`、`tests/test_laddering_preview.py`

**Interfaces:**
- Produces: `engine/laddering.py` 仅剩 gap_single 与 `amount_value`/`reconcile_buy_orders`。

- [ ] **Step 1: 删函数**

`engine/laddering.py` 删除：`build_ladder`（~261）、`_interval_action`（~310）、`resolve_tier_share`（~324）、`compute_market_ladders`（~342）、`apply_double_sided_floor`（~404）、`_verbose_levels`（~452）、`preview_market_ladders`（~498）。保留：`amount_value`、`explain_gap_single_order`、`plan_gap_single_order`、`_GAP_RULE_LABEL`、`gap_single_reason`、`gap_single_price_basis`、`compute_market_single_orders`、`reconcile_buy_orders`、`preview_gap_single_market`。

- [ ] **Step 2: 删测试文件**

```bash
git rm tests/test_laddering.py tests/test_laddering_preview.py
```

- [ ] **Step 3: 跑测试**

Run: `pytest -q`
Expected: PASS（无 import 已删函数的 collection 错误）。

- [ ] **Step 4: grep 确认零残留引用**

Run: `grep -rn "compute_market_ladders\|apply_double_sided_floor\|preview_market_ladders\|build_ladder\|resolve_tier_share\|_verbose_levels\|_interval_action" engine web tests`
Expected: 无输出。

- [ ] **Step 5: Commit**

```bash
git add engine/laddering.py
git commit -m "refactor: 删除 laddering 专用函数与其测试"
```

---

### Task 5: 删除 laddering 配置默认值与相关断言

**Files:**
- Modify: `config.py::TEMPLATE_DEFAULTS`（删 `placement_mode`、`tier_rules`、`min_price_double_cents`、`tier_match_var`）
- Test: `tests/test_database.py`（删 57-58 `tier_rules`、64 `min_price_double_cents`、609 `tier_match_var` 断言）、`tests/test_settings_routes.py`（删 `tier_rules`/`placement_mode`/`min_price_double_cents` 相关键与断言 ~29-100）、`tests/test_templates_routes.py`（删 `tier_rules` ~35/40）

**Interfaces:**
- Produces: `TEMPLATE_DEFAULTS` 不再含这四键；白名单读写自动忽略 DB 中旧行。

- [ ] **Step 1: 删默认键**

`config.py::TEMPLATE_DEFAULTS` 删除：`"placement_mode": "gap_single"`（~58）、`"tier_rules": [...]`（~71）、`"min_price_double_cents": 10`（~75）、`"tier_match_var": "cumulative_thickness"`（~90）。保留 `amount_value_table`（~91）、`gap_*`/`rule*`。

- [ ] **Step 2: 删相关断言**

按 Files 删除 `test_database.py`/`test_settings_routes.py`/`test_templates_routes.py` 中对这四键的断言与测试数据键。

- [ ] **Step 3: 跑测试**

Run: `pytest -q`
Expected: PASS。

- [ ] **Step 4: Commit**

```bash
git add config.py tests/test_database.py tests/test_settings_routes.py tests/test_templates_routes.py
git commit -m "refactor: 删除 laddering/tier_match_var 配置默认值"
```

---

### Task 6: 配置页与市场发现页 UI 清理（主 agent 直接改）

**Files:**
- Modify: `web/templates/config.html`
- Modify: `web/templates/markets.html`

**Interfaces:**
- Produces: 配置页无挂单模式下拉/区间变量下拉/多档规则编辑器/双边地板输入；市场发现展开预演只剩 gap_single 表。

- [ ] **Step 1: config.html —— 删表单块**

删除：`min_price_double_cents` 输入所在 form-group（~102）；挂单模式下拉 `<select name="placement_mode" id="placement-mode">` 整块（~109-110 起，含 `多档做市` option），gap_single 的断层参数区块（`gap_high_coeff_sum_min`/`gap_wide`/`gap_mid`/`rule1~3` ~118-123）**保留并改为常显**（不再由挂单模式切换显隐）；单份奖励阈值整块（h3~141 + 启用勾选 + 档位阈值 + 说明，若 Task 1 未删净则在此补删）；区间变量下拉 `<select name="tier_match_var" id="tier-match-var">`（~154-165）；多档挂单规则编辑器（h3~168 起整块）。

- [ ] **Step 2: config.html —— 删 JS**

删除函数：`updatePlacementMode`（~358）、`tierMatchVarLabel`（~367-370）、`updateTierMatchVar`、`updatePerShareEnabled`、`renderTierEditor`、`serializeTierRules`。删除加载填充里的 per_share/tier 相关（~326/332/334/336）与保存收集里的相关键（~608-609 `tier_match_var`/`placement_mode`、~620-621 `per_share_*`、~637 `tier_rules`、~622 的 `if placement_mode !== 'gap_single'` 分支）。确保没有残留 JS 引用已删的 DOM id（`placement-mode`/`tier-match-var`/`per-share-enabled` 等）。

- [ ] **Step 3: markets.html —— 删 laddering 预演分支**

展开预演 JS 里保留 `if (data.placement_mode === 'gap_single') { ... }` 的 gap_single 渲染（~203），删除其后的 laddering 渲染分支（`const isRisk = data.tier_match_var ...` ~209 起，含累计厚度/风险系数表头 ~235）。指标说明（~31）里「跑 v4 推演看梯队(订单厚度 / 累计厚度 / 有效价格)」改写为 gap_single 口径（如「点展开实时拉订单簿、按断层单档规则推演选档」）。

- [ ] **Step 4: 校验前端无残留、无 BOM**

Run: `grep -rn "placement_mode\|tier_match_var\|tier_rules\|per_share\|累积厚度\|多档\|min_price_double\|梯队" web/templates/config.html web/templates/markets.html`
Expected: 无输出（`gap_single` 字样若仍作为唯一模式标识出现可接受，但不应有 laddering/per_share 残留）。
Run: `grep -rlP "^\xEF\xBB\xBF" web/templates/config.html web/templates/markets.html`
Expected: 无输出（无 BOM）。

- [ ] **Step 5: 人工走查**

启动 `python app.py`，登录后：配置页能正常渲染+保存（无 JS 报错）；市场发现「展开」某市场，预演返回 gap_single 选档结果。

- [ ] **Step 6: Commit**

```bash
git add web/templates/config.html web/templates/markets.html
git commit -m "refactor: 配置页/市场发现页移除 laddering 与单份奖励阈值 UI"
```

---

### Task 7: 收尾全库校验

**Files:** 无改动（仅校验）

- [ ] **Step 1: 全库 grep 确认生产代码零残留**

Run:
```bash
grep -rn "placement_mode\|compute_market_ladders\|apply_double_sided_floor\|preview_market_ladders\|tier_rules\|tier_match_var\|cumulative_thickness\|min_price_double_cents\|per_share_reward\|reward_bracket" engine web config.py models
```
Expected: 无输出（`docs/` 历史文档不计）。

- [ ] **Step 2: 跑全套测试**

Run: `pytest -q`
Expected: PASS（全绿）。

- [ ] **Step 3: 用 /verify 或人工驱动一轮真实流程**

确认引擎经 gap_single 正常发现+下单、配置页可保存、市场发现预演可用（若 Task 6 Step 5 已走查可复用结论）。

---

## Self-Review 结论

- **Spec 覆盖**：spec 删除清单 A（per_share）→ Task 1；B（laddering + tier_match_var）→ Task 2-6；收尾 grep+pytest → Task 7。保留项（gap_single/amount_value_table）落在 Global Constraints 并在每个 Task 的保留说明里点名。
- **无占位符**：每步给出具体文件、行锚点与删/留代码块；测试删除按文件+用例名指明，以 `pytest` 全绿为客观判据。
- **类型一致**：全程无新增函数/类型，仅删除；保留函数签名不动，无命名漂移。
