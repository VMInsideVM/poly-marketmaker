# SP6c tier_rules 可视化编辑器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把配置页策略表单里的 tier_rules JSON 文本框 + tiers_k 输入框替换为可视化嵌套编辑器（档 → 区间 → 动作）。

**Architecture:** 纯前端 `web/templates/config.html`（HTML + 原生 JS）。无后端改动——tier_rules 已由 SP6b `PUT /api/templates/<id>` 存取（其 round-trip 已被 `tests/test_templates_routes.py` 覆盖）。编辑器在 `loadStrategy` 时 `renderTierEditor(data.tier_rules)` 渲染，策略表单提交时 `serializeTierRules()` 走 DOM 生成 tier_rules。

**Tech Stack:** Flask Jinja 模板 + 原生 JS / 验证：`node --check` 语法 + 人工核对清单（无 JS 测试框架）。

**执行顺序:** 单任务（一组 config.html 编辑）。基线:SP6b 合并后 `418 passed`（本期不动 Python，计数不变）。

---

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `web/templates/config.html` | 删 tiers_k 输入 + tier_rules JSON 框；加可视化编辑器 HTML + JS；改 loadStrategy/submit 接线 | 修改 |

---

## Task 1: config.html tier_rules 可视化编辑器

**Files:** Modify `web/templates/config.html`。

- [ ] **Edit A — 删 tiers_k 输入框**

删除策略表单 `form-grid` 里这个 `form-group`:
```html
            <div class="form-group">
                <label>价格档数 K</label>
                <input type="number" name="tiers_k" step="1" min="1">
            </div>
```

- [ ] **Edit B — tier_rules JSON 段换成编辑器容器**

把:
```html
        <h3>多档挂单规则 tier_rules（JSON）</h3>
        <textarea id="tier-rules-json" rows="10" style="width:100%;font-family:monospace"></textarea>
        <p style="color:#888;font-size:12px;margin-top:4px;">
            JSON：档位数组，每档是若干区间 {"upper": 累加厚度上限或 null, "action": {...}}（半开升序 [前一上界, upper)）。
            动作 type 五选一：min_size（最小份数）/ fixed_shares（固定份数，带 "shares"）/ fixed_amount（固定金额，带 "usd"）/ wallet_total（钱包剩余全额）/ skip（不挂）。
            例：[[{"upper": null, "action": {"type": "min_size"}}]]。留默认即每档挂最小份数。
        </p>
```
替换为:
```html
        <h3>多档挂单规则</h3>
        <p style="color:#888;font-size:12px;margin-top:4px;">每档从买一往下；每行「累加厚度 &lt; X → 动作」，末行「其余」兜底。动作：最小份数 / 固定份数 / 固定金额 / 钱包全额 / 不挂。</p>
        <div id="tier-rules-editor"></div>
        <button type="button" class="btn" onclick="addTier()">+ 加一档</button>
```

- [ ] **Edit C — loadStrategy 接线**

把 `loadStrategy` 里这行:
```javascript
        document.getElementById('tier-rules-json').value =
            JSON.stringify(data.tier_rules || [], null, 2);
```
替换为:
```javascript
        renderTierEditor(data.tier_rules || []);
```

- [ ] **Edit D — 策略表单 submit 接线**

把策略表单 submit handler 里的 tier_rules 块:
```javascript
    try {
        data.tier_rules = JSON.parse(document.getElementById('tier-rules-json').value);
    } catch (err) {
        alert('tier_rules JSON 格式错误，请检查后再保存');
        return;
    }
```
替换为:
```javascript
    const badUpper = Array.from(
        document.querySelectorAll('#tier-rules-editor .interval-row:not(.catch-all) .upper-input')
    ).some(inp => inp.value === '' || isNaN(parseFloat(inp.value)));
    if (badUpper) { alert('请填写每个区间的厚度上限'); return; }
    data.tier_rules = serializeTierRules();
```

- [ ] **Edit E — 加编辑器 JS 函数**

在 `<script>` 块内（例如 `loadStrategy` 函数之后）加入以下函数:
```javascript
const ACTION_LABELS = {
    min_size: '最小份数', fixed_shares: '固定份数', fixed_amount: '固定金额',
    wallet_total: '钱包全额', skip: '不挂',
};

function actionSelectHtml(selectedType) {
    return Object.keys(ACTION_LABELS).map(t =>
        `<option value="${t}" ${t === selectedType ? 'selected' : ''}>${ACTION_LABELS[t]}</option>`
    ).join('');
}

function makeIntervalRow(interval, isCatchAll) {
    const action = (interval && interval.action) || {type: 'min_size'};
    const type = action.type || 'min_size';
    const row = document.createElement('div');
    row.className = 'interval-row' + (isCatchAll ? ' catch-all' : '');
    row.style.cssText = 'display:flex;align-items:center;gap:6px;margin:4px 0;flex-wrap:wrap';
    const upperHtml = isCatchAll
        ? '<span>其余</span>'
        : `厚度 &lt; <input class="upper-input" type="number" step="0.1" style="width:70px" value="${(interval && interval.upper != null) ? interval.upper : ''}">`;
    let paramVal = '';
    if (type === 'fixed_shares') paramVal = (action.shares != null) ? action.shares : '';
    if (type === 'fixed_amount') paramVal = (action.usd != null) ? action.usd : '';
    const paramHidden = (type === 'fixed_shares' || type === 'fixed_amount') ? '' : 'style="display:none"';
    const paramLabel = (type === 'fixed_amount') ? '金额(USD)' : '份数';
    const delBtn = isCatchAll ? ''
        : '<button type="button" class="btn btn-sm" onclick="this.closest(\'.interval-row\').remove()">✕</button>';
    row.innerHTML =
        `${upperHtml} → ` +
        `<select class="action-select" onchange="onActionChange(this)">${actionSelectHtml(type)}</select> ` +
        `<span class="param-wrap" ${paramHidden}><span class="param-label">${paramLabel}</span> ` +
        `<input class="param-input" type="number" step="0.01" style="width:90px" value="${paramVal}"></span> ` +
        delBtn;
    return row;
}

function onActionChange(sel) {
    const row = sel.closest('.interval-row');
    const wrap = row.querySelector('.param-wrap');
    const t = sel.value;
    if (t === 'fixed_shares' || t === 'fixed_amount') {
        wrap.style.display = '';
        row.querySelector('.param-label').textContent = (t === 'fixed_amount') ? '金额(USD)' : '份数';
    } else {
        wrap.style.display = 'none';
    }
}

function makeTierCard(tierIntervals) {
    const card = document.createElement('div');
    card.className = 'tier-card';
    card.style.cssText = 'border:1px solid #ddd;border-radius:4px;padding:8px;margin:8px 0';
    const head = document.createElement('div');
    head.className = 'tier-head';
    head.style.cssText = 'font-weight:bold;margin-bottom:4px';
    card.appendChild(head);
    const body = document.createElement('div');
    body.className = 'tier-body';
    card.appendChild(body);
    const numbered = (tierIntervals || []).filter(iv => iv && iv.upper != null);
    let catchAll = (tierIntervals || []).find(iv => iv && iv.upper == null);
    if (!catchAll) catchAll = {upper: null, action: {type: 'min_size'}};
    numbered.forEach(iv => body.appendChild(makeIntervalRow(iv, false)));
    body.appendChild(makeIntervalRow(catchAll, true));
    const ctrls = document.createElement('div');
    ctrls.style.cssText = 'margin-top:4px';
    ctrls.innerHTML =
        '<button type="button" class="btn btn-sm" onclick="addInterval(this.closest(\'.tier-card\'))">+ 区间</button> ' +
        '<button type="button" class="btn btn-sm btn-danger" onclick="removeTier(this.closest(\'.tier-card\'))">删此档</button>';
    card.appendChild(ctrls);
    return card;
}

function addInterval(card) {
    const body = card.querySelector('.tier-body');
    const catchAll = body.querySelector('.interval-row.catch-all');
    body.insertBefore(makeIntervalRow({upper: '', action: {type: 'min_size'}}, false), catchAll);
}

function addTier() {
    document.getElementById('tier-rules-editor').appendChild(makeTierCard([]));
    relabelTiers();
}

function removeTier(card) {
    card.remove();
    relabelTiers();
}

function relabelTiers() {
    document.querySelectorAll('#tier-rules-editor .tier-card').forEach((card, i) => {
        card.querySelector('.tier-head').textContent = (i === 0) ? '档 1（买一）' : `档 ${i + 1}`;
    });
}

function renderTierEditor(tierRules) {
    const ed = document.getElementById('tier-rules-editor');
    ed.innerHTML = '';
    const tiers = (tierRules && tierRules.length) ? tierRules : [[]];
    tiers.forEach(t => ed.appendChild(makeTierCard(t)));
    relabelTiers();
}

function serializeTierRules() {
    const tiers = [];
    document.querySelectorAll('#tier-rules-editor .tier-card').forEach(card => {
        const intervals = [];
        card.querySelectorAll('.interval-row').forEach(row => {
            const type = row.querySelector('.action-select').value;
            const action = {type};
            if (type === 'fixed_shares') {
                action.shares = parseInt(row.querySelector('.param-input').value, 10) || 0;
            }
            if (type === 'fixed_amount') {
                action.usd = parseFloat(row.querySelector('.param-input').value) || 0;
            }
            const upper = row.classList.contains('catch-all')
                ? null : parseFloat(row.querySelector('.upper-input').value);
            intervals.push({upper, action});
        });
        tiers.push(intervals);
    });
    return tiers;
}
```

- [ ] **Step F — 全套测试无回归**

Run: `python -m pytest -q`
Expected: 仍 `418 passed`（纯前端，不动 Python）。

- [ ] **Step G — JS 语法检查**

```bash
python - <<'PY'
import re
html = open("web/templates/config.html", encoding="utf-8").read()
js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
open("_cfg_check.js", "w", encoding="utf-8").write(js)
PY
node --check _cfg_check.js && echo "JS OK"; rm -f _cfg_check.js
```
Expected: `JS OK`。再确认死控件已去:
```bash
grep -nE "tier-rules-json|name=\"tiers_k\"" web/templates/config.html || echo "removed OK"
grep -nE "tier-rules-editor|serializeTierRules|renderTierEditor" web/templates/config.html
```
Expected: 第一个 `removed OK`（无残留）；第二个有匹配。

- [ ] **Step H — 人工核对清单（无 JS 测试框架）**

启动 `python app.py` 登录 → 配置页:
1. 策略参数里不再有「价格档数 K」输入、不再有 tier_rules JSON 文本框；出现可视化编辑器。
2. 默认模板载入 → 档卡片正确（档 1 标「买一」）；每行动作/参数/厚度上限与值一致。
3. 「+ 加一档」「删此档」→ 卡片头重编号；「+ 区间」在「其余」前插行、「✕」删编号行（「其余」行无删除按钮）。
4. 动作下拉切「固定份数」→ 出现「份数」框；切「固定金额」→ 出现「金额(USD)」框；切其它 → 隐藏。
5. 改几处 + 加一档 →「保存策略参数」→ 切走再切回 / 刷新 → 回填一致。
6. 某编号行厚度上限留空保存 → 弹「请填写每个区间的厚度上限」、不提交。

- [ ] **Step I — Commit**

```bash
git add web/templates/config.html
git commit -m "feat(config-ui): tier_rules 可视化编辑器(档/区间/动作嵌套,替掉 JSON 文本框 + tiers_k 输入)"
```

---

## 验收 checkpoint（对应 spec §六）

1. tier_rules 用可视化编辑器（无 JSON 框、无 tiers_k 输入）:Edit A/B + Step G grep。
2. 加/删档、加/删区间、动作切参数显隐:Edit E + Step H.3/4。
3. 每档固定「其余」兜底行不可删:`makeIntervalRow(..., true)` 无删除按钮 + Step H.3。
4. 保存序列化合法 tier_rules、round-trip:Edit D + Step H.5（后端 round-trip SP6b 已覆盖）。
5. 编号行 upper 留空拦截:Edit D + Step H.6。
6. `node --check` 通过、`pytest` 全绿:Step F/G。

## 范围之外

SP6d 死字段清理（`tiers_k` / `held_condition_ids` / `needs_replace` / `strategy_check`）。
