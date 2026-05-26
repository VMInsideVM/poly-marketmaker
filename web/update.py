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
