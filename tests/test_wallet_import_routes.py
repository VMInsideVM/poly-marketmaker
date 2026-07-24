"""tests/test_wallet_import_routes.py — 批量导入钱包的路由:后台跑 + 逐行结果 + 并发保护。

导入本体(_import_wallet)在这里被替身掉:它要联网探代理和账户类型,不是本测试的对象。
"""

import time

import pytest

import web.routes as routes
from web.wallet_import import ImportJob

KEY_A = "0x" + "a" * 64
KEY_B = "0x" + "b" * 64


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(routes, "_import_job", ImportJob())  # 每个用例一份干净状态
    routes.app.config["TESTING"] = True
    c = routes.app.test_client()
    with c.session_transaction() as sess:
        sess["logged_in"] = True
    return c


def _wait_done(client, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = client.get("/api/wallets/import-status").get_json()
        if not snap["running"]:
            return snap
        time.sleep(0.02)
    raise AssertionError("批量导入没有在超时内结束")


def test_imports_every_row(client, monkeypatch):
    seen = []

    def fake(key, funder="", proxy="", remark=""):
        seen.append((key, proxy, remark))
        return {
            "address": "0x" + key[2:6],
            "funder": "0xF",
            "signature_type": 2,
            "remark": remark,
            "proxy_protocol": "HTTP",
            "proxy_exit_ip": "1.1.1.1",
        }

    monkeypatch.setattr(routes, "_import_wallet", fake)
    r = client.post(
        "/api/wallets/import",
        json={"text": f"{KEY_A},1.2.3.4:8080,主号\n{KEY_B},,小号2"},
    )
    assert r.status_code == 200
    assert r.get_json()["total"] == 2

    snap = _wait_done(client)
    assert snap["done"] == 2
    assert [x["ok"] for x in snap["results"]] == [True, True]
    assert seen == [(KEY_A, "1.2.3.4:8080", "主号"), (KEY_B, "", "小号2")]


def test_one_bad_row_does_not_stop_the_rest(client, monkeypatch):
    def fake(key, funder="", proxy="", remark=""):
        if key == KEY_A:
            raise routes.WalletImportError("该钱包已存在")
        return {
            "address": "0xBBBB",
            "funder": "0xF",
            "signature_type": 2,
            "remark": remark,
            "proxy_protocol": "",
            "proxy_exit_ip": None,
        }

    monkeypatch.setattr(routes, "_import_wallet", fake)
    client.post("/api/wallets/import", json={"text": f"{KEY_A}\n{KEY_B}"})

    snap = _wait_done(client)
    assert snap["results"][0]["ok"] is False
    assert snap["results"][0]["error"] == "该钱包已存在"
    assert snap["results"][1]["ok"] is True


def test_malformed_row_reported_without_calling_import(client, monkeypatch):
    calls = []

    def fake(key, funder="", proxy="", remark=""):
        calls.append(key)
        return {
            "address": "0xAAAA",
            "funder": "0xF",
            "signature_type": 2,
            "remark": remark,
            "proxy_protocol": "",
            "proxy_exit_ip": None,
        }

    monkeypatch.setattr(routes, "_import_wallet", fake)
    client.post("/api/wallets/import", json={"text": f"{KEY_A},a,b,c\n{KEY_B}"})

    snap = _wait_done(client)
    assert snap["results"][0]["ok"] is False and "字段" in snap["results"][0]["error"]
    assert calls == [KEY_B]  # 坏行不该白跑一次联网导入


def test_results_never_leak_private_keys(client, monkeypatch):
    monkeypatch.setattr(
        routes,
        "_import_wallet",
        lambda key, funder="", proxy="", remark="": {
            "address": "0xAAAA",
            "funder": "0xF",
            "signature_type": 2,
            "remark": remark,
            "proxy_protocol": "",
            "proxy_exit_ip": None,
        },
    )
    client.post("/api/wallets/import", json={"text": f"{KEY_A},,主号"})
    snap = _wait_done(client)
    assert KEY_A not in str(snap)


def test_empty_text_rejected(client):
    r = client.post("/api/wallets/import", json={"text": "  \n\n"})
    assert r.status_code == 400


def test_second_submit_rejected_while_running(client, monkeypatch):
    release = __import__("threading").Event()

    def slow(key, funder="", proxy="", remark=""):
        release.wait(5)
        return {
            "address": "0xAAAA",
            "funder": "0xF",
            "signature_type": 2,
            "remark": remark,
            "proxy_protocol": "",
            "proxy_exit_ip": None,
        }

    monkeypatch.setattr(routes, "_import_wallet", slow)
    assert client.post("/api/wallets/import", json={"text": KEY_A}).status_code == 200
    r2 = client.post("/api/wallets/import", json={"text": KEY_B})
    assert r2.status_code == 409
    release.set()
    _wait_done(client)
