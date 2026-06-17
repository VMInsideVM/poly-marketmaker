"""tests/test_net.py — pick_port 选择可绑定端口。"""

import socket

from utils.net import pick_port

HOST = "127.0.0.1"


def test_returns_preferred_when_free():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()  # 释放,preferred 此时空闲
    assert pick_port(HOST, port) == port


def test_falls_back_when_preferred_unavailable():
    # 占住 preferred,使其无法绑定——与 Windows 保留区间端口同样的失败方式
    # (bind 抛 OSError / WinError 10013)。
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind((HOST, 0))
    taken = held.getsockname()[1]
    try:
        chosen = pick_port(HOST, taken)
        assert chosen != taken
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((HOST, chosen))  # 选中的端口必须真能绑
        finally:
            probe.close()
    finally:
        held.close()


def test_raises_when_nothing_bindable():
    # preferred 和系统分配端口(0)都绑不上时必须抛错,而非返回一个绑不上的端口
    # (旧行为 return preferred → Flask 随后崩在晦涩的 WinError 10013)(F13)。
    from unittest.mock import MagicMock, patch
    import pytest

    with patch("utils.net.socket.socket") as sock:
        s = MagicMock()
        s.bind.side_effect = OSError("port unavailable")
        sock.return_value.__enter__.return_value = s
        with pytest.raises(OSError):
            pick_port(HOST, 8765)
