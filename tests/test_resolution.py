"""tests/test_resolution.py — UMA 结算状态判定纯函数(不触网)。"""

from engine.resolution import in_resolution


def test_none_is_not_in_resolution():
    assert in_resolution(None) is False


def test_empty_string_is_not_in_resolution():
    assert in_resolution("") is False


def test_whitespace_is_not_in_resolution():
    assert in_resolution("   ") is False


def test_proposed_is_in_resolution():
    assert in_resolution("proposed") is True


def test_disputed_is_in_resolution():
    assert in_resolution("disputed") is True


def test_resolved_is_in_resolution():
    assert in_resolution("resolved") is True
