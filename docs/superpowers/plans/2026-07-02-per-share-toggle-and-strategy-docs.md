# 单份奖励阈值开关 + 策略概念说明翻新 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给"单份奖励阈值"筛选加一个每模板全局开关（可整体关掉），并在配置页加内联小字、翻新 help.html 使用说明与 README，把单份奖励/累计厚度/风险系数三个概念讲清。

**Architecture:** 后端一个布尔旗 `per_share_reward_enabled`（默认 True，零回归）包住 `scanner.filter_for_template` 里的 per_share 门槛段；前端配置页加勾选框（关时置灰档输入）+ 两行小字；help.html/README 做文档翻新（含订正 v3.0.0 后遗留的"品类黑名单"过时表述）。

**Tech Stack:** Python 3 / Flask / SQLite（key-value 模板）/ pytest；纯前端 JS（无框架）；Markdown/HTML 文档。

## Global Constraints

- UI 文案、说明一律简体中文；改动的 `config.html`/`help.html`/`README.md` 保持**无 BOM**。
- 模板参数走 `TEMPLATE_DEFAULTS` 的 key/value merge：新键加进去即自动持久化（`web/routes.py` 按 `k in TEMPLATE_DEFAULTS` 过滤保存），**不改保存路由**。
- 开关默认 `True` = 行为零回归；关掉只跳过 per_share 门槛，`rewards_min_size_min/max`、价差/单价/结算/冷却/品类白名单等**其余筛选照常**。
- 三个概念的权威口径（文档统一照此，README 要带例子）：
  - 单份奖励 = 市场每日奖励 ÷ 最低下单份数(`rewards_min_size`)。例：日奖励 $60 / 最低份数 20 = 3.0。
  - 累计厚度 = 从买一往下累加到该档的厚度和；单档厚度 = 盘口该价挂单量 ÷ 最低份数。例：最低份数 20，0.30 挂 60(厚度3)、0.29 挂 40(厚度2) → 0.29 档累计厚度 5。
  - 风险系数 = 本档厚度 ÷ 金额数值(该档价)。例：金额表 20¢→1/25¢→1.5/30¢→2，价 0.25 厚度 3 → 3/1.5 = 2.0；价超表→该档不挂。
- 中文文件（`config.html`/`help.html`/`README.md`）由主控直接 Write/Edit（规避 subagent 中文别字/BOM），改后核对无 BOM、关键中文无别字。
- 版本：向后兼容新增功能 → MINOR，`version.py` `3.0.0` → `3.1.0`。
- 每 Task 独立可测/独立提交；提交只 stage 本 Task 文件。

---

### Task 1: 后端开关（config.py + scanner + 测试）

**Files:**
- Modify: `config.py`（`TEMPLATE_DEFAULTS`）
- Modify: `engine/scanner.py`（`filter_for_template` per_share 段，当前第 257-262 行）
- Test: `tests/test_scanner.py`（`TestFilterForTemplate` 新增用例）
- Test: `tests/test_database.py`（默认值断言）

**Interfaces:**
- Produces: `TEMPLATE_DEFAULTS["per_share_reward_enabled"]`（bool，默认 True）；`filter_for_template` 在该键为 False 时跳过 per_share 门槛。

- [ ] **Step 1: 写失败测试**

`tests/test_scanner.py` 在 `TestFilterForTemplate` 类内加两个用例（复用现有 `_candidate`/`_template`/`_scanner` 助手）：
```python
    def test_per_share_disabled_lets_low_through(self):
        scanner = self._scanner()
        # 单份奖励 20/100 = 0.20 < 0.30,但关掉开关 -> 应放行
        pool = [self._candidate("A", [], daily_reward=20)]
        tmpl = self._template(per_share_reward_enabled=False)
        out = scanner.filter_for_template(pool, tmpl, "0xW")
        assert any(e["market_id"] == "A" for e in out)

    def test_per_share_enabled_by_default_still_excludes(self):
        scanner = self._scanner()
        pool = [self._candidate("A", [], daily_reward=20)]
        # _template 不设该键 -> 默认启用 -> 仍剔除
        assert scanner.filter_for_template(pool, self._template(), "0xW") == []
```
`tests/test_database.py` 在 `test_config_split_engine_and_template_defaults`（含 `TEMPLATE_DEFAULTS` 断言那处）加一行：
```python
    assert TEMPLATE_DEFAULTS["per_share_reward_enabled"] is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_scanner.py -k per_share tests/test_database.py -k defaults -q`
Expected: FAIL（`test_per_share_disabled_lets_low_through` 因当前无视开关仍剔除 A → 断言失败；database 断言 KeyError）

- [ ] **Step 3: config.py 加键**

`config.py` `TEMPLATE_DEFAULTS` 里，在 `"per_share_reward_thresholds": {...}` 附近加：
```python
    # 单份奖励阈值筛选总开关(每模板);False=整段跳过,其余筛选不受影响。
    "per_share_reward_enabled": True,
```

- [ ] **Step 4: scanner 包住 per_share 段**

`engine/scanner.py` 第 257-262 行整段替换为：
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

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_scanner.py tests/test_database.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add config.py engine/scanner.py tests/test_scanner.py tests/test_database.py
git commit -m "feat(scanner): 单份奖励阈值全局开关 per_share_reward_enabled(默认启用)"
```

---

### Task 2: 配置页（开关 UI + 置灰 + 两行小字）

**Files:**
- Modify: `web/templates/config.html`（单份奖励段 104-111；区间变量段 113-125；`loadStrategy`；保存段）

**Interfaces:**
- Consumes: 后端模板键 `per_share_reward_enabled`（Task 1）。
- Produces: 表单保存 `data.per_share_reward_enabled`（bool）；新 JS 函数 `updatePerShareEnabled()`。

- [ ] **Step 1: 单份奖励段加勾选框 + 小字**

`web/templates/config.html` 第 104 行 `<h3>单份奖励阈值（按最低份数取档）</h3>` 之后、第 105 行 `<div id="per-share-thresholds"...` 之前，插入：
```html
        <label class="ps-enable"><input type="checkbox" id="per-share-enabled" checked onchange="updatePerShareEnabled()"> 启用单份奖励阈值筛选</label>
        <p class="hint" style="color:#888;font-size:12px">单份奖励 = 市场每日奖励 ÷ 该市场最低下单份数；按最低份数所在档(20/50/100/200/250)分别设阈值，低于阈值不做该市场。</p>
```

- [ ] **Step 2: 区间变量段加小字**

同文件第 125 行 `</div>`（`id="amount-value-group"` 的收尾 div）之后、第 127 行 `<h3>多档挂单规则</h3>` 之前，插入：
```html
        <p class="hint" style="color:#888;font-size:12px">累计厚度＝从买一往下累加的盘口深度(以最低份数为单位)；风险系数＝本档厚度 ÷ 金额数值(该档价)，价越高系数越低、价超金额表则该档不挂。</p>
```

- [ ] **Step 3: 加 updatePerShareEnabled + loadStrategy 回填**

在 `<script>` 内合适处（`loadStrategy` 之前）加：
```javascript
function updatePerShareEnabled() {
    const on = document.getElementById('per-share-enabled').checked;
    document.querySelectorAll('#per-share-thresholds input[data-bracket]').forEach(inp => {
        inp.disabled = !on;
    });
}
```
在 `loadStrategy` 里，per-share 档回填那段之后（`document.querySelectorAll('#per-share-thresholds input[data-bracket]').forEach(...)` 块结束后）加：
```javascript
        document.getElementById('per-share-enabled').checked =
            (data.per_share_reward_enabled !== false);
        updatePerShareEnabled();
```

- [ ] **Step 4: 保存段带上开关**

在保存处理里 `data.per_share_reward_thresholds = ps;` 之后加：
```javascript
    data.per_share_reward_enabled = document.getElementById('per-share-enabled').checked;
```

- [ ] **Step 5: 校验 JS 语法 + 无 BOM**

Run:
```bash
cd "C:/Users/Hank/PycharmProjects/poly简单做市"
python - <<'PY'
import re
html = open("web/templates/config.html", encoding="utf-8").read()
open("_check.js","w",encoding="utf-8").write("\n".join(re.findall(r"<script>(.*?)</script>", html, re.S)))
PY
node --check _check.js && rm -f _check.js
head -c 3 web/templates/config.html | od -An -tx1   # 应非 ef bb bf
```
Expected: `node --check` OK；无 BOM。人工确认新增中文无别字。

- [ ] **Step 6: 提交**

```bash
git add web/templates/config.html
git commit -m "feat(config-ui): 单份奖励阈值开关 + 单份奖励/区间变量内联小字"
```

---

### Task 3: help.html 使用说明翻新

**Files:**
- Modify: `web/templates/help.html`

**Interfaces:** 无（纯文档）。

- [ ] **Step 1: 订正品类黑名单→白名单（发现段）**

第 20 行整行替换：
```html
    <p>程序拉取 Polymarket 当前所有发奖励的市场，先按你在配置里勾选的<strong>做市品类白名单</strong>过滤（默认做除体育/电竞/天气外的全部，含「其他/未分类」），再用几道门槛筛：</p>
```
第 23 行（`<li>单份奖励要够高…`）整行替换：
```html
        <li>单份奖励要够高：用「市场日奖励 ÷ 该市场最低挂单份数」算，低于阈值不做（此筛选可在策略参数里整体关闭）。</li>
```
第 24 行（`<li>最低挂单份数…0 到 250…`）整行替换：
```html
        <li>最低挂单份数要落在你设的<strong>奖励最低份额范围</strong>内（默认 1~250）。</li>
```

- [ ] **Step 2: 策略参数表订正 + 补行**

第 74 行（`<tr><td>排除品类</td>...`）整行替换为两行：
```html
            <tr><td>做市品类</td><td>除体育/电竞/天气外全部</td><td>白名单：只做勾中的品类。数量为当前该品类的奖励市场数。</td></tr>
            <tr><td>其他/未分类</td><td>做</td><td>是否做「不属于任何 curated 品类」的市场。</td></tr>
```
第 75 行（`<tr><td>单份奖励阈值</td>...`）整行替换为（补开关 + 细化）：
```html
            <tr><td>单份奖励阈值（可关闭）</td><td>启用，各档 0.30</td><td>「市场日奖励 ÷ 最低挂单份数」低于阈值就不做；按最低份数所在档（20/50/100/200/250）分别设。可整体关闭，关了就不按单份奖励卡这一关。</td></tr>
            <tr><td>奖励最低份额范围</td><td>1 ~ 250</td><td>只做「最低挂单份数」落在此范围的市场（硬顶 250）。</td></tr>
            <tr><td>区间变量</td><td>累计厚度</td><td>下方多档规则的阈值按哪个量匹配：累计厚度 或 风险系数（见下）。</td></tr>
            <tr><td>金额数值表</td><td>20¢→1 / 25¢→1.5 / 30¢→2</td><td>仅「风险系数」模式用：按价格档给系数；价超此表的档不挂。</td></tr>
```

- [ ] **Step 3: 多档规则段补风险系数 + 金额数值表**

第 83 行（`<li><strong>厚度</strong>…累计厚度…</li>`）整行替换：
```html
        <li><strong>厚度</strong>：某个价位上别人挂单的总量 ÷ 这个市场的最低份数。厚度 ≥ 1 才算合格价位。<strong>累计厚度</strong> = 从买一往下到这一档（含）所有价位厚度之和，盘口越深这个数越大。<br>例：最低份数 20，买一 0.30 挂了 60 张（厚度 3）、0.29 挂了 40 张（厚度 2），那么 0.29 这档的累计厚度 = 3 + 2 = 5。</li>
        <li><strong>区间变量（累计厚度 / 风险系数）</strong>：多档规则的阈值可以按两种量匹配。默认<strong>累计厚度</strong>（上一条）。另一种<strong>风险系数</strong> = 本档厚度 ÷ 金额数值（该档价）；金额数值来自「金额数值表」，按价格档配（价越高数值越大），价超表的档直接不挂。<br>例：金额数值表 20¢→1、25¢→1.5、30¢→2；某档价 0.25、厚度 3，则风险系数 = 3 ÷ 1.5 = 2.0。价越高分母越大，同样厚度算出的系数越低（越贵的价位要更厚的盘口才够格）。</li>
```

- [ ] **Step 4: 校验无 BOM + 中文**

Run:
```bash
cd "C:/Users/Hank/PycharmProjects/poly简单做市"
head -c 3 web/templates/help.html | od -An -tx1   # 应非 ef bb bf
grep -n "做市品类白名单\|风险系数\|奖励最低份额范围" web/templates/help.html
```
Expected: 无 BOM；关键中文命中、无别字。

- [ ] **Step 5: 提交**

```bash
git add web/templates/help.html
git commit -m "docs(help): 翻新使用说明(品类白名单订正+单份奖励开关+风险系数/金额数值表)"
```

---

### Task 4: README 翻新

**Files:**
- Modify: `README.md`（策略参数表 175-189；其后加概念小节）

**Interfaces:** 无（纯文档）。

- [ ] **Step 1: 参数表补行**

`README.md` 第 189 行 `| \`per_share_reward_thresholds\` | 各档 0.30 | 单份奖励按最低份数取档的阈值 |` 整行替换为：
```markdown
| `per_share_reward_thresholds` | 各档 0.30 | 单份奖励按最低份数取档的阈值 |
| `per_share_reward_enabled` | `true` | 单份奖励阈值筛选总开关；`false`=不按单份奖励卡这一关 |
| `rewards_min_size_min` / `rewards_min_size_max` | 1 / 250 | 只做最低下单份数落在此范围的市场（硬顶 250） |
| `tier_match_var` | `cumulative_thickness` | 多档规则阈值按哪个量匹配：`cumulative_thickness`（累计厚度）/ `risk_coefficient`（风险系数） |
| `amount_value_table` | 20¢→1 / 25¢→1.5 / 30¢→2 | 仅风险系数模式用：价格档→系数；价超表的档不挂 |
```

- [ ] **Step 2: 加「几个概念怎么算」小节（带例子）**

`README.md` 第 189 行所在表格结束后、第 191 行 `运行时数据位置 / Runtime data：` 之前，插入：
```markdown

**几个概念怎么算 / Key metrics**

- **单份奖励** = 市场每日 LP 奖励 ÷ 该市场最低下单份数(`rewards_min_size`)，即"每一手最低单每天能拿多少奖励"。例：日奖励池 $60、最低份数 20 → 单份奖励 = 60 / 20 = 3.0；若该档阈值 0.30，则 3.0 ≥ 0.30 通过。可用 `per_share_reward_enabled=false` 整体关闭这道筛选。
- **累计厚度** = 从买一往下累加到该档的厚度之和；单档厚度 = 盘口该价位挂单量 ÷ 最低份数。例：最低份数 20，买一 0.30 挂 60 张(厚度 3)、0.29 挂 40 张(厚度 2) → 0.29 档累计厚度 = 3 + 2 = 5。多档规则「累计厚度 > X → 动作」按此分档。
- **风险系数** = 本档厚度 ÷ 金额数值(该档价)（逐档、非累计）。金额数值表按价格档配系数(价越高系数越大)。例：金额表 20¢→1 / 25¢→1.5 / 30¢→2，某档价 0.25、厚度 3 → 风险系数 = 3 / 1.5 = 2.0；价超金额表最大档 → 该档不挂。价越高分母越大，同样厚度算出的系数越低。
```

- [ ] **Step 3: 校验无 BOM**

Run:
```bash
cd "C:/Users/Hank/PycharmProjects/poly简单做市"
head -c 3 README.md | od -An -tx1   # 应非 ef bb bf
grep -n "per_share_reward_enabled\|几个概念怎么算\|风险系数" README.md
```
Expected: 无 BOM；命中新内容。

- [ ] **Step 4: 提交**

```bash
git add README.md
git commit -m "docs(readme): 参数表补开关/份额范围/区间变量 + 三概念带例子说明"
```

---

### Task 5: 版本号 + 全量回归

**Files:**
- Modify: `version.py`

**Interfaces:** 无。

- [ ] **Step 1: 全量测试**

Run: `pytest -q`
Expected: 全绿（若某处遗漏，按报错修）。

- [ ] **Step 2: 版本号 → 3.1.0**

`version.py` 第 7 行 `__version__ = "3.0.0"` → `__version__ = "3.1.0"`。

- [ ] **Step 3: 提交**

```bash
git add version.py
git commit -m "chore(release): v3.1.0 — 单份奖励阈值开关 + 策略说明翻新"
```

---

## Self-Review

**1. Spec coverage：**
- 单份奖励全局开关（后端）→ Task 1 ✓；（前端 UI + 置灰）→ Task 2 ✓
- 内联小字（单份奖励 / 区间变量）→ Task 2 Step 1/2 ✓
- help.html 订正过时（品类黑名单→白名单）→ Task 3 Step 1/2 ✓
- help.html 补 v4 参数 + 风险系数/金额数值表 → Task 3 Step 2/3 ✓
- README 参数表补行 + 三概念带例子 → Task 4 ✓
- 版本 MINOR → Task 5 ✓

**2. Placeholder scan：** 无 TBD/TODO；每步含实际代码/prose 与命令。

**3. Type consistency：** `per_share_reward_enabled`（bool）在 config.py / scanner `.get(...,True)` / config.html save `.checked` / 文档处处一致；`updatePerShareEnabled()` 定义（Task2 Step3）与调用（onchange、loadStrategy）一致；三概念文案在 help.html 与 README 口径一致（均引 Global Constraints 的定义与例子）。
