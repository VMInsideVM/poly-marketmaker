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
