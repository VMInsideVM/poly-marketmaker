"""tests/test_wallet_import.py — 批量导入钱包的粘贴文本解析(纯函数)。

格式:一行一个钱包 `私钥,代理,备注`,后两个字段可省。分隔符不能用冒号,
代理串本身就是 host:port:账户:密码。
"""

from web.wallet_import import parse_import_lines

KEY_A = "0x" + "a" * 64
KEY_B = "0x" + "b" * 64


def test_full_line():
    rows = parse_import_lines(f"{KEY_A},1.2.3.4:8080:user:pass,主号")
    assert len(rows) == 1
    assert rows[0] == {
        "line_no": 1,
        "private_key": KEY_A,
        "proxy": "1.2.3.4:8080:user:pass",
        "remark": "主号",
        "error": "",
    }


def test_key_only():
    rows = parse_import_lines(KEY_A)
    assert rows[0]["proxy"] == ""
    assert rows[0]["remark"] == ""
    assert rows[0]["error"] == ""


def test_empty_proxy_field_keeps_remark():
    """`私钥,,备注` —— 中间留空表示不走代理。"""
    rows = parse_import_lines(f"{KEY_A},,小号3")
    assert rows[0]["proxy"] == ""
    assert rows[0]["remark"] == "小号3"
    assert rows[0]["error"] == ""


def test_key_and_proxy_without_remark():
    rows = parse_import_lines(f"{KEY_A},1.2.3.4:8080")
    assert rows[0]["proxy"] == "1.2.3.4:8080"
    assert rows[0]["remark"] == ""


def test_blank_lines_skipped_but_line_numbers_are_real():
    """行号对应用户在文本框里看到的行,跳过空行不能把号也跳没。"""
    rows = parse_import_lines(f"\n{KEY_A}\n   \n{KEY_B}\n")
    assert [r["line_no"] for r in rows] == [2, 4]


def test_whitespace_around_fields_stripped():
    rows = parse_import_lines(f"  {KEY_A} ,  1.2.3.4:8080 ,  主号  ")
    assert rows[0]["private_key"] == KEY_A
    assert rows[0]["proxy"] == "1.2.3.4:8080"
    assert rows[0]["remark"] == "主号"


def test_fullwidth_comma_accepted():
    """中文输入法容易打出全角逗号,按半角同等对待。"""
    rows = parse_import_lines(f"{KEY_A}，1.2.3.4:8080，主号")
    assert rows[0]["proxy"] == "1.2.3.4:8080"
    assert rows[0]["remark"] == "主号"


def test_too_many_fields_is_an_error_row():
    rows = parse_import_lines(f"{KEY_A},1.2.3.4:8080,主号,多余")
    assert rows[0]["error"]
    assert "3 个字段" in rows[0]["error"]


def test_missing_key_is_an_error_row():
    rows = parse_import_lines(",1.2.3.4:8080,主号")
    assert rows[0]["error"]
    assert "私钥" in rows[0]["error"]


def test_bad_row_does_not_drop_the_others():
    """一行坏掉不能整批拒绝,其余行照常入库。"""
    rows = parse_import_lines(f"{KEY_A}\n,,\n{KEY_B}")
    assert len(rows) == 3
    assert rows[0]["error"] == ""
    assert rows[1]["error"]
    assert rows[2]["error"] == ""


def test_empty_text_yields_nothing():
    assert parse_import_lines("") == []
    assert parse_import_lines("\n  \n\n") == []
