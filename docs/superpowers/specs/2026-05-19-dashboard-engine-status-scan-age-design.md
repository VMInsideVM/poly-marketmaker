# 仪表盘"引擎状态" + 扫描时长设计

日期：2026-05-19

## 目标

仪表盘上让用户直接看到：(1) 监控引擎整体是"运行中"还是"已停止"；(2) 上一次扫描距现在过了多久（绝对时间 + 相对"X 分钟前"，且相对值自动往前走）。

## 背景（现状）

- `web/templates/dashboard.html`：`refreshDashboard()` 每 5 秒轮询 `/api/dashboard`（返回 `total_orders/total_positions/total_pnl/wallets[]`，其中每个 `wallet` 含 `running` 布尔）。summary-cards 现有三张卡：总挂单/总持仓/总盈亏。钱包表已有 `运行中/已停止` 标记，用 `<span class="status running|stopped">`。
- 扫描行 `<p id="scan-time">`：`refreshEligible()` 从 `/api/eligible` 取 `last_scan_time`（unix 秒）+ markets，渲染"上次扫描: <绝对时间> (共 N 个)" 或 "尚未扫描"。一个 10 秒轮询仅在 `last_scan_time` **变化**时才调用 `refreshEligible()`——因此相对"X 分钟前"必须由前端自行定时重算，否则不会自动推进。

所需数据均已存在，**无需改后端**。

## 决策（已与用户确认）

- 引擎"运行中"判定：`data.wallets` 中**任一** `running === true` 即"运行中"，否则"已停止"。
- 扫描时长展示：绝对时间 + 相对"X 分钟前"，相对值随仪表盘 5 秒节拍自动推进。
- 仅改 `web/templates/dashboard.html`，不动后端 / `/api/dashboard` / `/api/eligible` / 扫描·监控逻辑 / 总盈亏口径。

## 组件设计（全部在 `web/templates/dashboard.html`）

### 1. 引擎状态卡片

在 `<div class="summary-cards" id="summary">` 内、最前面新增一张卡：
```html
<div class="card"><h3>引擎状态</h3><p id="engine-status">-</p></div>
```
（顺序：引擎状态 · 总挂单 · 总持仓 · 总盈亏）

在 `refreshDashboard()` 的 `.then(data => { ... })` 内，渲染钱包表之前/之后加：
```js
const engineRunning = (data.wallets || []).some(w => w.running);
const es = document.getElementById('engine-status');
es.innerHTML = `<span class="status ${engineRunning ? 'running' : 'stopped'}">${engineRunning ? '运行中' : '已停止'}</span>`;
```
（复用钱包表已有的 `.status.running/.stopped` 样式，无新 CSS。）

### 2. 扫描时长（绝对 + 相对，自动推进）

新增模块级变量：`let lastScanTime = 0; let lastScanCount = 0;`

新增纯函数（格式化相对时长）：
```js
function relTime(sec) {
    if (!sec) return '';
    const d = Math.floor(Date.now() / 1000 - sec);
    if (d < 60) return '刚刚';
    if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
    return Math.floor(d / 3600) + ' 小时前';
}
```

新增渲染函数（用已存值重画扫描行）：
```js
function renderScanLine() {
    const el = document.getElementById('scan-time');
    if (!lastScanTime) { el.textContent = '尚未扫描'; return; }
    const abs = new Date(lastScanTime * 1000).toLocaleString('zh-CN');
    el.textContent = `上次扫描：${abs}（${relTime(lastScanTime)}，共 ${lastScanCount} 个）`;
}
```

修改 `refreshEligible()`：拿到数据后存值并调用 `renderScanLine()`，替换其原先直接写 `scan-time.textContent` 的两处分支：
- 有 `last_scan_time`：`lastScanTime = data.last_scan_time; lastScanCount = (data.markets || []).length; renderScanLine();`
- 无：`lastScanTime = 0; renderScanLine();`（即显示"尚未扫描"）

`pollScanProgress()` 中"扫描中/扫描完成"的实时文案保持原样不动（那是扫描进行中的临时提示，结束后下一次 `refreshEligible()` 会接管为带相对值的稳定展示）；扫描完成分支末尾不需要改。

让相对值自动推进：在现有
```js
setInterval(refreshDashboard, 5000);
```
之后追加：
```js
setInterval(renderScanLine, 5000);
```
（5 秒重算一次；相对值粒度为分钟，足够。）

## 数据流

`refreshDashboard`（5s）→ `/api/dashboard` → 由 `wallets[].running` 计算并渲染引擎状态卡。
`refreshEligible`（启动时 + scan_time 变化时）→ `/api/eligible` → 存 `lastScanTime/lastScanCount` → `renderScanLine()`。
独立 5s `setInterval(renderScanLine)` → 用存值重算相对时长（无网络请求）。

## 错误处理

- `data.wallets` 缺失/为空 → `some(...)` 返回 false → 显示"已停止"（合理：无钱包即未运行）。
- `last_scan_time` 为 0/缺失 → "尚未扫描"。
- `relTime` 永不抛错（纯算术）。

## 测试

- 本功能为纯前端 HTML/JS。本仓库前端模板（`history.html`/`config.html`/`logs.html` 等）一致地无自动化 UI 测试，且未引入前端测试框架；保持一致——不为此新增前端测试框架。
- `relTime` 逻辑足够简单（三档阈值），内联于模板；不抽到 Python 侧（它是浏览器端展示逻辑，无 Python 调用点）。
- 验收靠：`python -c "import web.routes"` 通过、`python -m pytest -q` 仍全绿（不应有任何后端变化导致回归）、人工打开仪表盘核对。

## 不做（YAGNI）

- 不改后端任何路由/逻辑。
- 不修正"总盈亏"口径（本次仅口头解释；如需修正另开需求）。
- 不区分"监控 vs 自动扫描"两个独立指示灯（已选单一"引擎状态"）。
- 不做秒级跳动的相对时间（5 秒粒度足够，分钟级展示）。
- 不加新 CSS（复用现有 `.status` 样式）。
