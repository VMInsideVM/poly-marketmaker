"""tests/test_update.py — 自动更新纯逻辑单测(无网络)。"""

import re
from version import __version__
import hashlib

from web.update import (
    parse_version,
    is_newer,
    parse_release,
    check_update,
    verify_sha256,
    engine_active,
    _State,
    _run_update,
    start_update,
)
import web.update as updater


def test_version_is_semver_string():
    assert isinstance(__version__, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__


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


def _fake_release(tag="v1.0.8", with_exe=True, with_sha=True, body="修复若干问题"):
    assets = []
    if with_exe:
        assets.append(
            {
                "name": "PolymarketMarketMaker_Setup.exe",
                "browser_download_url": "https://example.com/Setup.exe",
                "size": 12345678,
            }
        )
    if with_sha:
        assets.append(
            {
                "name": "PolymarketMarketMaker_Setup.exe.sha256",
                "browser_download_url": "https://example.com/Setup.exe.sha256",
                "size": 70,
            }
        )
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
        out = check_update(
            current="1.0.7", fetch=lambda: _fake_release("v1.0.8", with_exe=False)
        )
        assert out["update_available"] is False

    def test_network_error_is_swallowed(self):
        def boom():
            raise OSError("network down")

        out = check_update(current="1.0.7", fetch=boom)
        assert out["update_available"] is False
        assert out["current"] == "1.0.7"


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
                state,
                self._info(),
                str(tmp_path),
                download=download,
                verify=lambda p, exp: True,
                fetch_sha=lambda url: b"deadbeef  Setup.exe",
                launch=lambda p: launched.append(p),
                shutdown=lambda: shut.append(True),
            )

        run()
        assert state.state == "installing"
        assert launched and launched[0].endswith(
            "PolymarketMarketMaker_Setup_1.0.8.exe"
        )
        assert shut == [True]

    def test_sha_mismatch_aborts(self, tmp_path):
        state = _State()
        launched = []

        def download(url, dest, total, cb):
            with open(dest, "wb") as f:
                f.write(b"x")

        _run_update(
            state,
            self._info(),
            str(tmp_path),
            download=download,
            verify=lambda p, exp: False,  # 校验失败
            fetch_sha=lambda url: b"deadbeef",
            launch=lambda p: launched.append(p),
            shutdown=lambda: launched.append("SHUTDOWN"),
        )
        assert state.state == "error"
        assert "校验" in state.message
        assert launched == []  # 绝不启动安装包、绝不退出进程

    def test_download_exception_sets_error(self, tmp_path):
        state = _State()

        def download(url, dest, total, cb):
            raise OSError("disk full")

        _run_update(
            state,
            self._info(),
            str(tmp_path),
            download=download,
            verify=lambda p, exp: True,
            fetch_sha=lambda url: b"x",
            launch=lambda p: None,
            shutdown=lambda: None,
        )
        assert state.state == "error"


class TestStartUpdate:
    def test_blocked_when_engine_active(self):
        mgr = _FakeManager(engines={"a": _FakeWorker(True)})
        out = start_update(
            mgr,
            info_provider=lambda: {
                "version": "1.0.8",
                "exe_url": "u",
                "sha256_url": "s",
            },
        )
        assert out["ok"] is False
        assert "引擎" in out["message"]

    def test_no_assets_reports_error(self):
        updater.STATE.state = "idle"  # 复位,避免前序测试遗留状态干扰并发闸
        out = start_update(
            None,
            info_provider=lambda: {
                "version": "1.0.8",
                "exe_url": None,
                "sha256_url": None,
            },
        )
        assert out["ok"] is False

    def test_blocked_when_already_in_progress(self, monkeypatch):
        # 已有更新在进行中时,应立即返回 ok=True+"已在进行中",且不再启动新线程
        run_calls = []
        monkeypatch.setattr(updater, "_run_update", lambda *a, **k: run_calls.append(1))
        monkeypatch.setattr(
            updater.threading, "Thread", lambda *a, **k: _ImmediateThread(*a, **k)
        )
        for busy_state in ("downloading", "verifying", "installing"):
            updater.STATE.state = busy_state
            out = start_update(
                None,
                info_provider=lambda: {
                    "version": "1.0.8",
                    "exe_url": "u",
                    "sha256_url": "s",
                },
            )
            assert out["ok"] is True, f"expected ok=True when state={busy_state}"
            assert out.get("message"), f"expected a message when state={busy_state}"
            assert (
                run_calls == []
            ), f"_run_update must NOT be called when state={busy_state}"
        # 复位,避免泄漏到后续测试
        updater.STATE.state = "idle"

    def test_starts_background_and_sets_downloading(self, monkeypatch):
        # 用同步桩替换线程实际执行体,断言入口把状态置为 downloading 并真正调度
        called = {}

        def fake_run(state, info, dest_dir, **deps):
            called["ran"] = True
            called["info"] = info

        monkeypatch.setattr(updater, "_run_update", fake_run)
        # 让线程同步执行,避免竞态
        monkeypatch.setattr(
            updater.threading, "Thread", lambda *a, **k: _ImmediateThread(*a, **k)
        )
        # 复位单例
        updater.STATE.state = "idle"
        out = start_update(
            None,
            info_provider=lambda: {
                "version": "1.0.8",
                "exe_url": "u",
                "sha256_url": "s",
                "exe_size": 1,
            },
        )
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
