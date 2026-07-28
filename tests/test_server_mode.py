"""tests/test_server_mode.py — 服务器模式开关(PMM_SERVER)。"""

import importlib
import config


def _reload_with(monkeypatch, value):
    """用给定的 PMM_SERVER 值重新导入 config,返回重载后的模块。"""
    if value is None:
        monkeypatch.delenv("PMM_SERVER", raising=False)
    else:
        monkeypatch.setenv("PMM_SERVER", value)
    return importlib.reload(config)


def test_off_by_default(monkeypatch):
    assert _reload_with(monkeypatch, None).SERVER_MODE is False


def test_on_when_env_is_1(monkeypatch):
    assert _reload_with(monkeypatch, "1").SERVER_MODE is True


def test_off_for_other_values(monkeypatch):
    assert _reload_with(monkeypatch, "0").SERVER_MODE is False
    assert _reload_with(monkeypatch, "true").SERVER_MODE is False
    assert _reload_with(monkeypatch, "").SERVER_MODE is False


def teardown_module():
    """把 config 恢复成不带环境变量的状态,避免污染其他测试。"""
    import os

    os.environ.pop("PMM_SERVER", None)
    importlib.reload(config)
