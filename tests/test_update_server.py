"""tests/test_update_server.py — 服务器模式更新(git 同步,全离线)。"""

import threading

import pytest

import web.update as updater
from web.update import _State, _run_git_update, check_update, start_update


class _FakeRunner:
    """把 (args, cwd) 记下来,按预设返回码应答。"""

    def __init__(self, failures=None):
        # failures: {命令中的关键字: (返回码, 输出)}
        self.failures = failures or {}
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append(args)
        for keyword, result in self.failures.items():
            if keyword in args:
                return result
        if args[:2] == ["git", "rev-parse"]:
            return 0, "oldcommit123\n"
        return 0, ""

    def git_args(self):
        return [a for a in self.calls if a and a[0] == "git"]


INFO = {"tag": "v9.9.9", "version": "9.9.9"}


class TestHappyPath:
    def _run(self):
        state, runner, exited = _State(), _FakeRunner(), []
        _run_git_update(
            state, INFO, "/repo", run_cmd=runner, shutdown=lambda: exited.append(1)
        )
        return state, runner, exited

    def test_command_sequence(self):
        _, runner, _ = self._run()
        assert runner.calls[0] == ["git", "rev-parse", "HEAD"]
        assert runner.calls[1] == ["git", "fetch", "--tags", "origin"]
        assert runner.calls[2] == ["git", "reset", "--hard", "v9.9.9"]
        assert "pip" in runner.calls[3]
        assert "install" in runner.calls[3]

    def test_exits_for_systemd_to_restart(self):
        _, _, exited = self._run()
        assert exited == [1]

    def test_no_error_state(self):
        state, _, _ = self._run()
        assert state.state != "error"


class TestFailureRollback:
    def test_pip_failure_rolls_back_and_keeps_running(self):
        state, exited = _State(), []
        runner = _FakeRunner(failures={"install": (1, "No matching distribution")})
        _run_git_update(
            state, INFO, "/repo", run_cmd=runner, shutdown=lambda: exited.append(1)
        )
        # 回滚到更新前的 commit
        assert ["git", "reset", "--hard", "oldcommit123"] in runner.git_args()
        assert state.state == "error"
        assert "回滚" in state.message
        # 关键:不退出进程,旧版本继续跑
        assert exited == []

    def test_fetch_failure_stops_before_reset(self):
        state, exited = _State(), []
        runner = _FakeRunner(failures={"fetch": (1, "could not resolve host")})
        _run_git_update(
            state, INFO, "/repo", run_cmd=runner, shutdown=lambda: exited.append(1)
        )
        assert not any("reset" in a for a in runner.git_args())
        assert state.state == "error"
        assert exited == []

    def test_reset_failure_stops_before_pip(self):
        state, exited = _State(), []
        runner = _FakeRunner(failures={"reset": (1, "unknown revision")})
        _run_git_update(
            state, INFO, "/repo", run_cmd=runner, shutdown=lambda: exited.append(1)
        )
        assert not any("pip" in a for a in runner.calls)
        assert state.state == "error"
        assert exited == []

    def test_unexpected_exception_is_caught(self):
        state, exited = _State(), []

        def boom(args, cwd):
            raise RuntimeError("炸了")

        _run_git_update(
            state, INFO, "/repo", run_cmd=boom, shutdown=lambda: exited.append(1)
        )
        assert state.state == "error"
        assert exited == []


class TestStartUpdateDispatch:
    @pytest.fixture(autouse=True)
    def _reset(self):
        updater.STATE.state = "idle"
        yield
        updater.STATE.state = "idle"

    def test_server_mode_uses_git_path(self, monkeypatch):
        monkeypatch.setattr(updater, "SERVER_MODE", True)
        seen, done = {}, threading.Event()

        def _fake(state, info, repo_dir, **kw):
            seen["info"], seen["repo"] = info, repo_dir
            done.set()

        # 不要去 patch updater.threading.Thread —— 那是全局 threading 模块,
        # 改它会影响整个进程。让真线程跑,用 Event 等它。
        monkeypatch.setattr(updater, "_run_git_update", _fake)
        result = start_update(None, info_provider=lambda: INFO)
        assert result["ok"] is True
        assert done.wait(5), "更新线程未在 5 秒内启动"
        assert seen["info"]["tag"] == "v9.9.9"

    def test_engine_running_blocks_update(self, monkeypatch):
        monkeypatch.setattr(updater, "SERVER_MODE", True)
        monkeypatch.setattr(updater, "engine_active", lambda mgr: True)
        called = []
        monkeypatch.setattr(
            updater, "_run_git_update", lambda *a, **k: called.append(1)
        )
        result = start_update(object(), info_provider=lambda: INFO)
        assert result["ok"] is False
        assert "停止引擎" in result["message"]
        assert called == []

    def test_missing_tag_reports_error(self, monkeypatch):
        monkeypatch.setattr(updater, "SERVER_MODE", True)
        result = start_update(None, info_provider=lambda: None)
        assert result["ok"] is False


class TestCheckUpdateInServerMode:
    @pytest.fixture(autouse=True)
    def _clear(self):
        updater._reset_cache()
        yield
        updater._reset_cache()

    def _release(self, tag):
        # 没有任何 Linux 安装包 —— 服务器模式不该依赖 asset
        return {"tag_name": tag, "body": "", "assets": []}

    def test_server_mode_ignores_missing_asset(self, monkeypatch):
        monkeypatch.setattr(updater, "SERVER_MODE", True)
        r = check_update(current="1.0.0", fetch=lambda: self._release("v1.0.1"))
        assert r["update_available"] is True

    def test_server_mode_still_respects_version(self, monkeypatch):
        monkeypatch.setattr(updater, "SERVER_MODE", True)
        r = check_update(current="1.0.1", fetch=lambda: self._release("v1.0.1"))
        assert r["update_available"] is False

    def test_local_mode_still_requires_asset(self, monkeypatch):
        monkeypatch.setattr(updater, "SERVER_MODE", False)
        r = check_update(current="1.0.0", fetch=lambda: self._release("v1.0.1"))
        assert r["update_available"] is False
