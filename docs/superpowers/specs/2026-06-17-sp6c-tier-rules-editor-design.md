# SP6c：tier_rules 可视化编辑器（visual tier-rules editor）设计 / spec

> 日期：2026-06-17
> 状态：待用户评审
> SP6（模板管理 UI）的第三个子块（SP6a 配置页对齐、SP6b 多模板 CRUD 已合并）。父背景见记忆 [[v4-strategy-integration-roadmap]]。

## 零、背景与定位

SP6a/6b 让配置页能按模板编辑全部 v4 策略参数,但 `tier_rules`（多档挂单规则,核心 IP）仍是个 **JSON 文本框**——小白用户没法用。SP6c 用**可视化嵌套编辑器**替掉它。

**tier_rules 结构**（引擎 `engine/laddering.py`）：`tier_rules` = 档列表;每档 = 区间列表;每区间 = `{"upper": 累加厚度上限或 null, "action": {"type": ...}}`。`_interval_action` 取**第一个** `upper is None or ct < upper` 的区间(半开升序 `[前一上界, upper)`)。`resolve_tier_share` 动作 5 种:`min_size` / `fixed_shares`(带 `shares`) / `fixed_amount`(带 `usd`) / `wallet_total` / `skip`(或不匹配 → 0)。

**关键事实**：引擎实际档数 = `len(tier_rules)`(`compute_market_ladders` line 76 `tiers_k = len(tier_rules)`);模板 `tiers_k` 字段**没被任何行为读取**——SP6a 加的 `tiers_k` 输入框是误导性死控件。

**SP6c 范围（纯前端 `config.html`）**：替掉 tier_rules JSON 文本框 + tiers_k 输入框,换成可视化编辑器。**无后端改动**（tier_rules 已由 SP6b `PUT /api/templates/<id>` 存取）。

**不在 SP6c**：SP6d 死字段代码清理（`tiers_k` / `held_condition_ids` / `needs_replace` / `strategy_check` 从 `TEMPLATE_DEFAULTS`/代码退役）。

## 一、已确认决策

| | 决策 |
| --- | --- |
| JSON 文本框 | **纯可视化替掉**（不保留 JSON 兜底,保存时由编辑器序列化成 tier_rules） |
| tiers_k 输入框 | **去掉**（档数由编辑器加/删档决定 = `len(tier_rules)`;TEMPLATE_DEFAULTS 死键留 SP6d） |
| 「其余」兜底行 | 每档**固定**一行 `upper=null` 的兜底行、**不可删**（保证高厚度有归属,不漏判成 skip） |

## 二、编辑器结构与行为

容器 `<div id="tier-rules-editor">`。

**档卡片**（`.tier-card`）：
- 卡片头：「档 1（买一）」「档 2」…按 DOM 位置编号（增删档后**重编号所有卡片头**）。
- 卡片体：若干**编号区间行** + 末尾一行固定**「其余」兜底行**。
- 卡片底：`[+ 区间]`（在「其余」行前插一个编号行）、`[删此档]`。

**区间行**（`.interval-row`）：
- 编号行：`厚度 <` + `<input class="upper-input" type="number">` + 动作下拉 + 条件参数 + `[删]`。
- 「其余」行（`.catch-all`）：显示「其余」（无 upper 输入）+ 动作下拉 + 条件参数;**无删除按钮**。
- 动作下拉 `<select class="action-select">` 5 选一:`min_size`「最小份数」/ `fixed_shares`「固定份数」/ `fixed_amount`「固定金额」/ `wallet_total`「钱包全额」/ `skip`「不挂」。
- 条件参数 `<input class="param-input">`:选 `fixed_shares` 显示（label「份数」,整数）、选 `fixed_amount` 显示（label「金额(USD)」,小数）、其余隐藏。动作下拉 `change` 时切显隐。

**容器底**：`[+ 加一档]`（追加一张新卡片,内含一行「其余=最小份数」）。

## 三、JS 函数（`config.html` `<script>`）

- `renderTierEditor(tierRules)`：清空 `#tier-rules-editor`;`tierRules` 为空 → 视为 `[[]]`（一档,仅兜底）。对每档建卡片:数值 `upper` 区间 → 编号行,`upper===null` 区间 → 兜底行;若该档无 null-upper 区间 → 末尾补一行「其余=min_size」。最后 `relabelTiers()`。
- `makeIntervalRow(interval, isCatchAll)`：建一行 DOM（含动作下拉按 `interval.action.type` 预选、参数框按类型显隐并填 `shares`/`usd`、编号行填 `upper`）。
- `relabelTiers()`：遍历卡片,头文案设为「档 1（买一）」/「档 N」。
- `addTier()` / `removeTier(card)`：增删卡片后 `relabelTiers()`。
- `addInterval(card)`：在该卡片「其余」行前插一个编号行（默认动作 min_size）。
- `removeInterval(row)`：删编号行（兜底行无此按钮）。
- 动作下拉 `change` → 切该行 `.param-input` 显隐 + label。
- `serializeTierRules()`：遍历 `.tier-card` → `.interval-row`,编号行 `upper=parseFloat(.upper-input)`、兜底行 `upper=null`;`action={type}`,`fixed_shares` 加 `shares=parseInt(.param-input)`、`fixed_amount` 加 `usd=parseFloat(.param-input)`;返回档列表。

**接线改动**（SP6b 的策略表单 JS）：
- `loadStrategy(tid)`：把原 `document.getElementById('tier-rules-json').value = JSON.stringify(...)` 改为 `renderTierEditor(data.tier_rules || [])`。
- 策略表单 `submit`：把原 `try { data.tier_rules = JSON.parse(textarea) } catch {...}` 改为校验 + `data.tier_rules = serializeTierRules()`。校验:任一编号行 `upper` 为空/NaN → `alert('请填写每个区间的厚度上限')` 并 `return`（不提交）。
- `tiers_k` 输入框删除后,策略表单的 `input[type=number][name]` 遍历自然不含它（不发送）。

## 四、HTML 改动（`config.html` 策略表单内）

- **删**：`<div class="form-group"><label>价格档数 K</label><input name="tiers_k" ...></div>`。
- **删**：`<h3>多档挂单规则 tier_rules（JSON）</h3>` + `<textarea id="tier-rules-json">` + 其格式提示 `<p>`。
- **加**（在原 tier_rules 位置）：
  ```html
  <h3>多档挂单规则</h3>
  <p style="color:#888;font-size:12px;">每档从买一往下；每行「累加厚度 &lt; X → 动作」，末行「其余」兜底。动作：最小份数 / 固定份数 / 固定金额 / 钱包全额 / 不挂。</p>
  <div id="tier-rules-editor"></div>
  <button type="button" class="btn" onclick="addTier()">+ 加一档</button>
  ```
  （「保存策略参数」按钮仍在表单末尾,不变。）

## 五、测试

- **无 JS 测试框架** → 实现后:
  1. `node --check` 提取的 `<script>` 通过（语法）。
  2. **人工核对清单**（启动 `python app.py` 登录后配置页）：
     - 默认模板载入 → 6 档(或现有档)正确渲染,每档动作/参数/upper 与值一致。
     - 加一档 / 删此档 → 卡片头重编号正确;加/删区间正确（兜底行无删除按钮）。
     - 动作下拉切到「固定份数」「固定金额」→ 参数框出现且 label 对;切到其它 → 隐藏。
     - 改几处 → 「保存策略参数」→ 切走再切回 / 刷新 → 回填一致。
     - 编号行 upper 留空保存 → 弹「请填写每个区间的厚度上限」、不提交。
     - 序列化结果符合引擎口径：编号行在前(升序 upper)、兜底行 `upper:null` 在末;`fixed_shares` 带 `shares`、`fixed_amount` 带 `usd`。
- **后端不变**：tier_rules round-trip 已由 SP6b `test_templates_routes.py::test_create_get_save_roundtrip` 覆盖（PUT tier_rules → get_template 反映）;`pytest` 全绿（计数不变,本期不动 Python）。

## 六、验收 checkpoint

1. tier_rules 用可视化编辑器编辑(无 JSON 文本框、无 tiers_k 输入框)。
2. 加/删档、加/删区间、动作切换参数显隐 均工作。
3. 每档固定「其余」兜底行(不可删),保证覆盖。
4. 保存 → 序列化成合法 tier_rules,刷新/切模板回填一致(round-trip)。
5. 编号行 upper 留空被拦截。
6. `node --check` 通过;`pytest` 全绿(Python 未改)。

## 七、范围之外

SP6d 死字段清理（`tiers_k` / `held_condition_ids` / `needs_replace` / `strategy_check`）。
