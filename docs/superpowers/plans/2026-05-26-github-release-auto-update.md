# GitHub Release 自动更新 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 程序每次启动时检测 GitHub 最新 Release,弹窗询问用户是否更新;同意后下载安装包、校验 SHA-256、静默安装并自动重启;同时提供本地一键发布脚本把新版 setup.exe 发到 GitHub Release。

**Architecture:** 复用现有 PyInstaller + Inno Setup 打包链。新增纯逻辑模块 `web/update.py`(版本比较、release JSON 解析、下载、SHA-256 校验、状态机、安全闸),全部副作用(网络/启动安装包/退出进程)以可注入依赖方式实现,便于离线单测。Flask 暴露三个**免登录**端点(检测/应用/状态),前端用一个被 `base.html`/`login.html`/`setup.html` 共享 include 的弹窗片段驱动。发布侧:`version.py` 作为版本号单一来源,`build_installer.ps1` 注入版本,`release.ps1` 构建+算哈希+打 tag+`gh release create --generate-notes`。

**Tech Stack:** Python 3 标准库(`urllib`、`hashlib`、`subprocess`、`threading`)、Flask、Jinja2、原生 JS/CSS、PowerShell、Inno Setup、GitHub CLI(`gh`)。**不新增第三方依赖。**

设计文档:`docs/superpowers/specs/2026-05-26-github-release-auto-update-design.md`

---

## 文件结构

- **新增** `version.py` — 版本号单一来源 `__version__`。
- **新增** `web/update.py` — 更新逻辑:版本比较、release 解析、下载、SHA-256 校验、状态单例、安全闸、orchestration。
- **新增** `tests/test_update.py` — 上述纯逻辑 + 状态机(注入 fake)的单测。
- **新增** `web/templates/_update_modal.html` — 弹窗标记 + JS 片段。
- **改** `web/routes.py` — 注册 `/api/update/check|apply|status`(免登录),`import` 更新模块。
- **改** `web/templates/base.html`、`login.html`、`setup.html` — `{% include %}` 弹窗片段。
- **改** `web/static/style.css` — 弹窗 + 进度条样式。
- **改** `PolymarketMarketMaker.iss` — `#ifndef` 版本守卫、`[Run]` 静默重启项、`CloseApplications=yes`。
- **改** `build_installer.ps1` — 从 `version.py` 读版本并 `/D` 注入 ISCC。
- **新增** `release.ps1` — 构建 + 算 SHA-256 + 打 tag + `gh release create`。

---

## Task 1: 版本号单一来源 `version.py`

**Files:**
- Create: `version.py`
- Test: `tests/test_update.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_update.py`:

```python
"""tests/test_update.py — 自动更新纯逻辑单测(无网络)。"""

import re
from version import __version__


def test_version_is_semver_string():
    assert isinstance(__version__, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_update.py::test_version_is_semver_string -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'version'`

- [ ] **Step 3: 写最小实现**

创建 `version.py`:

```python
"""version.py — 应用版本号的唯一来源。

发版只改这里;build_installer.ps1 读取它注入 Inno Setup,release.ps1 用它打 git tag,
运行时 web/update.py 用它与 GitHub 最新 Release 比对。
"""

__version__ = "1.0.7"
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_update.py::test_version_is_semver_string -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add version.py tests/test_update.py
git commit -m "feat: version.py 作为版本号单一来源"
```

---

## Task 2: 版本比较 `parse_version` / `is_newer`

**Files:**
- Create: `web/update.py`
- Test: `tests/test_update.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_update.py` 顶部 import 行后追加:

```python
from web.update import parse_version, is_newer


class TestParseVersion:
    def test_plain(self):
        assert parse_version("1.0.8") == (1, 0, 8)

    def test_v_prefix(self):
        assert parse_version("v1.0.8") == (1, 0, 8)
        assert parse_version("V2.10.3") == (2, 10, 3)

    def test_whitespace(self):
        assert parse_version("  1.2.3  ") == (1, 2, 3)

    def test_unparseable_returns_none(self):
        assert parse_version("latest") is None
        assert parse_version("") is None
        assert parse_version("1.x.0") is None


class TestIsNewer:
    def test_higher(self):
        assert is_newer("1.0.8", "1.0.7") is True
        assert is_newer("1.1.0", "1.0.9") is True
        assert is_newer("2.0.0", "1.9.9") is True

    def test_equal_or_lower(self):
        assert is_newer("1.0.7", "1.0.7") is False
        assert is_newer("1.0.6", "1.0.7") is False

    def test_v_prefix_mixed(self):
        assert is_newer("v1.0.8", "1.0.7") is True

    def test_unparseable_is_not_newer(self):
        assert is_newer("latest", "1.0.7") is False
        assert is_newer("1.0.8", "garbage") is False
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_update.py -k "ParseVersion or IsNewer" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'web.update'`

- [ ] **Step 3: 写最小实现**

创建 `web/update.py`:

```python
"""web/update.py — GitHub Release 自动更新:检测/下载/校验/安装。

副作用(网络、启动安装包、退出进程)都以可注入依赖实现,便于离线单测。
"""

import hashlib
import json
import logging
import os
import subprocess
import threading
import urllib.request

from config import DATA_DIR
from version import __version__

logger = logging.getLogger(__name__)

REPO = "VMInsideVM/poly-marketmaker"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
_UPDATE_DIR = os.path.join(DATA_DIR, "update")
_UA = {"User-Agent": "PolymarketMarketMaker"}


def parse_version(tag):
    """'v1.0.8' / '1.0.8' -> (1, 0, 8);无法解析 -> None。"""
    s = (tag or "").strip().lstrip("vV")
    if not s:
        return None
    try:
        return tuple(int(p) for p in s.split("."))
    except ValueError:
        return None


def is_newer(latest, current):
    """latest 是否严格高于 current;任一无法解析则视为不更新。"""
    lv, cv = parse_version(latest), parse_version(current)
    if lv is None or cv is None:
        return False
    return lv > cv
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_update.py -k "ParseVersion or IsNewer" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add web/update.py tests/test_update.py
git commit -m "feat: 版本号 semver 解析与比较"
```

---

## Task 3: 解析 GitHub Release JSON `parse_release`

**Files:**
- Modify: `web/update.py`
- Test: `tests/test_update.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_update.py` 追加(同时更新顶部 import 为 `from web.update import parse_version, is_newer, parse_release`):

```python
def _fake_release(tag="v1.0.8", with_exe=True, with_sha=True, body="修复若干问题"):
    assets = []
    if with_exe:
        assets.append({
            "name": "PolymarketMarketMaker_Setup.exe",
            "browser_download_url": "https://example.com/Setup.exe",
            "size": 12345678,
        })
    if with_sha:
        assets.append({
            "name": "PolymarketMarketMaker_Setup.exe.sha256",
            "browser_download_url": "https://example.com/Setup.exe.sha256",
            "size": 70,
        })
    return {"tag_name": tag, "body": body, "assets": assets}


class TestParseRelease:
    def test_full(self):
        info = parse_release(_fake_release())
        assert info["tag"] == "v1.0.8"
        assert info["version"] == "1.0.8"
        assert info["notes"] == "修复若干问题"
        assert info["exe_url"] == "https://example.com/Setup.exe"
        assert info["exe_size"] == 12345678
        assert info["sha256_url"] == "https://example.com/Setup.exe.sha256"

    def test_missing_exe(self):
        info = parse_release(_fake_release(with_exe=False))
        assert info["exe_url"] is None
        assert info["exe_size"] is None

    def test_missing_sha(self):
        info = parse_release(_fake_release(with_sha=False))
        assert info["sha256_url"] is None

    def test_empty_release(self):
        info = parse_release({})
        assert info["exe_url"] is None
        assert info["sha256_url"] is None
        assert info["notes"] == ""
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_update.py -k ParseRelease -v`
Expected: FAIL — `ImportError: cannot import name 'parse_release'`

- [ ] **Step 3: 写最小实现**

在 `web/update.py` 的 `is_newer` 之后追加:

```python
def parse_release(rel):
    """从 GitHub release JSON 取出更新所需字段。资源缺失则对应字段为 None。"""
    tag = rel.get("tag_name", "") or ""
    assets = rel.get("assets", []) or []

    def _find(suffix):
        for a in assets:
            if (a.get("name", "") or "").lower().endswith(suffix):
                return a
        return None

    exe = _find(".exe")
    sha = _find(".sha256")
    return {
        "tag": tag,
        "version": tag.lstrip("vV"),
        "notes": rel.get("body", "") or "",
        "exe_url": exe.get("browser_download_url") if exe else None,
        "exe_size": exe.get("size") if exe else None,
        "sha256_url": sha.get("browser_download_url") if sha else None,
    }
```

注意:`_find(".exe")` 会先于 `.sha256` 匹配,但因为 `.sha256` 不以 `.exe` 结尾、`.exe` 不以 `.sha256` 结尾,两者互斥,顺序无碍。

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_update.py -k ParseRelease -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add web/update.py tests/test_update.py
git commit -m "feat: 解析 GitHub release JSON 取 exe/sha256 资源"
```

---

## Task 4: 检测更新 `check_update`(非阻塞,可注入 fetch)

**Files:**
- Modify: `web/update.py`
- Test: `tests/test_update.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_update.py` 追加(更新 import 增加 `check_update`):

```python
class TestCheckUpdate:
    def test_update_available(self):
        out = check_update(current="1.0.7", fetch=lambda: _fake_release("v1.0.8"))
        assert out["update_available"] is True
        assert out["current"] == "1.0.7"
        assert out["latest"] == "1.0.8"
        assert out["notes"] == "修复若干问题"
        assert out["size"] == 12345678

    def test_same_version_no_update(self):
        out = check_update(current="1.0.8", fetch=lambda: _fake_release("v1.0.8"))
        assert out["update_available"] is False

    def test_no_exe_asset_no_update(self):
        out = check_update(current="1.0.7",
                           fetch=lambda: _fake_release("v1.0.8", with_exe=False))
        assert out["update_available"] is False

    def test_network_error_is_swallowed(self):
        def boom():
            raise OSError("network down")
        out = check_update(current="1.0.7", fetch=boom)
        assert out["update_available"] is False
        assert out["current"] == "1.0.7"
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_update.py -k CheckUpdate -v`
Expected: FAIL — `ImportError: cannot import name 'check_update'`

- [ ] **Step 3: 写最小实现**

在 `web/update.py` 的 `parse_release` 之后追加:

```python
def _http_get(url, timeout=30):
    """GET 原始字节;带 UA。供下载 .sha256 与 fetch JSON 复用。"""
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch_latest_release():
    """拉取并解析 GitHub releases/latest 为 dict;5 秒超时。"""
    return json.loads(_http_get(LATEST_URL, timeout=5).decode("utf-8"))


def check_update(current=__version__, fetch=None):
    """检测最新版本。永不抛异常:任何失败都返回 update_available=False。

    fetch: 无参函数,返回 GitHub release JSON dict(测试可注入)。
    """
    fetch = fetch or _fetch_latest_release
    try:
        info = parse_release(fetch())
        available = bool(info["exe_url"]) and is_newer(info["version"], current)
        return {
            "update_available": available,
            "current": current,
            "latest": info["version"],
            "notes": info["notes"],
            "size": info["exe_size"],
        }
    except Exception as e:  # noqa: BLE001 — 检测必须非阻塞
        logger.warning("更新检测失败: %s", e)
        return {"update_available": False, "current": current}
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_update.py -k CheckUpdate -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add web/update.py tests/test_update.py
git commit -m "feat: check_update 非阻塞检测最新 Release"
```

---

## Task 5: SHA-256 校验 `verify_sha256`

**Files:**
- Modify: `web/update.py`
- Test: `tests/test_update.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_update.py` 追加(更新 import 增加 `verify_sha256`;文件顶部 import 增加 `import hashlib`):

```python
class TestVerifySha256:
    def test_match(self, tmp_path):
        f = tmp_path / "blob.bin"
        data = b"hello polymarket"
        f.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        assert verify_sha256(str(f), digest) is True

    def test_match_case_insensitive_and_whitespace(self, tmp_path):
        f = tmp_path / "blob.bin"
        data = b"abc"
        f.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest().upper()
        assert verify_sha256(str(f), "  " + digest + "  ") is True

    def test_mismatch(self, tmp_path):
        f = tmp_path / "blob.bin"
        f.write_bytes(b"abc")
        assert verify_sha256(str(f), "0" * 64) is False
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_update.py -k VerifySha256 -v`
Expected: FAIL — `ImportError: cannot import name 'verify_sha256'`

- [ ] **Step 3: 写最小实现**

在 `web/update.py` 的 `check_update` 之后追加:

```python
def verify_sha256(path, expected):
    """文件实算 SHA-256 是否等于 expected(忽略大小写与首尾空白)。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().lower() == (expected or "").strip().lower()
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_update.py -k VerifySha256 -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add web/update.py tests/test_update.py
git commit -m "feat: verify_sha256 文件哈希校验"
```

---

## Task 6: 安全闸 `engine_active`

**Files:**
- Modify: `web/update.py`
- Test: `tests/test_update.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_update.py` 追加(更新 import 增加 `engine_active`):

```python
class _FakeWorker:
    def __init__(self, running):
        self.running = running


class _FakeManager:
    def __init__(self, engines=None, scanner_thread=None, scan_status="idle"):
        self.engines = engines or {}
        self._scanner_thread = scanner_thread
        self.scan_status = scan_status


class TestEngineActive:
    def test_no_manager(self):
        assert engine_active(None) is False

    def test_all_idle(self):
        mgr = _FakeManager(engines={"a": _FakeWorker(False)})
        assert engine_active(mgr) is False

    def test_worker_running(self):
        mgr = _FakeManager(engines={"a": _FakeWorker(False), "b": _FakeWorker(True)})
        assert engine_active(mgr) is True

    def test_scanner_thread_alive(self):
        mgr = _FakeManager(scanner_thread=object())
        assert engine_active(mgr) is True

    def test_scanning_status(self):
        mgr = _FakeManager(scan_status="scanning")
        assert engine_active(mgr) is True
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_update.py -k EngineActive -v`
Expected: FAIL — `ImportError: cannot import name 'engine_active'`

- [ ] **Step 3: 写最小实现**

在 `web/update.py` 的 `verify_sha256` 之后追加:

```python
def engine_active(mgr):
    """是否有引擎/扫描在跑 —— 更新会中断做市并使持仓失去止损保护,故此时拒绝更新。"""
    if mgr is None:
        return False
    if getattr(mgr, "_scanner_thread", None) is not None:
        return True
    if getattr(mgr, "scan_status", "") == "scanning":
        return True
    return any(getattr(w, "running", False)
               for w in getattr(mgr, "engines", {}).values())
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_update.py -k EngineActive -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add web/update.py tests/test_update.py
git commit -m "feat: engine_active 安全闸(引擎运行时禁更新)"
```

---

## Task 7: 更新状态机与编排 `_State` / `_run_update`

**Files:**
- Modify: `web/update.py`
- Test: `tests/test_update.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_update.py` 追加(更新 import 增加 `_State, _run_update`):

```python
class TestRunUpdate:
    def _info(self):
        return {
            "version": "1.0.8",
            "exe_url": "https://example.com/Setup.exe",
            "sha256_url": "https://example.com/Setup.exe.sha256",
            "exe_size": 100,
        }

    def test_happy_path(self, tmp_path):
        state = _State()
        seen = []
        launched = []
        shut = []

        def download(url, dest, total, cb):
            cb(50)
            cb(100)
            with open(dest, "wb") as f:
                f.write(b"installer-bytes")
            seen.append(("download", dest))

        def run():
            _run_update(
                state, self._info(), str(tmp_path),
                download=download,
                verify=lambda p, exp: True,
                fetch_sha=lambda url: b"deadbeef  Setup.exe",
                launch=lambda p: launched.append(p),
                shutdown=lambda: shut.append(True),
            )

        run()
        assert state.state == "installing"
        assert launched and launched[0].endswith("PolymarketMarketMaker_Setup_1.0.8.exe")
        assert shut == [True]

    def test_sha_mismatch_aborts(self, tmp_path):
        state = _State()
        launched = []

        def download(url, dest, total, cb):
            with open(dest, "wb") as f:
                f.write(b"x")

        _run_update(
            state, self._info(), str(tmp_path),
            download=download,
            verify=lambda p, exp: False,   # 校验失败
            fetch_sha=lambda url: b"deadbeef",
            launch=lambda p: launched.append(p),
            shutdown=lambda: launched.append("SHUTDOWN"),
        )
        assert state.state == "error"
        assert "校验" in state.message
        assert launched == []   # 绝不启动安装包、绝不退出进程

    def test_download_exception_sets_error(self, tmp_path):
        state = _State()

        def download(url, dest, total, cb):
            raise OSError("disk full")

        _run_update(
            state, self._info(), str(tmp_path),
            download=download,
            verify=lambda p, exp: True,
            fetch_sha=lambda url: b"x",
            launch=lambda p: None,
            shutdown=lambda: None,
        )
        assert state.state == "error"
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_update.py -k RunUpdate -v`
Expected: FAIL — `ImportError: cannot import name '_State'`

- [ ] **Step 3: 写最小实现**

在 `web/update.py` 的 `engine_active` 之后追加:

```python
class _State:
    """更新进度状态(单进程单用户,模块级单例即可)。"""

    def __init__(self):
        self.state = "idle"  # idle|downloading|verifying|installing|error
        self.percent = 0
        self.message = ""

    def snapshot(self):
        return {"state": self.state, "percent": self.percent, "message": self.message}


STATE = _State()


def _run_update(state, info, dest_dir, *, download, verify, fetch_sha, launch, shutdown):
    """下载→校验→静默安装→退出。所有副作用经参数注入,便于测试。

    校验失败或任何异常 -> state=error,且绝不启动安装包、绝不退出进程。
    """
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir,
                            f"PolymarketMarketMaker_Setup_{info['version']}.exe")

        state.state, state.percent, state.message = "downloading", 0, ""
        download(info["exe_url"], dest, info.get("exe_size"),
                 lambda p: setattr(state, "percent", p))

        state.state = "verifying"
        expected = fetch_sha(info["sha256_url"]).decode("utf-8").strip().split()[0]
        if not verify(dest, expected):
            state.state = "error"
            state.message = "校验失败(SHA-256 不匹配),已取消更新"
            try:
                os.remove(dest)
            except OSError:
                pass
            return

        state.state = "installing"
        launch(dest)
        shutdown()
    except Exception as e:  # noqa: BLE001
        logger.exception("更新失败")
        state.state = "error"
        state.message = f"更新失败:{e}"
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_update.py -k RunUpdate -v`
Expected: PASS(3 个用例)

- [ ] **Step 5: 提交**

```bash
git add web/update.py tests/test_update.py
git commit -m "feat: 更新状态机与编排(校验失败即中止,不启动安装包)"
```

---

## Task 8: 生产副作用 + `start_update` 入口

**Files:**
- Modify: `web/update.py`
- Test: `tests/test_update.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_update.py` 追加(更新 import 增加 `start_update`;复用 Task 6 的 `_FakeManager`/`_FakeWorker`):

```python
import web.update as updater


class TestStartUpdate:
    def test_blocked_when_engine_active(self):
        mgr = _FakeManager(engines={"a": _FakeWorker(True)})
        out = start_update(mgr, info_provider=lambda: {
            "version": "1.0.8", "exe_url": "u", "sha256_url": "s"})
        assert out["ok"] is False
        assert "引擎" in out["message"]

    def test_no_assets_reports_error(self):
        updater.STATE.state = "idle"  # 复位,避免前序测试遗留状态干扰并发闸
        out = start_update(None, info_provider=lambda: {
            "version": "1.0.8", "exe_url": None, "sha256_url": None})
        assert out["ok"] is False

    def test_starts_background_and_sets_downloading(self, monkeypatch):
        # 用同步桩替换线程实际执行体,断言入口把状态置为 downloading 并真正调度
        called = {}

        def fake_run(state, info, dest_dir, **deps):
            called["ran"] = True
            called["info"] = info

        monkeypatch.setattr(updater, "_run_update", fake_run)
        # 让线程同步执行,避免竞态
        monkeypatch.setattr(updater.threading, "Thread",
                            lambda *a, **k: _ImmediateThread(*a, **k))
        # 复位单例
        updater.STATE.state = "idle"
        out = start_update(None, info_provider=lambda: {
            "version": "1.0.8", "exe_url": "u", "sha256_url": "s", "exe_size": 1})
        assert out["ok"] is True
        assert called.get("ran") is True


class _ImmediateThread:
    """假线程:start() 即同步调用 target。"""
    def __init__(self, *a, **k):
        self._target = k.get("target")
        self._args = k.get("args", ())
        self._kwargs = k.get("kwargs", {})

    def start(self):
        self._target(*self._args, **self._kwargs)
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_update.py -k StartUpdate -v`
Expected: FAIL — `ImportError: cannot import name 'start_update'`

- [ ] **Step 3: 写最小实现**

在 `web/update.py` 的 `_run_update` 之后追加:

```python
def _download(url, dest, total, progress_cb):
    """流式下载到 dest,按已下载/总字节回调百分比(0-100)。"""
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        size = total or int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if size:
                progress_cb(min(100, int(done * 100 / size)))


def _launch_installer(path):
    """以 detached 方式静默启动安装包,使其在本进程退出后存活。"""
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
        creationflags=flags,
        close_fds=True,
    )


def _shutdown_self():
    """立即退出进程,释放 exe 文件锁,交由安装包覆盖并重启。"""
    logger.info("为安装更新而退出进程")
    os._exit(0)


def start_update(mgr, *, info_provider=None, **deps):
    """供路由调用的入口:做安全闸/并发检查,然后后台线程执行 _run_update。"""
    if engine_active(mgr):
        return {"ok": False,
                "message": "引擎正在运行,更新会中断做市并使持仓失去止损保护,"
                           "请先停止引擎再更新。"}
    if STATE.state in ("downloading", "verifying", "installing"):
        return {"ok": True, "message": "更新已在进行中"}

    info = (info_provider or (lambda: parse_release(_fetch_latest_release())))()
    if not info or not info.get("exe_url") or not info.get("sha256_url"):
        return {"ok": False, "message": "未找到可用的更新包(缺少 exe 或 sha256 资源)"}

    deps.setdefault("download", _download)
    deps.setdefault("verify", verify_sha256)
    deps.setdefault("fetch_sha", _http_get)
    deps.setdefault("launch", _launch_installer)
    deps.setdefault("shutdown", _shutdown_self)

    STATE.state, STATE.percent, STATE.message = "downloading", 0, ""
    threading.Thread(
        target=_run_update,
        args=(STATE, info, _UPDATE_DIR),
        kwargs=deps,
        daemon=True,
    ).start()
    return {"ok": True}
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_update.py -k StartUpdate -v`
Expected: PASS

- [ ] **Step 5: 全量回归**

Run: `pytest tests/test_update.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add web/update.py tests/test_update.py
git commit -m "feat: start_update 入口(安全闸+并发检查+后台执行)与生产副作用"
```

---

## Task 9: Flask 端点(免登录)

**Files:**
- Modify: `web/routes.py`
- Test: `tests/test_update_routes.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_update_routes.py`:

```python
"""tests/test_update_routes.py — 更新端点(免登录 + 安全闸)。"""

import web.routes as routes
import web.update as updater


def _client():
    routes.app.config["TESTING"] = True
    return routes.app.test_client()


def test_check_is_public_and_returns_json(monkeypatch):
    monkeypatch.setattr(updater, "check_update",
                        lambda: {"update_available": False, "current": "1.0.7"})
    # 关键:不登录也能访问
    resp = _client().get("/api/update/check")
    assert resp.status_code == 200
    assert resp.get_json()["current"] == "1.0.7"


def test_apply_blocked_returns_409(monkeypatch):
    monkeypatch.setattr(updater, "start_update",
                        lambda mgr: {"ok": False, "message": "引擎正在运行"})
    resp = _client().post("/api/update/apply")
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False


def test_apply_ok_returns_200(monkeypatch):
    monkeypatch.setattr(updater, "start_update", lambda mgr: {"ok": True})
    resp = _client().post("/api/update/apply")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_status_returns_snapshot():
    updater.STATE.state = "downloading"
    updater.STATE.percent = 42
    resp = _client().get("/api/update/status")
    body = resp.get_json()
    assert body["state"] == "downloading"
    assert body["percent"] == 42
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_update_routes.py -v`
Expected: FAIL — check 返回 302(被 `login` 重定向)或 404(端点不存在)

- [ ] **Step 3: 写最小实现**

在 `web/routes.py` 顶部 import 区(`from config import ...` 之后)加:

```python
from web import update as updater
```

在文件末尾(`api_dashboard` 之后)追加:

```python
# --- API: 自动更新(免登录:启动时弹窗在登录前出现) ---


@app.route("/api/update/check", methods=["GET"])
def api_update_check():
    return jsonify(updater.check_update())


@app.route("/api/update/apply", methods=["POST"])
def api_update_apply():
    result = updater.start_update(manager)
    return jsonify(result), (200 if result.get("ok") else 409)


@app.route("/api/update/status", methods=["GET"])
def api_update_status():
    return jsonify(updater.STATE.snapshot())
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_update_routes.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add web/routes.py tests/test_update_routes.py
git commit -m "feat: /api/update/check|apply|status 免登录端点"
```

---

## Task 10: 弹窗片段 `_update_modal.html`

**Files:**
- Create: `web/templates/_update_modal.html`

- [ ] **Step 1: 创建片段**

创建 `web/templates/_update_modal.html`:

```html
{# 自动更新弹窗:页面加载时检测,有新版则询问;同意后显示下载进度。 #}
<div id="update-modal" class="update-modal" style="display:none">
  <div class="update-box">
    <h2 id="update-title">发现新版本</h2>
    <pre id="update-notes" class="update-notes"></pre>
    <div id="update-progress-wrap" style="display:none">
      <div class="update-bar"><div id="update-bar-fill"></div></div>
      <p id="update-progress-text">准备中…</p>
    </div>
    <p id="update-error" class="update-error" style="display:none"></p>
    <div id="update-actions">
      <button id="update-yes" class="btn btn-primary" type="button">是,现在更新</button>
      <button id="update-no" class="btn" type="button">稍后</button>
    </div>
  </div>
</div>
<script>
(function () {
  var modal = document.getElementById('update-modal');
  if (!modal) return;
  var $ = function (id) { return document.getElementById(id); };

  fetch('/api/update/check').then(function (r) { return r.json(); }).then(function (d) {
    if (!d.update_available) return;
    $('update-title').textContent = '发现新版本 v' + d.latest + '(当前 v' + d.current + ')';
    $('update-notes').textContent = d.notes || '';
    modal.style.display = 'flex';
  }).catch(function () {});

  $('update-no').onclick = function () { modal.style.display = 'none'; };

  $('update-yes').onclick = function () {
    $('update-actions').style.display = 'none';
    $('update-error').style.display = 'none';
    $('update-progress-wrap').style.display = 'block';
    setProgress(0, '准备中…');
    fetch('/api/update/apply', { method: 'POST' })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (x) {
        if (!x.ok) { showError(x.d.message || '更新失败'); return; }
        poll();
      })
      .catch(function () { showError('无法启动更新,请稍后重试'); });
  };

  function setProgress(pct, text) {
    $('update-bar-fill').style.width = (pct || 0) + '%';
    $('update-progress-text').textContent = text;
  }

  function showError(msg) {
    $('update-progress-wrap').style.display = 'none';
    $('update-actions').style.display = 'block';
    var e = $('update-error');
    e.textContent = msg;
    e.style.display = 'block';
  }

  var INSTALL_MSG = '正在安装并自动重启,请稍候,几秒后程序会自动重新打开。';

  function poll() {
    fetch('/api/update/status').then(function (r) { return r.json(); }).then(function (s) {
      if (s.state === 'downloading') setProgress(s.percent, '正在下载… ' + (s.percent || 0) + '%');
      else if (s.state === 'verifying') setProgress(100, '正在校验…');
      else if (s.state === 'installing') setProgress(100, INSTALL_MSG);
      else if (s.state === 'error') { showError(s.message || '更新失败'); return; }
      setTimeout(poll, 800);
    }).catch(function () {
      // 连接断开 = 服务端已退出、进入安装阶段(预期现象)
      setProgress(100, INSTALL_MSG);
      setTimeout(poll, 2000);
    });
  }
})();
</script>
```

- [ ] **Step 2: 提交**

```bash
git add web/templates/_update_modal.html
git commit -m "feat: 自动更新弹窗片段(检测/进度/错误)"
```

---

## Task 11: 三个页面 include 弹窗 + 样式

**Files:**
- Modify: `web/templates/base.html`
- Modify: `web/templates/login.html`
- Modify: `web/templates/setup.html`
- Modify: `web/static/style.css`

- [ ] **Step 1: base.html include**

把 `web/templates/base.html` 的:

```html
    <main class="container">
```

改为:

```html
    {% include "_update_modal.html" %}
    <main class="container">
```

- [ ] **Step 2: login.html include**

把 `web/templates/login.html` 的 `</body>` 行改为:

```html
    {% include "_update_modal.html" %}
</body>
```

- [ ] **Step 3: setup.html include**

把 `web/templates/setup.html` 的 `</body>` 行改为:

```html
    {% include "_update_modal.html" %}
</body>
```

- [ ] **Step 4: 追加样式**

在 `web/static/style.css` 末尾追加:

```css
/* --- 自动更新弹窗 --- */
.update-modal {
  position: fixed; inset: 0; z-index: 1000;
  display: flex; align-items: center; justify-content: center;
  background: rgba(0, 0, 0, 0.5);
}
.update-box {
  background: #fff; border-radius: 8px; padding: 24px;
  width: min(520px, 90vw); box-shadow: 0 8px 32px rgba(0, 0, 0, 0.25);
}
.update-box h2 { margin: 0 0 12px; font-size: 18px; }
.update-notes {
  max-height: 200px; overflow: auto; white-space: pre-wrap;
  background: #f5f5f5; border-radius: 4px; padding: 10px;
  font-size: 13px; margin: 0 0 16px;
}
.update-bar {
  height: 14px; background: #e5e7eb; border-radius: 7px; overflow: hidden;
}
#update-bar-fill {
  height: 100%; width: 0; background: #2563eb; transition: width 0.3s ease;
}
#update-progress-text { font-size: 13px; color: #374151; margin: 8px 0 0; }
.update-error { color: #b91c1c; font-size: 13px; margin: 8px 0 0; }
#update-actions { display: flex; gap: 12px; justify-content: flex-end; margin-top: 16px; }
```

- [ ] **Step 5: 手动验证(开发模式)**

Run: `python app.py`
- 用浏览器打开 `http://127.0.0.1:8000/login`。
- 打开浏览器开发者工具 Network,确认有 `GET /api/update/check` 请求且返回 JSON。
- 因当前 `version.py`=1.0.7 且线上若无更高 Release,弹窗不出现属正常。临时验证弹窗:可在浏览器控制台手动跑
  `document.getElementById('update-modal').style.display='flex'` 看样式是否正常。
- Ctrl+C 关闭。

- [ ] **Step 6: 提交**

```bash
git add web/templates/base.html web/templates/login.html web/templates/setup.html web/static/style.css
git commit -m "feat: 三个页面 include 自动更新弹窗 + 样式"
```

---

## Task 12: Inno Setup 脚本调整

**Files:**
- Modify: `PolymarketMarketMaker.iss`

**注意:`.iss` 必须保持 UTF-8 with BOM 编码(否则中文乱码)。用编辑工具修改时保留 BOM。**

- [ ] **Step 1: 版本号改用 `#ifndef` 守卫**

把:

```
#define MyAppName "Polymarket 做市助手"
#define MyAppVersion "1.0.7"
```

改为:

```
#define MyAppName "Polymarket 做市助手"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
```

(版本号将由 `build_installer.ps1` 通过 `ISCC /DMyAppVersion=<ver>` 注入;命令行未传时回落到 0.0.0。)

- [ ] **Step 2: `[Setup]` 增加 CloseApplications**

在 `[Setup]` 段的 `UninstallDisplayIcon=...` 行之后加一行:

```
CloseApplications=yes
```

- [ ] **Step 3: `[Run]` 增加静默重启项**

把 `[Run]` 段:

```
[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动程序"; Flags: nowait postinstall skipifsilent
```

改为:

```
[Run]
; 交互式首装:勾选框"立即启动"
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动程序"; Flags: nowait postinstall skipifsilent
; 静默(自动更新)安装:装完自动拉起程序
Filename: "{app}\{#MyAppExeName}"; Flags: nowait runasoriginaluser; Check: WizardSilent
```

(交互安装时 `WizardSilent` 为 false、第二条跳过;静默更新时第一条因 `skipifsilent` 跳过、第二条执行 —— 二者互斥,不会双启动。)

- [ ] **Step 4: 提交**

```bash
git add PolymarketMarketMaker.iss
git commit -m "feat: .iss 版本守卫 + 静默重启 + CloseApplications"
```

---

## Task 13: build_installer.ps1 注入版本号

**Files:**
- Modify: `build_installer.ps1`

- [ ] **Step 1: 读版本并注入 ISCC**

把 `build_installer.ps1` 的:

```powershell
Write-Host "[2/2] Inno Setup: building installer ..." -ForegroundColor Cyan
$iscc = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { $iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $iscc)) { throw "ISCC.exe not found. Install Inno Setup 6 (winget install -e --id JRSoftware.InnoSetup)." }
& $iscc "$root\PolymarketMarketMaker.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compile failed" }
```

改为:

```powershell
Write-Host "[2/2] Inno Setup: building installer ..." -ForegroundColor Cyan
$ver = (& python -c "import version; print(version.__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $ver) { throw "无法从 version.py 读取版本号" }
Write-Host "    版本号: $ver" -ForegroundColor DarkGray
$iscc = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { $iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $iscc)) { throw "ISCC.exe not found. Install Inno Setup 6 (winget install -e --id JRSoftware.InnoSetup)." }
& $iscc "/DMyAppVersion=$ver" "$root\PolymarketMarketMaker.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compile failed" }
```

- [ ] **Step 2: 手动验证构建**

Run: `powershell -ExecutionPolicy Bypass -File build_installer.ps1`
Expected:
- 控制台打印 `版本号: 1.0.7`。
- 生成 `installer\PolymarketMarketMaker_Setup.exe`。
- (可选)在"添加/删除程序"或安装包属性里确认版本为 1.0.7,而非 0.0.0。

- [ ] **Step 3: 提交**

```bash
git add build_installer.ps1
git commit -m "feat: build_installer 从 version.py 注入版本号"
```

---

## Task 14: release.ps1 一键发布

**Files:**
- Create: `release.ps1`

- [ ] **Step 1: 创建发布脚本**

创建 `release.ps1`:

```powershell
# release.ps1 — 一键发布:构建 -> 算 SHA-256 -> 打 tag -> gh release create。
# 用法:先改 version.py 的版本号,再运行:
#   powershell -ExecutionPolicy Bypass -File release.ps1
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

# 1) 检查 gh CLI 已安装且已登录
$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
  throw "未找到 gh CLI。请先安装:winget install --id GitHub.cli  然后运行 gh auth login"
}
& gh auth status
if ($LASTEXITCODE -ne 0) { throw "gh 未登录。请运行: gh auth login" }

# 2) 读版本号
$ver = (& python -c "import version; print(version.__version__)").Trim()
if (-not $ver) { throw "无法从 version.py 读取版本号" }
$tag = "v$ver"
Write-Host "准备发布 $tag" -ForegroundColor Cyan

# 3) tag 不能已存在(避免覆盖已发布版本)
& git rev-parse $tag 2>$null
if ($LASTEXITCODE -eq 0) {
  throw "tag $tag 已存在。请先在 version.py 提升版本号再发布。"
}

# 4) 构建安装包(build_installer.ps1 会用 version.py 注入版本)
& "$root\build_installer.ps1"
if ($LASTEXITCODE -ne 0) { throw "构建失败" }

$setup = "$root\installer\PolymarketMarketMaker_Setup.exe"
if (-not (Test-Path $setup)) { throw "未找到安装包: $setup" }

# 5) 算 SHA-256 写同名 .sha256(纯 hash,UTF-8 无 BOM)
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
$shaFile = "$setup.sha256"
[System.IO.File]::WriteAllText($shaFile, $hash)
Write-Host "SHA-256: $hash" -ForegroundColor DarkGray

# 6) 打 tag 并推送
& git tag $tag
& git push origin $tag
if ($LASTEXITCODE -ne 0) { throw "git push tag 失败" }

# 7) 创建 Release 并上传 setup.exe + .sha256(发布说明自动生成)
& gh release create $tag $setup $shaFile --title $tag --generate-notes
if ($LASTEXITCODE -ne 0) { throw "gh release create 失败" }

Write-Host ""
Write-Host "已发布 $tag,资源已上传到 GitHub Release。" -ForegroundColor Green
```

- [ ] **Step 2: 静态自检(不真正发布)**

Run: `powershell -ExecutionPolicy Bypass -Command "Get-Command gh -ErrorAction SilentlyContinue; python -c 'import version; print(version.__version__)'"`
Expected:
- 若 `gh` 未装,提示安装(说明脚本第 1 步会正确拦截)。
- 打印版本号 `1.0.7`。
- 不要真正运行 `release.ps1` 直到准备好首次正式发版。

- [ ] **Step 3: 提交**

```bash
git add release.ps1
git commit -m "feat: release.ps1 一键发布到 GitHub Release"
```

---

## Task 15: 端到端联调(手动,首次发版)

**Files:** 无(纯验证)

> 这一步需要真正发一个测试版本,验证"检测→弹窗→下载→校验→静默安装→重启"完整闭环。建议用一个比当前高的小版本号试跑一次。

- [ ] **Step 1: 发首个 Release**

- 确认本地 `version.py` 与线上一致(如 1.0.7),正常运行一次 `release.ps1` 发布 `v1.0.7`(作为基线)。
- 把 `version.py` 改为 `1.0.8`,`git commit`,再 `release.ps1` 发布 `v1.0.8`。

- [ ] **Step 2: 装旧版跑、验证更新**

- 在干净环境(或先卸载)安装 `v1.0.7` 的 setup.exe。
- 启动程序 → 浏览器打开 → 应弹出"发现新版本 v1.0.8"。
- 点"是" → 看到下载进度条 → 校验 → "正在安装并自动重启" → 程序自动重启为 v1.0.8。

- [ ] **Step 3: 用 exe 自身日志确认(避免端口误判)**

参考打包验证坑:不要只看端口响应。用
`Start-Process "<安装目录>\MarketMaker.exe" -RedirectStandardError err.log` 或直接看
`%LOCALAPPDATA%\PolymarketMarketMaker\market_maker.log`,确认无更新相关异常、重启后版本正确。

- [ ] **Step 4: 故障路径抽查**

- 断网启动:`/api/update/check` 失败,程序正常运行、无弹窗、无报错(检测非阻塞)。
- (可选)手动改坏线上 `.sha256` 内容再试一次,确认校验失败时弹窗显示"校验失败"且**不**执行安装。

---

## 自检清单(实现完成后核对)

- [ ] spec 每节都有对应 Task:版本单一来源(T1/T13)、检测端点(T4/T9)、弹窗(T10/T11)、应用更新(T7/T8/T9)、安装包重启(T12)、发布脚本(T14)、安全/失败处理(T4/T6/T7)、SmartScreen(T8 用 urllib+CreateProcess,无需额外代码)。
- [ ] 无占位符:所有步骤含完整代码/命令。
- [ ] 类型/命名一致:`STATE`/`_State.snapshot()`、`start_update(mgr, ...)`、`_run_update(state, info, dest_dir, *, download, verify, fetch_sha, launch, shutdown)`、`engine_active(mgr)`、`parse_release` 返回键(`exe_url`/`exe_size`/`sha256_url`/`version`/`notes`)在各 Task 中一致。
- [ ] `.iss` 两条 `[Run]` 互斥;`#ifndef` 守卫与 `/D` 注入配套。
```
