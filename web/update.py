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


def verify_sha256(path, expected):
    """文件实算 SHA-256 是否等于 expected(忽略大小写与首尾空白)。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().lower() == (expected or "").strip().lower()


def engine_active(mgr):
    """是否有引擎/扫描在跑 —— 更新会中断做市并使持仓失去止损保护,故此时拒绝更新。"""
    if mgr is None:
        return False
    if getattr(mgr, "_scanner_thread", None) is not None:
        return True
    if getattr(mgr, "scan_status", "") == "scanning":
        return True
    return any(
        getattr(w, "running", False) for w in getattr(mgr, "engines", {}).values()
    )


class _State:
    """更新进度状态(单进程单用户,模块级单例即可)。"""

    def __init__(self):
        self.state = "idle"  # idle|downloading|verifying|installing|error
        self.percent = 0
        self.message = ""

    def snapshot(self):
        return {"state": self.state, "percent": self.percent, "message": self.message}


STATE = _State()


def _run_update(
    state, info, dest_dir, *, download, verify, fetch_sha, launch, shutdown
):
    """下载→校验→静默安装→退出。所有副作用经参数注入,便于测试。

    校验失败或任何异常 -> state=error,且绝不启动安装包、绝不退出进程。
    """
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(
            dest_dir, f"PolymarketMarketMaker_Setup_{info['version']}.exe"
        )

        state.state, state.percent, state.message = "downloading", 0, ""
        download(
            info["exe_url"],
            dest,
            info.get("exe_size"),
            lambda p: setattr(state, "percent", p),
        )

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
        return {
            "ok": False,
            "message": "引擎正在运行,更新会中断做市并使持仓失去止损保护,"
            "请先停止引擎再更新。",
        }
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
