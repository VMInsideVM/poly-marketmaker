"""tests/test_update.py — 自动更新纯逻辑单测(无网络)。"""

import re
from version import __version__
import hashlib

from web.update import (
    parse_version,
    is_newer,
    parse_release,
    check_update,
    verify_sha256,
)


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


def _fake_release(tag="v1.0.8", with_exe=True, with_sha=True, body="修复若干问题"):
    assets = []
    if with_exe:
        assets.append(
            {
                "name": "PolymarketMarketMaker_Setup.exe",
                "browser_download_url": "https://example.com/Setup.exe",
                "size": 12345678,
            }
        )
    if with_sha:
        assets.append(
            {
                "name": "PolymarketMarketMaker_Setup.exe.sha256",
                "browser_download_url": "https://example.com/Setup.exe.sha256",
                "size": 70,
            }
        )
    return {"tag_name": tag, "body": body, "assets": assets}


class TestParseRelease:
    def test_full(self):
        info = parse_release(_fake_release())
        assert info["tag"] == "v1.0.8"
        assert info["version"] == "1.0.8"
        assert info["notes"] == "修复若干问题"
        assert info["exe_url"] == "https://example.com/Setup.exe"
        assert info["exe_size"] == 12345678
        assert info["sha256_url"] == "https://example.com/Setup.exe.sha256"

    def test_missing_exe(self):
        info = parse_release(_fake_release(with_exe=False))
        assert info["exe_url"] is None
        assert info["exe_size"] is None

    def test_missing_sha(self):
        info = parse_release(_fake_release(with_sha=False))
        assert info["sha256_url"] is None

    def test_empty_release(self):
        info = parse_release({})
        assert info["exe_url"] is None
        assert info["sha256_url"] is None
        assert info["notes"] == ""


class TestCheckUpdate:
    def test_update_available(self):
        out = check_update(current="1.0.7", fetch=lambda: _fake_release("v1.0.8"))
        assert out["update_available"] is True
        assert out["current"] == "1.0.7"
        assert out["latest"] == "1.0.8"
        assert out["notes"] == "修复若干问题"
        assert out["size"] == 12345678

    def test_same_version_no_update(self):
        out = check_update(current="1.0.8", fetch=lambda: _fake_release("v1.0.8"))
        assert out["update_available"] is False

    def test_no_exe_asset_no_update(self):
        out = check_update(
            current="1.0.7", fetch=lambda: _fake_release("v1.0.8", with_exe=False)
        )
        assert out["update_available"] is False

    def test_network_error_is_swallowed(self):
        def boom():
            raise OSError("network down")

        out = check_update(current="1.0.7", fetch=boom)
        assert out["update_available"] is False
        assert out["current"] == "1.0.7"


class TestVerifySha256:
    def test_match(self, tmp_path):
        f = tmp_path / "blob.bin"
        data = b"hello polymarket"
        f.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        assert verify_sha256(str(f), digest) is True

    def test_match_case_insensitive_and_whitespace(self, tmp_path):
        f = tmp_path / "blob.bin"
        data = b"abc"
        f.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest().upper()
        assert verify_sha256(str(f), "  " + digest + "  ") is True

    def test_mismatch(self, tmp_path):
        f = tmp_path / "blob.bin"
        f.write_bytes(b"abc")
        assert verify_sha256(str(f), "0" * 64) is False
