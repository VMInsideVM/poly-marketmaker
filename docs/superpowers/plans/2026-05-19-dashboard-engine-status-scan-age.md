# 仪表盘"引擎状态" + 扫描时长 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 仪表盘新增"引擎状态"卡片，并把扫描行改为"绝对时间 +（X 分钟前）"且相对值自动推进。

**Architecture:** 纯前端，仅改 `web/templates/dashboard.html`。引擎状态由 `/api/dashboard` 已返回的 `wallets[].running` 在 `refreshDashboard()` 内派生；扫描时长用 `/api/eligible` 已返回的 `last_scan_time` 存入模块级变量，由独立 5 秒 `setInterval` 重算相对值。无后端改动。

**Tech Stack:** Jinja2 模板 / 原生 JS。

参考 spec：`docs/superpowers/specs/2026-05-19-dashboard-engine-status-scan-age-design.md`

---

### Task 1: 引擎状态卡片 + 扫描时长（dashboard.html）

**Files:**
- Modify: `web/templates/dashboard.html`

- [ ] **Step 1: 加引擎状态卡片到 summary-cards**

`web/templates/dashboard.html` 当前为：
```html
<div class="summary-cards" id="summary">
    <div class="card"><h3>总挂单</h3><p id="total-orders">-</p></div>
    <div class="card"><h3>总持仓</h3><p id="total-positions">-</p></div>
    <div class="card"><h3>总盈亏</h3><p id="total-pnl">-</p></div>
</div>
```
改为（在最前面插入一张卡）：
```html
<div class="summary-cards" id="summary">
    <div class="card"><h3>引擎状态</h3><p id="engine-status">-</p></div>
    <div class="card"><h3>总挂单</h3><p id="total-orders">-</p></div>
    <div class="card"><h3>总持仓</h3><p id="total-positions">-</p></div>
    <div class="card"><h3>总盈亏</h3><p id="total-pnl">-</p></div>
</div>
```

- [ ] **Step 2: 在 refreshDashboard 渲染引擎状态**

当前 `refreshDashboard()` 开头为：
```js
function refreshDashboard() {
    fetch('/api/dashboard').then(r => r.json()).then(data => {
        document.getElementById('total-orders').textContent = data.total_orders;
        document.getElementById('total-positions').textContent = data.total_positions;
        document.getElementById('total-pnl').textContent = (data.total_pnl || 0).toFixed(2) + ' pUSD';
        const tbody = document.getElementById('wallet-body');
```
在 `total-pnl` 那行之后、`const tbody = ...` 之前插入两行：
```js
        document.getElementById('total-pnl').textContent = (data.total_pnl || 0).toFixed(2) + ' pUSD';
        const engineRunning = (data.wallets || []).some(w => w.running);
        document.getElementById('engine-status').innerHTML =
            `<span class="status ${engineRunning ? 'running' : 'stopped'}">${engineRunning ? '运行中' : '已停止'}</span>`;
        const tbody = document.getElementById('wallet-body');
```
（复用钱包表已有的 `.status.running/.stopped` 样式，不加新 CSS。）

- [ ] **Step 3: 加 relTime + renderScanLine + 模块级变量**

在 `<script>` 内、`function refreshDashboard()` 之前（紧跟 `<script>` 起始处）加入：
```js
let lastScanTime = 0;
let lastScanCount = 0;

function relTime(sec) {
    if (!sec) return '';
    const d = Math.floor(Date.now() / 1000 - sec);
    if (d < 60) return '刚刚';
    if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
    return Math.floor(d / 3600) + ' 小时前';
}

function renderScanLine() {
    const el = document.getElementById('scan-time');
    if (!lastScanTime) { el.textContent = '尚未扫描'; return; }
    const abs = new Date(lastScanTime * 1000).toLocaleString('zh-CN');
    el.textContent = `上次扫描：${abs}（${relTime(lastScanTime)}，共 ${lastScanCount} 个）`;
}
```
(注意：现有代码已有一个 `let lastKnownScanTime = 0;` 在文件底部 setInterval 区域 — 那是另一个变量，**不要**与新加的 `lastScanTime` 混淆，也不要删它。)

- [ ] **Step 4: 改 refreshEligible 用 renderScanLine**

当前 `refreshEligible()` 为：
```js
function refreshEligible() {
    fetch('/api/eligible').then(r => r.json()).then(data => {
        const markets = data.markets || [];
        const scanTime = data.last_scan_time;
        if (scanTime) {
            document.getElementById('scan-time').textContent =
                `上次扫描: ${new Date(scanTime * 1000).toLocaleString('zh-CN')} (共 ${markets.length} 个)`;
        } else {
            document.getElementById('scan-time').textContent = '尚未扫描';
        }
        eligibleData = markets;
        renderEligibleTable();
    });
}
```
改为：
```js
function refreshEligible() {
    fetch('/api/eligible').then(r => r.json()).then(data => {
        const markets = data.markets || [];
        const scanTime = data.last_scan_time;
        if (scanTime) {
            lastScanTime = scanTime;
            lastScanCount = markets.length;
        } else {
            lastScanTime = 0;
        }
        renderScanLine();
        eligibleData = markets;
        renderEligibleTable();
    });
}
```

- [ ] **Step 5: 加 renderScanLine 的 5 秒自动推进**

文件底部当前为：
```js
refreshDashboard();
refreshEligible();
setInterval(refreshDashboard, 5000);
```
改为（追加一行 setInterval；其余不动）：
```js
refreshDashboard();
refreshEligible();
setInterval(refreshDashboard, 5000);
setInterval(renderScanLine, 5000);
```
（其后的 `let lastKnownScanTime = 0;` 及 10 秒 eligible 变化轮询保持原样不动。`pollScanProgress()` 中"扫描中/扫描完成"的临时文案也保持原样——扫描完成后 10 秒轮询检测到 `last_scan_time` 变化会调用 `refreshEligible()`，由 `renderScanLine()` 接管为带相对值的稳定展示。)

- [ ] **Step 6: 校验后端无回归 + 模板可加载**

Run: `python -c "import web.routes"`
Expected: 退出码 0，无输出。

Run: `python -m pytest -q`
Expected: 全绿（本改动无后端变化，计数与改前一致）。

- [ ] **Step 7: 提交（仅此文件；工作树有无关未跟踪/已改文件 — 用显式路径，绝不 `git add -A`/`.`）**

```bash
git add web/templates/dashboard.html
git commit -m "feat: dashboard 引擎状态 card + scan-age relative time"
```
提交信息结尾须有 footer：`Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

- [ ] **Step 8: 确认提交范围**

Run: `git show --stat HEAD`
Expected: 仅 `web/templates/dashboard.html`。

---

## Self-Review

**Spec coverage:**
- 引擎状态卡片 + 任一 wallet running 即"运行中" → Step 1-2 ✓
- 扫描行绝对+相对、未扫描"尚未扫描"、相对自动推进 → Step 3-5 ✓
- 复用 `.status` 样式不加新 CSS → Step 2 ✓
- 不改后端/路由/扫描·监控逻辑/总盈亏 → 计划仅改 dashboard.html ✓
- 无前端测试框架先例，靠 import + pytest 全绿 + 人工核对 → Step 6 ✓

**Placeholder scan:** 无 TBD/TODO；每步含完整代码与精确命令、预期输出。

**Type/name consistency:** `lastScanTime`/`lastScanCount`/`relTime`/`renderScanLine`/`engine-status`(id)/`engineRunning` 在 Step 1-5 定义与使用一致；明确与既有 `lastKnownScanTime` 区分、不删除既有 10s 轮询与 `pollScanProgress`。`#engine-status` 元素（Step 1）由 Step 2 写入；`#scan-time` 既有元素由 `renderScanLine` 复用。
