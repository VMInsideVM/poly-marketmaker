"""utils/net.py — 选一个能绑定的本地端口。"""

import socket


def pick_port(host: str, preferred: int) -> int:
    """返回 host 上一个可绑定的 TCP 端口,优先用 preferred。

    Windows 上 Hyper-V/WSL/Docker 会动态保留一批 TCP 端口区间
    (见 `netsh int ipv4 show excludedportrange`)。写死的端口可能正好落进
    某个保留区间,即使没有进程在用,绑定也会以 WinError 10013 失败。当
    preferred 绑不上时回退到系统分配的空闲端口,保证 app 仍能启动。
    """
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, candidate))
            except OSError:
                continue
            return s.getsockname()[1]
    raise OSError(
        f"无法绑定任何端口(首选 {preferred} 与系统分配端口均失败);"
        f"请检查 {host} 上的端口占用/保留情况"
    )


def resolve_port(host: str, preferred: int, server_mode: bool) -> int:
    """选定实际监听端口。

    服务器模式下固定用 preferred:反向代理写死了这个端口,一旦回退到别的端口,
    服务看起来启动成功、网页却打不开。端口被占用时让后续 bind 直接失败,
    由 systemd 的重启循环和日志把问题暴露出来。
    本地模式沿用 pick_port 的回退行为(见其 docstring 中的 Windows 端口保留问题)。
    """
    return preferred if server_mode else pick_port(host, preferred)
