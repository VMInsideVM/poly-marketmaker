"""tests/test_update.py — 自动更新纯逻辑单测(无网络)。"""

import re
from version import __version__
from web.update import parse_version, is_newer


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
