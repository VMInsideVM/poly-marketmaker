# 扫描时间语义 + 跨页续进度 + 进度条 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `last_scan_time` 仅整轮扫描完成才更新；仪表盘切回后据后端 `scan_status` 续上进度；扫描中显示可视化 `<progress>` 进度条。

**Architecture:** Task 1 后端一行删除（`scan_markets` 的 `on_found` 不再刷新时间）+ 单元测试；Task 2 纯前端 `dashboard.html`：新增 `#scan-progress` 元素与 `ensureScanPolling()`，`pollScanProgress` 改为渲染进度条到独立元素，`refreshEligible` 检测 scanning 续轮询，移除上一轮的 renderScanLine 守卫。

**Tech Stack:** Python / pytest / unittest.mock；Jinja2 + 原生 JS `<progress>`。

参考 spec：`docs/superpowers/specs/2026-05-19-scan-time-resume-progressbar-design.md`

---

### Task 1: 后端——last_scan_time 仅整轮完成才更新

**Files:**
- Modify: `engine/manager.py` (`scan_markets`)
- Test: `tests/test_manager.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_manager.py` (the file already has `from unittest.mock import MagicMock, patch` and a `_make_manager()` helper returning `(manager, db)`):

```python
class TestScanMarketsLastScanTime:
    def test_last_scan_time_only_updates_at_round_completion(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()  # skip API-construction branch

        observed = []

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def scan(self, on_progress=None, on_found=None):
                on_found({"market_id": "m1"})
                observed.append(manager.last_scan_time)
                on_found({"market_id": "m2"})
                observed.append(manager.last_scan_time)
                return [{"market_id": "m1"}, {"market_id": "m2"}]

        assert manager.last_scan_time == 0
        with patch("engine.manager.MarketScanner", FakeScanner):
            manager.scan_markets()

        # during the scan (right after each on_found) the timestamp stayed 0
        assert observed == [0, 0]
        # only the completed round set it
        assert manager.last_scan_time > 0
        assert manager.scan_status == "done"
        assert manager.eligible_markets == [{"market_id": "m1"},
                                            {"market_id": "m2"}]
```

- [ ] **Step 2: Run the test to verify it FAILS**

Run: `python -m pytest tests/test_manager.py::TestScanMarketsLastScanTime -v`
Expected: FAIL — `assert observed == [0, 0]` fails because the current `on_found` sets `self.last_scan_time = _time.time()`, so `observed` contains non-zero timestamps.

- [ ] **Step 3: Remove the time write from `on_found`**

In `engine/manager.py` `scan_markets()`, the current code is:
```python
        def on_found(entry):
            self.eligible_markets.append(entry)
            self.last_scan_time = _time.time()
```
Change to:
```python
        def on_found(entry):
            self.eligible_markets.append(entry)
```
Leave everything else in `scan_markets()` unchanged — in particular keep, after `eligible = scanner.scan(...)`:
```python
        self.eligible_markets = eligible
        self.last_scan_time = _time.time()
        self.scan_status = "done"
        self.scan_progress = f"Done: {len(eligible)} eligible"
```
Do NOT touch `_do_scan()` (auto scanner loop — already only sets `last_scan_time` after `scanner.scan()` returns).

- [ ] **Step 4: Run the test to verify it PASSES**

Run: `python -m pytest tests/test_manager.py::TestScanMarketsLastScanTime -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `python -m pytest -q`
Expected: all pass (prior count + 1).

- [ ] **Step 6: Commit**

```bash
git add engine/manager.py tests/test_manager.py
git commit -m "fix: last_scan_time updates only at scan-round completion, not per market found"
```
Commit message MUST end with footer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

### Task 2: 前端——续进度 + 进度条 + 分元素

**Files:**
- Modify: `web/templates/dashboard.html`

(Read the file first to match exact text. Current relevant code is shown below per step.)

- [ ] **Step 1: Add the `#scan-progress` element under `#scan-time`**

Current (lines ~45-47):
```html
<h2>符合条件的市场 (Eligible Markets)</h2>
<p id="scan-time" style="color:#888; font-size:13px;"></p>
<table class="data-table" id="eligible-table">
```
Change to:
```html
<h2>符合条件的市场 (Eligible Markets)</h2>
<p id="scan-time" style="color:#888; font-size:13px;"></p>
<div id="scan-progress" style="display:none; color:#888; font-size:13px; margin-bottom:8px;"></div>
<table class="data-table" id="eligible-table">
```

- [ ] **Step 2: Remove the guard in `renderScanLine`**

Current:
```js
function renderScanLine() {
    if (scanPollTimer) return;  // scan in progress: let pollScanProgress own #scan-time
    const el = document.getElementById('scan-time');
    if (!lastScanTime) { el.textContent = '尚未扫描'; return; }
    const abs = new Date(lastScanTime * 1000).toLocaleString('zh-CN');
    el.textContent = `上次扫描：${abs}（${relTime(lastScanTime)}，共 ${lastScanCount} 个）`;
}
```
Change to (delete the guard line only):
```js
function renderScanLine() {
    const el = document.getElementById('scan-time');
    if (!lastScanTime) { el.textContent = '尚未扫描'; return; }
    const abs = new Date(lastScanTime * 1000).toLocaleString('zh-CN');
    el.textContent = `上次扫描：${abs}（${relTime(lastScanTime)}，共 ${lastScanCount} 个）`;
}
```

- [ ] **Step 3: Add `ensureScanPolling()` and simplify `scanMarkets()`**

Current:
```js
function scanMarkets() {
    document.getElementById('scan-time').textContent = '正在扫描...';
    fetch('/api/engine/scan', {method: 'POST'});
    // Start polling for progress while scan runs in background
    if (scanPollTimer) clearInterval(scanPollTimer);
    scanPollTimer = setInterval(pollScanProgress, 2000);
}
```
Change to:
```js
function ensureScanPolling() {
    if (scanPollTimer) return;
    scanPollTimer = setInterval(pollScanProgress, 2000);
    pollScanProgress();
}

function scanMarkets() {
    fetch('/api/engine/scan', {method: 'POST'});
    ensureScanPolling();
}
```
(`scanPollTimer` is the existing global `let scanPollTimer = null;` — do not redeclare it.)

- [ ] **Step 4: Rewrite `pollScanProgress()` to drive `#scan-progress`**

Current:
```js
function pollScanProgress() {
    fetch('/api/eligible').then(r => r.json()).then(data => {
        const status = data.scan_status || 'idle';
        const progress = data.scan_progress || '';
        const checked = data.scan_checked || 0;
        const total = data.scan_total || 0;
        const markets = data.markets || [];

        if (status === 'scanning') {
            document.getElementById('scan-time').textContent =
                `扫描中 [${checked}/${total}] ${progress} (已找到 ${markets.length} 个)`;
        } else if (status === 'done') {
            document.getElementById('scan-time').textContent =
                `扫描完成: ${new Date(data.last_scan_time * 1000).toLocaleString('zh-CN')} (共 ${markets.length} 个)`;
            if (scanPollTimer) { clearInterval(scanPollTimer); scanPollTimer = null; }
        }

        eligibleData = markets;
        renderEligibleTable();
    });
}
```
Change to:
```js
function pollScanProgress() {
    fetch('/api/eligible').then(r => r.json()).then(data => {
        const status = data.scan_status || 'idle';
        const checked = data.scan_checked || 0;
        const total = data.scan_total || 0;
        const markets = data.markets || [];
        const box = document.getElementById('scan-progress');

        if (status === 'scanning') {
            const bar = total > 0
                ? `<progress value="${checked}" max="${total}"></progress>`
                : `<progress></progress>`;
            box.innerHTML = total > 0
                ? `扫描中 ${bar} ${checked}/${total}，已找到 ${markets.length} 个`
                : `正在初筛… ${bar} 已找到 ${markets.length} 个`;
            box.style.display = 'block';
        } else {
            box.style.display = 'none';
            box.innerHTML = '';
            if (status === 'done' && scanPollTimer) {
                clearInterval(scanPollTimer);
                scanPollTimer = null;
            }
        }

        eligibleData = markets;
        renderEligibleTable();
    });
}
```
(`#scan-time` is no longer touched here — it is owned solely by `renderScanLine()`.)

- [ ] **Step 5: `refreshEligible()` resumes polling when backend is scanning**

Current:
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
            lastScanCount = 0;
        }
        renderScanLine();
        eligibleData = markets;
        renderEligibleTable();
    });
}
```
Change to (add the scanning check before rendering the table):
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
            lastScanCount = 0;
        }
        renderScanLine();
        if (data.scan_status === 'scanning') ensureScanPolling();
        eligibleData = markets;
        renderEligibleTable();
    });
}
```

- [ ] **Step 6: Verify backend imports + suite unaffected**

Run: `python -c "import web.routes"`
Expected: exit 0, no output.

Run: `python -m pytest -q`
Expected: all pass (same count as end of Task 1 — no backend change in this task).

- [ ] **Step 7: Static sanity grep**

Run: `grep -n "scan-progress\|ensureScanPolling\|if (scanPollTimer) return" web/templates/dashboard.html`
Expected: shows the new `#scan-progress` element + `ensureScanPolling` definition and its two call sites (`scanMarkets`, `refreshEligible`); the OLD `renderScanLine` guard line `if (scanPollTimer) return;  // scan in progress` is GONE. (`ensureScanPolling` itself contains `if (scanPollTimer) return;` — that is expected and distinct from the removed `renderScanLine` guard comment.)

- [ ] **Step 8: Commit**

```bash
git add web/templates/dashboard.html
git commit -m "feat: scan progress bar + resume progress across page navigation"
```
Footer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

## Self-Review

**Spec coverage:**
- A `scan_markets.on_found` 去掉 last_scan_time、保留整轮完成那次、不动 `_do_scan` → Task 1 ✓（含单元测试断言 on_found 期间不变、整轮后变、status=done）
- B `ensureScanPolling` + `refreshEligible` 检测 scanning + `scanMarkets` 复用 → Task 2 Step 3/5 ✓
- C 原生 `<progress>`（total>0 确定 / =0 不确定）+ 旁文字、仅 scanning 显示、独立 `#scan-progress` 元素、done 隐藏并停轮询 → Task 2 Step 1/4 ✓
- 分元素 + 移除上轮 `renderScanLine` 守卫 → Task 2 Step 2/4 ✓
- 不改 `_do_scan`、`/api/eligible` 结构、`lastKnownScanTime`/10s 轮询、引擎状态卡 → 计划未触及 ✓
- 前端无 UI 测试惯例 → Task 2 靠 import + pytest + grep ✓

**Placeholder scan:** 无 TBD/TODO；每步含完整前后代码块与精确命令、预期输出。pytest 计数以实际 `-q` 0 失败为准。

**Type/name consistency:** `ensureScanPolling`/`scanPollTimer`(既有全局，不重复声明)/`#scan-progress`/`pollScanProgress`/`renderScanLine`/`refreshEligible` 跨步骤一致；`pollScanProgress` 不再写 `#scan-time`，`renderScanLine` 独占 `#scan-time`，二者元素不交叉；Task 1 仅删 `on_found` 内一行、保留 282 行整轮赋值，测试 `FakeScanner.scan(on_progress, on_found)` 签名与 `scan_markets` 调用 `scanner.scan(on_progress=on_progress, on_found=on_found)` 一致。
