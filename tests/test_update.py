"""tests/test_update.py — 自动更新纯逻辑单测(无网络)。"""

import os
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
    _install_mac,
    _platform_launcher,
    _launch_installer,
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


def _fake_release(
    tag="v1.0.8", with_exe=True, with_dmg=True, with_sha=True, body="修复若干问题"
):
    """构造一个 release JSON。默认同时含 Windows(.exe) 与 macOS(.dmg) 两套包,
    每套各带自己的 .sha256(模拟 v1.0.15 起的双平台 release)。"""
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
    if with_dmg:
        assets.append(
            {
                "name": "PolymarketMarketMaker_Mac_arm64.dmg",
                "browser_download_url": "https://example.com/Mac.dmg",
                "size": 29502630,
            }
        )
        if with_sha:
            assets.append(
                {
                    "name": "PolymarketMarketMaker_Mac_arm64.dmg.sha256",
                    "browser_download_url": "https://example.com/Mac.dmg.sha256",
                    "size": 65,
                }
            )
    return {"tag_name": tag, "body": body, "assets": assets}


class TestParseRelease:
    def test_windows_picks_exe(self):
        info = parse_release(_fake_release(), system="win32")
        assert info["tag"] == "v1.0.8"
        assert info["version"] == "1.0.8"
        assert info["notes"] == "修复若干问题"
        assert info["pkg_name"] == "PolymarketMarketMaker_Setup.exe"
        assert info["pkg_url"] == "https://example.com/Setup.exe"
        assert info["pkg_size"] == 12345678
        # sha256 必须配对到 exe 的那个,而不是 dmg 的
        assert info["sha256_url"] == "https://example.com/Setup.exe.sha256"

    def test_mac_picks_dmg(self):
        info = parse_release(_fake_release(), system="darwin")
        assert info["pkg_name"] == "PolymarketMarketMaker_Mac_arm64.dmg"
        assert info["pkg_url"] == "https://example.com/Mac.dmg"
        assert info["pkg_size"] == 29502630
        # sha256 必须配对到 dmg 的那个,而不是 exe 的(防止取错导致校验失败)
        assert info["sha256_url"] == "https://example.com/Mac.dmg.sha256"

    def test_sha_paired_not_first(self):
        # 资源顺序里 dmg.sha256 在 exe.sha256 之前时,windows 仍须取 exe.sha256
        rel = _fake_release()
        rel["assets"].reverse()
        info = parse_release(rel, system="win32")
        assert info["sha256_url"] == "https://example.com/Setup.exe.sha256"

    def test_missing_platform_package(self):
        # mac-only release,Windows 客户端取不到 .exe -> 无包
        info = parse_release(_fake_release(with_exe=False), system="win32")
        assert info["pkg_url"] is None
        assert info["pkg_size"] is None

    def test_single_package_sha_fallback(self):
        # 老版本只有一个包+一个 .sha256(名字不配对)时,回退到任意 .sha256
        rel = {
            "tag_name": "v1.0.8",
            "body": "x",
            "assets": [
                {"name": "Setup.exe", "browser_download_url": "u", "size": 1},
                {"name": "checksums.sha256", "browser_download_url": "s", "size": 1},
            ],
        }
        info = parse_release(rel, system="win32")
        assert info["pkg_url"] == "u"
        assert info["sha256_url"] == "s"

    def test_empty_release(self):
        info = parse_release({}, system="win32")
        assert info["pkg_url"] is None
        assert info["sha256_url"] is None
        assert info["notes"] == ""


class TestCheckUpdate:
    def setup_method(self):
        updater._reset_cache()  # 模块级缓存会跨用例残留,每个用例前清空

    def test_update_available(self, monkeypatch):
        # check_update 内部按 sys.platform 选包(不透传 system 参数),固定为
        # win32 避免在 macOS 上误选 fixture 里的 .dmg 资源导致断言的 size 对不上。
        monkeypatch.setattr(updater.sys, "platform", "win32")
        out = check_update(current="1.0.7", fetch=lambda: _fake_release("v1.0.8"))
        assert out["update_available"] is True
        assert out["current"] == "1.0.7"
        assert out["latest"] == "1.0.8"
        assert out["notes"] == "修复若干问题"
        assert out["size"] == 12345678

    def test_same_version_no_update(self):
        out = check_update(current="1.0.8", fetch=lambda: _fake_release("v1.0.8"))
        assert out["update_available"] is False

    def test_no_exe_asset_no_update(self, monkeypatch):
        # 同上:固定 win32,否则 mac 上仍带 .dmg 的 fixture 会被判定为"有更新"。
        monkeypatch.setattr(updater.sys, "platform", "win32")
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


class TestCheckUpdateCache:
    def setup_method(self):
        updater._reset_cache()

    def _counting_fetch(self):
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return _fake_release("v1.0.8")

        return calls, fetch

    def test_second_call_within_ttl_uses_cache(self):
        calls, fetch = self._counting_fetch()
        a = check_update(current="1.0.7", fetch=fetch, now=0)
        b = check_update(current="1.0.7", fetch=fetch, now=100)  # < 1800s TTL
        assert a["update_available"] is True
        assert b["update_available"] is True
        assert calls["n"] == 1  # 第二次命中缓存,未再请求

    def test_refetch_after_ttl(self):
        calls, fetch = self._counting_fetch()
        check_update(current="1.0.7", fetch=fetch, now=0)
        check_update(current="1.0.7", fetch=fetch, now=1801)  # > 1800s TTL
        assert calls["n"] == 2

    def test_current_recomputed_from_cached_info(self):
        # 缓存的是原始 release 信息,update_available 按当次 current 重算
        calls, fetch = self._counting_fetch()
        a = check_update(current="1.0.7", fetch=fetch, now=0)
        b = check_update(current="1.0.8", fetch=fetch, now=10)  # 命中缓存
        assert a["update_available"] is True
        assert b["update_available"] is False  # 同为缓存信息,但当前已是最新
        assert calls["n"] == 1

    def test_failure_short_ttl_then_retries(self):
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise OSError("rate limit")

        check_update(current="1.0.7", fetch=boom, now=0)
        check_update(current="1.0.7", fetch=boom, now=30)  # < 60s 失败 TTL,不重试
        assert calls["n"] == 1
        out = check_update(current="1.0.7", fetch=boom, now=61)  # > 60s,重试
        assert calls["n"] == 2
        assert out["update_available"] is False


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
            "pkg_name": "PolymarketMarketMaker_Setup.exe",
            "pkg_url": "https://example.com/Setup.exe",
            "sha256_url": "https://example.com/Setup.exe.sha256",
            "pkg_size": 100,
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
        # 下载文件名取自 release 的资源名(跨平台:.exe / .dmg)
        assert launched and launched[0].endswith("PolymarketMarketMaker_Setup.exe")
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
                "pkg_url": "u",
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
                "pkg_url": None,
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
                    "pkg_url": "u",
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
                "pkg_url": "u",
                "sha256_url": "s",
                "pkg_size": 1,
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


class TestPlatformLauncher:
    def test_mac_uses_install_mac(self):
        assert _platform_launcher("darwin") is _install_mac

    def test_windows_uses_installer(self):
        assert _platform_launcher("win32") is _launch_installer

    def test_non_darwin_uses_installer(self):
        # 非 darwin 一律走"启动安装包"路径(Windows 风格)
        assert _platform_launcher("linux") is _launch_installer


class TestInstallMac:
    def test_writes_helper_script_and_launches_detached(self, tmp_path):
        dmg = str(tmp_path / "PolymarketMarketMaker_Mac_arm64.dmg")
        with open(dmg, "wb") as f:
            f.write(b"dmgbytes")
        app_path = "/Applications/PolymarketMarketMaker.app"
        popen_calls = []

        _install_mac(
            dmg,
            app_path=app_path,
            popen=lambda *a, **k: popen_calls.append((a, k)),
        )

        # 启动了恰好一个 detached 的 bash helper
        assert len(popen_calls) == 1
        args, _kwargs = popen_calls[0]
        cmd = args[0]
        assert cmd[0] == "/bin/bash"
        script = cmd[1]
        assert os.path.exists(script)

        body = open(script, encoding="utf-8").read()
        # 等本进程退出后再覆盖正在运行的包
        assert "kill -0" in body
        # 挂载 dmg -> ditto 覆盖 -> 卸载 -> 重新打开
        assert "hdiutil attach" in body
        assert "ditto" in body
        assert "hdiutil detach" in body
        assert "open " in body
        # 引用了正确的 dmg 与 app 路径
        assert dmg in body
        assert app_path in body
