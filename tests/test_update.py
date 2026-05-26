"""tests/test_update.py — 自动更新纯逻辑单测(无网络)。"""

import re
from version import __version__


def test_version_is_semver_string():
    assert isinstance(__version__, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__
