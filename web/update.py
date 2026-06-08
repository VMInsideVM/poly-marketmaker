"""web/update.py — GitHub Release 自动更新:检测/下载/校验/安装。

副作用(网络、启动安装包、退出进程)都以可注入依赖实现,便于离线单测。
"""

import hashlib
import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
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


def parse_release(rel, system=None):
    """从 GitHub release JSON 取出本平台更新所需字段。资源缺失则对应字段为 None。

    按平台选安装包:macOS 取 ``.dmg``,其余(Windows/Linux)取 ``.exe``。
    sha256 必须与所选安装包**按文件名配对**(``<包名>.sha256``):自 v1.0.15 起
    一个 release 同时含 .exe 与 .dmg 两套校验文件,若只按后缀取第一个 .sha256 会
    取错另一平台的校验值,导致校验失败。找不到配对项时回退到任意 .sha256(兼容只
    含单个包的老版本)。
    system 可注入以便测试;默认用当前运行平台 ``sys.platform``。
    """
    system = system or sys.platform
    pkg_suffix = ".dmg" if system == "darwin" else ".exe"
    tag = rel.get("tag_name", "") or ""
    assets = rel.get("assets", []) or []

    def _find(suffix):
        for a in assets:
            if (a.get("name", "") or "").lower().endswith(suffix):
                return a
        return None

    pkg = _find(pkg_suffix)
    sha = None
    if pkg:
        want = (pkg.get("name", "") or "") + ".sha256"
        for a in assets:
            if (a.get("name", "") or "") == want:
                sha = a
                break
    if sha is None:  # 兜底:老版本只有一个包+一个 .sha256
        sha = _find(".sha256")
    return {
        "tag": tag,
        "version": tag.lstrip("vV"),
        "notes": rel.get("body", "") or "",
        "pkg_name": pkg.get("name") if pkg else None,
        "pkg_url": pkg.get("browser_download_url") if pkg else None,
        "pkg_size": pkg.get("size") if pkg else None,
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


# 检测结果带 TTL 缓存,避开 GitHub 未认证 API 限额(60 次/小时/IP)。弹窗 JS 在每个
# 页面加载都会打 /api/update/check,无缓存时翻几页就会触发 403;缓存后每个进程每 30
# 分钟最多请求一次,页面间导航全部命中缓存。缓存存原始 release 信息(非最终结果),
# 每次按当前版本重算 update_available。
_CHECK_TTL_SEC = 1800  # 成功后 30 分钟内复用缓存
_FAIL_TTL_SEC = 60  # 失败后至少隔 60 秒再试,避免限流/断网时疯狂重试
_cache = {"ts": None, "info": None, "ok": False}


def _reset_cache():
    """清空检测缓存(测试用)。"""
    _cache.update(ts=None, info=None, ok=False)


def check_update(current=__version__, fetch=None, now=None):
    """检测最新版本。永不抛异常:任何失败都返回 update_available=False。

    结果带 TTL 缓存(成功 _CHECK_TTL_SEC 秒、失败 _FAIL_TTL_SEC 秒),期间复用、不再请求
    GitHub,以避开未认证 API 限额。
    fetch: 无参函数,返回 GitHub release JSON dict(测试可注入)。
    now: 当前时间戳,默认 time.time()(测试可注入以控制 TTL)。
    """
    fetch = fetch or _fetch_latest_release
    now = time.time() if now is None else now
    ttl = _CHECK_TTL_SEC if _cache["ok"] else _FAIL_TTL_SEC
    if _cache["ts"] is not None and (now - _cache["ts"]) < ttl:
        info = _cache["info"]
    else:
        try:
            info = parse_release(fetch())
            _cache.update(ts=now, info=info, ok=True)
        except Exception as e:  # noqa: BLE001 — 检测必须非阻塞
            logger.warning("更新检测失败: %s", e)
            _cache.update(ts=now, info=None, ok=False)
            info = None
    if info is None:
        return {"update_available": False, "current": current}
    available = bool(info["pkg_url"]) and is_newer(info["version"], current)
    return {
        "update_available": available,
        "current": current,
        "latest": info["version"],
        "notes": info["notes"],
        "size": info["pkg_size"],
    }


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
    """更新进度状态(单进程单用户,模块级单例即可)。

    线程安全说明:本应用单进程单用户,依赖 CPython GIL 保证单属性读写的原子性
    (后台更新线程写,Flask 请求线程经 snapshot() 读);若将来需要多属性原子快照,
    应加 threading.Lock。
    """

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
        # 下载文件名直接用 release 的资源名,天然区分平台(.exe / .dmg)。
        dest = os.path.join(dest_dir, info["pkg_name"])

        # state is already "downloading" — set by start_update (the sole owner of the
        # initial state) before the thread is spawned, so polling can observe it
        # immediately after start_update returns without waiting for the thread to run.
        download(
            info["pkg_url"],
            dest,
            info.get("pkg_size"),
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


def _current_app_bundle():
    """正在运行的 .app 包路径(……/Foo.app),仅 macOS 冻结运行时有意义。

    冻结后 sys.executable = ……/Foo.app/Contents/MacOS/Foo,向上三级即 .app。
    """
    return os.path.dirname(os.path.dirname(os.path.dirname(sys.executable)))


def _install_mac(path, *, app_path=None, popen=None):
    """macOS 静默更新:写一个 detached helper 脚本并启动它,随后本进程退出。

    helper 会:等本进程退出 → 挂载 .dmg → 用 ditto 取新 .app 覆盖当前安装位置 →
    卸载 → 重新打开。用 ditto 程序化拷贝(而非浏览器下载)使新包不带
    com.apple.quarantine 标记,重开不会再被 Gatekeeper 拦("已损坏")。
    app_path / popen 可注入以便测试。
    """
    app_path = app_path or _current_app_bundle()
    popen = popen or subprocess.Popen
    app_name = os.path.basename(app_path)  # PolymarketMarketMaker.app
    app_dir = os.path.dirname(app_path)  # 通常是 /Applications
    pid = os.getpid()
    script_path = os.path.join(os.path.dirname(path), "apply_update.sh")
    helper = (
        "#!/bin/bash\n"
        "set -e\n"
        f"DMG={shlex.quote(path)}\n"
        f"APP={shlex.quote(app_path)}\n"
        f"APPDIR={shlex.quote(app_dir)}\n"
        f"APPNAME={shlex.quote(app_name)}\n"
        f"PID={pid}\n"
        "# 等主程序退出,避免覆盖正在运行的包(最多约 20s)\n"
        'for _ in $(seq 1 100); do kill -0 "$PID" 2>/dev/null || break; sleep 0.2; done\n'
        "MNT=$(mktemp -d /tmp/pmm_update.XXXXXX)\n"
        'hdiutil attach -nobrowse -quiet -mountpoint "$MNT" "$DMG"\n'
        'if [ -d "$MNT/$APPNAME" ]; then\n'
        '  rm -rf "$APPDIR/$APPNAME.new"\n'
        '  /usr/bin/ditto "$MNT/$APPNAME" "$APPDIR/$APPNAME.new"\n'
        '  rm -rf "$APP"\n'
        '  mv "$APPDIR/$APPNAME.new" "$APP"\n'
        "fi\n"
        'hdiutil detach "$MNT" -quiet || true\n'
        'rmdir "$MNT" 2>/dev/null || true\n'
        'open "$APP"\n'
    )
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(helper)
    os.chmod(script_path, 0o755)
    popen(
        ["/bin/bash", script_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _platform_launcher(system=None):
    """按平台返回"安装动作":macOS 用 _install_mac,其余用 _launch_installer。"""
    system = system or sys.platform
    return _install_mac if system == "darwin" else _launch_installer


def _shutdown_self():
    """立即退出进程,释放文件锁,交由安装包/helper 覆盖并重启。"""
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
    if not info or not info.get("pkg_url") or not info.get("sha256_url"):
        return {
            "ok": False,
            "message": "未找到可用的更新包(缺少本平台安装包或 sha256 资源)",
        }

    deps.setdefault("download", _download)
    deps.setdefault("verify", verify_sha256)
    deps.setdefault("fetch_sha", _http_get)
    deps.setdefault("launch", _platform_launcher())
    deps.setdefault("shutdown", _shutdown_self)

    # Single owner of the initial "downloading" state — set here (not inside
    # _run_update) so the state is visible to the next poll before the thread starts.
    STATE.state, STATE.percent, STATE.message = "downloading", 0, ""
    threading.Thread(
        target=_run_update,
        args=(STATE, info, _UPDATE_DIR),
        kwargs=deps,
        daemon=True,
    ).start()
    return {"ok": True}
