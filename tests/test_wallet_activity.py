"""tests/test_wallet_activity.py — 钱包「上次活跃时间」(last_active_at)。

活跃 = 成功挂出一笔买单,或抓到一笔成交。覆盖式,只留最近一次,纯展示。
"""

import sqlite3
import time

from models.database import Database


def _db(tmp_path, name="act.db"):
    d = Database(str(tmp_path / name))
    d.init()
    return d


def test_new_wallet_has_no_activity(tmp_path):
    d = _db(tmp_path)
    try:
        d.add_wallet("0xabc", "enc")
        w = d.list_wallets()[0]
        assert w["last_active_at"] == 0
    finally:
        d.close()


def test_touch_updates_to_now(tmp_path):
    d = _db(tmp_path)
    try:
        d.add_wallet("0xabc", "enc")
        before = time.time()
        d.touch_wallet_active("0xabc")
        after = time.time()
        ts = d.list_wallets()[0]["last_active_at"]
        assert before <= ts <= after
    finally:
        d.close()


def test_touch_overwrites_previous(tmp_path):
    """只保留上一次:第二次 touch 覆盖第一次,不留历史。"""
    d = _db(tmp_path)
    try:
        d.add_wallet("0xabc", "enc")
        d.touch_wallet_active("0xabc")
        first = d.list_wallets()[0]["last_active_at"]
        time.sleep(0.01)
        d.touch_wallet_active("0xabc")
        second = d.list_wallets()[0]["last_active_at"]
        assert second > first
    finally:
        d.close()


def test_touch_unknown_wallet_is_noop(tmp_path):
    """钱包已被删除时 touch 不该炸——写入点在引擎线程里,炸了会打断下单/止损。"""
    d = _db(tmp_path)
    try:
        d.touch_wallet_active("0xdeleted")
    finally:
        d.close()


def _client_with_db(tmp_path, monkeypatch):
    import web.routes as routes

    d = _db(tmp_path, "routes.db")
    monkeypatch.setattr(routes, "db", d)
    monkeypatch.setattr(routes, "manager", None)
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client, d


def test_dashboard_exposes_last_active_at(tmp_path, monkeypatch):
    client, d = _client_with_db(tmp_path, monkeypatch)
    d.add_wallet("0xABC", "enc")
    d.touch_wallet_active("0xABC")
    w = client.get("/api/dashboard").get_json()["wallets"][0]
    assert w["last_active_at"] > 0


def test_wallets_route_exposes_last_active_at(tmp_path, monkeypatch):
    client, d = _client_with_db(tmp_path, monkeypatch)
    d.add_wallet("0xABC", "enc")
    assert client.get("/api/wallets").get_json()[0]["last_active_at"] == 0


def test_migrates_old_db_without_column(tmp_path):
    """老库(wallets 表没有 last_active_at)再次 init 后补上该列,已有钱包置 0。"""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE wallets ("
        "address TEXT PRIMARY KEY, encrypted_key TEXT NOT NULL, "
        "funder TEXT NOT NULL DEFAULT '', signature_type INTEGER NOT NULL DEFAULT 2, "
        "proxy TEXT NOT NULL DEFAULT '', remark TEXT NOT NULL DEFAULT '', "
        "enabled INTEGER NOT NULL DEFAULT 1, "
        "created_at REAL NOT NULL DEFAULT (strftime('%s','now')))"
    )
    conn.execute("INSERT INTO wallets (address, encrypted_key) VALUES ('0xold', 'enc')")
    conn.commit()
    conn.close()

    d = Database(path)
    d.init()
    try:
        w = [x for x in d.list_wallets() if x["address"] == "0xold"][0]
        assert w["last_active_at"] == 0
    finally:
        d.close()
