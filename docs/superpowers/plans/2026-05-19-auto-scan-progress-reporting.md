# 自动扫描进度上报 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 抽共享 `_scan_with_status()`，使自动扫描（`_do_scan`）和手动扫描（`scan_markets`）都上报 `scan_status`/进度；前端 10s 轮询检到 scanning 即显示进度条。

**Architecture:** 后端新增 `EngineManager._scan_with_status()`（设 scanning→带回调跑 scanner→成功设 done/last_scan_time→失败复位 done 不动 last_scan_time 并 raise）；`scan_markets` 保留 `_scanner_api` 引导后调它并仍 `save_eligible_markets`；`_do_scan` 调它后仍分发给 worker。前端 10s `/api/eligible` 轮询加一句 `if scanning → ensureScanPolling()`。

**Tech Stack:** Python / pytest / unittest.mock；Jinja2 + 原生 JS。

参考 spec：`docs/superpowers/specs/2026-05-19-auto-scan-progress-reporting-design.md`

---

### Task 1: 共享带状态扫描 + 两调用点 + 前端 10s 检测

**Files:**
- Modify: `engine/manager.py` (`scan_markets`, `_do_scan`, 新增 `_scan_with_status`)
- Modify: `web/templates/dashboard.html` (底部 10s 轮询)
- Test: `tests/test_manager.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_manager.py` (file already has `import time`, `import pytest`, `from unittest.mock import MagicMock, patch`, and `_make_manager()`):

```python
class TestSharedScanWithStatus:
    def test_manual_scan_sets_scanning_then_done(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        seen = []

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def scan(self, on_progress=None, on_found=None):
                on_progress(1, 2, "checking")
                seen.append(manager.scan_status)          # during scan
                on_found({"market_id": "m1"})
                return [{"market_id": "m1"}]

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager.scan_markets()

        assert seen == ["scanning"]
        assert manager.scan_status == "done"
        assert manager.last_scan_time > 0
        assert manager.eligible_markets == [{"market_id": "m1"}]
        db.save_eligible_markets.assert_called_once_with([{"market_id": "m1"}])

    def test_auto_do_scan_reports_status_and_distributes(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        worker = MagicMock()
        worker.running = True
        manager.engines = {"0xABC": worker}
        seen = []

        class FakeScanner:
            def __init__(self, api, db, addr):
                pass

            def scan(self, on_progress=None, on_found=None):
                on_progress(3, 3, "done-ish")
                seen.append(manager.scan_status)
                return [{"market_id": "m9"}]

        with patch("engine.manager.MarketScanner", FakeScanner):
            manager._do_scan()

        assert seen == ["scanning"]
        assert manager.scan_status == "done"
        assert manager.last_scan_time > 0
        worker.place_orders.assert_called_once_with([{"market_id": "m9"}])

    def test_scan_failure_resets_status_and_keeps_last_scan_time(self):
        manager, db = _make_manager()
        manager._scanner_api = MagicMock()
        manager.last_scan_time = 12345.0

        class BoomScanner:
            def __init__(self, api, db, addr):
                pass

            def scan(self, on_progress=None, on_found=None):
                raise RuntimeError("scanner blew up")

        with patch("engine.manager.MarketScanner", BoomScanner):
            with pytest.raises(RuntimeError):
                manager._scan_with_status()

        assert manager.scan_status == "done"        # not stuck on "scanning"
        assert manager.last_scan_time == 12345.0     # unchanged (round failed)
```

- [ ] **Step 2: Run the tests to verify they FAIL**

Run: `python -m pytest tests/test_manager.py::TestSharedScanWithStatus -v`
Expected: FAIL — `manager._scan_with_status` does not exist (`AttributeError`); `_do_scan` currently doesn't set `scan_status`, so `test_auto_do_scan_reports_status_and_distributes` fails its `seen == ["scanning"]` / `scan_status == "done"` assertions.

- [ ] **Step 3: Add `_scan_with_status()` to `EngineManager`**

In `engine/manager.py`, add this method immediately BEFORE `def _do_scan(self):`:

```python
    def _scan_with_status(self) -> list:
        """Run one scan, reporting scan_status/progress; shared by manual and
        auto paths. On success sets eligible_markets/last_scan_time and
        scan_status='done' and returns the eligible list. On failure resets
        scan_status to 'done' (never left 'scanning') WITHOUT touching
        last_scan_time (a failed round did not complete), then re-raises."""
        import time as _time

        self.scan_status = "scanning"
        self.scan_progress = "Starting..."
        self.scan_checked = 0
        self.scan_total = 0
        self.eligible_markets = []

        def on_progress(checked, total, message):
            self.scan_checked = checked
            self.scan_total = total
            self.scan_progress = message

        def on_found(entry):
            self.eligible_markets.append(entry)

        try:
            scanner = MarketScanner(self._scanner_api, self.db, "")
            eligible = scanner.scan(on_progress=on_progress, on_found=on_found)
        except Exception:
            self.scan_status = "done"  # not 'scanning': progress bar won't stick
            raise
        self.eligible_markets = eligible
        self.last_scan_time = _time.time()
        self.scan_status = "done"
        self.scan_progress = f"Done: {len(eligible)} eligible"
        logger.info("Scanner found %d eligible markets", len(eligible))
        return eligible
```

- [ ] **Step 4: Rewrite `scan_markets()` to use it**

Current `scan_markets()` body (lines ~241-288) — keep the docstring and the `_scanner_api` bootstrap block exactly (the `if not self._scanner_api:` … through the `logger.info("Created scanner API …")` line). REPLACE everything from `import time as _time` (line ~262) through `logger.info("Scanner found %d eligible markets", len(eligible))` (line ~284) with a single call that captures the return, and KEEP the existing persist tail. The resulting method after the bootstrap block becomes:

```python
        eligible = self._scan_with_status()

        # Persist to database (replace old data)
        self.db.save_eligible_markets(eligible)
        logger.info("Saved %d eligible markets to database", len(eligible))
```

(I.e. `scan_markets` = unchanged bootstrap + `eligible = self._scan_with_status()` + the unchanged `save_eligible_markets` lines. Note: `scan_markets` MUST capture the return value because `save_eligible_markets(eligible)` needs it — this corrects the spec's parenthetical "不需要返回值".)

- [ ] **Step 5: Rewrite `_do_scan()` to use it**

Replace the entire current `_do_scan()`:
```python
    def _do_scan(self):
        """Run one scan cycle: find eligible markets, distribute to wallets."""
        import time as _time

        # Use shared API for market scanning (no wallet-specific data needed)
        scanner = MarketScanner(self._scanner_api, self.db, "")
        eligible = scanner.scan()
        self.eligible_markets = eligible
        self.last_scan_time = _time.time()
        logger.info("Scanner found %d eligible markets", len(eligible))

        # Distribute to each running wallet
        for address, worker in self.engines.items():
            if worker.running:
                try:
                    worker.place_orders(eligible)
                except Exception as e:
                    logger.error("Error distributing to wallet %s: %s", address, e)
```
with:
```python
    def _do_scan(self):
        """Run one scan cycle: find eligible markets, distribute to wallets."""
        eligible = self._scan_with_status()

        # Distribute to each running wallet
        for address, worker in self.engines.items():
            if worker.running:
                try:
                    worker.place_orders(eligible)
                except Exception as e:
                    logger.error("Error distributing to wallet %s: %s", address, e)
```
Do NOT change `_scanner_loop` (its `try/except` around `_do_scan()` still catches the re-raised scanner error and logs it).

- [ ] **Step 6: Run backend tests**

Run: `python -m pytest tests/test_manager.py -v`
Expected: PASS — `TestSharedScanWithStatus` (3) plus all pre-existing `test_manager` tests (incl. `TestScanMarketsLastScanTime`) green.

- [ ] **Step 7: Run full suite**

Run: `python -m pytest -q`
Expected: all pass (prior count + 3).

- [ ] **Step 8: Frontend — 10s poll resumes progress when scanning**

In `web/templates/dashboard.html`, the bottom 10s poll is currently:
```js
let lastKnownScanTime = 0;
setInterval(() => {
    fetch('/api/eligible').then(r => r.json()).then(data => {
        if (data.last_scan_time && data.last_scan_time !== lastKnownScanTime) {
            lastKnownScanTime = data.last_scan_time;
            refreshEligible();
        }
    });
}, 10000);
```
Change to (add ONE line as the first statement inside `.then`):
```js
let lastKnownScanTime = 0;
setInterval(() => {
    fetch('/api/eligible').then(r => r.json()).then(data => {
        if (data.scan_status === 'scanning') ensureScanPolling();
        if (data.last_scan_time && data.last_scan_time !== lastKnownScanTime) {
            lastKnownScanTime = data.last_scan_time;
            refreshEligible();
        }
    });
}, 10000);
```
(`ensureScanPolling` already exists & is idempotent — defined in the prior feature. No other JS change.)

- [ ] **Step 9: Verify frontend wiring + suite**

Run: `python -c "import web.routes"`
Expected: exit 0, no output.

Run: `python -m pytest -q`
Expected: all pass (same count as Step 7 — no backend change here).

- [ ] **Step 10: Commit**

```bash
git add engine/manager.py tests/test_manager.py web/templates/dashboard.html
git commit -m "feat: shared _scan_with_status so auto scan reports progress like manual"
```
Commit message MUST end with footer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

- [ ] **Step 11: Confirm commit scope**

Run: `git show --stat HEAD`
Expected: only `engine/manager.py`, `tests/test_manager.py`, `web/templates/dashboard.html`.

---

## Self-Review

**Spec coverage:**
- `_scan_with_status()`（scanning→回调→成功 done/last_scan_time→失败 done 不动 last_scan_time + raise）→ Step 3 ✓
- `scan_markets` 保留 bootstrap + 调用 + 保留 `save_eligible_markets` → Step 4 ✓（修正 spec 括注：必须 `eligible = self._scan_with_status()` 以供持久化）
- `_do_scan` 调用 + 保留分发 → Step 5 ✓
- `_scanner_loop` 不动（仍捕获 raise）→ Step 5 备注 ✓
- 前端 10s 轮询加 `scan_status==='scanning' → ensureScanPolling()` → Step 8 ✓
- 失败语义（status 复位 done、last_scan_time 不变）→ Step 1 `test_scan_failure_*` + Step 3 ✓
- 测试：手动 scanning/done、自动 status+分发、失败复位 → Step 1 三个用例 ✓；既有 `TestScanMarketsLastScanTime` 等不回归 → Step 6/7 ✓
- 不改 scanner.py / /api/eligible 结构 / 渲染逻辑 / last_scan_time 仅整轮成功才更新 → 计划未触及 ✓

**Placeholder scan:** 无 TBD/TODO；每步含完整代码与精确命令、预期输出。计数以 `pytest -q` 实际为准。

**Type/name consistency:** `_scan_with_status() -> list`（Step 3 定义）被 `scan_markets`（Step 4）与 `_do_scan`（Step 5）以 `eligible = self._scan_with_status()` 调用；`save_eligible_markets(eligible)`、`worker.place_orders(eligible)` 均消费返回的列表；前端 `ensureScanPolling`（既有）与 `scan_status`/`/api/eligible` 字段一致；测试 `FakeScanner.scan(on_progress, on_found)` 签名与 `_scan_with_status` 内 `scanner.scan(on_progress=on_progress, on_found=on_found)` 一致。
