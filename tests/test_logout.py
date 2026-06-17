"""tests/test_logout.py — 退出时清理进程内密钥与 API 缓存(F9)。"""

from web import routes


def test_logout_clears_encryption_key_and_api_cache():
    routes.app.config["TESTING"] = True
    routes.set_encryption_key(b"x" * 32)
    routes._api_cache["0xW"] = object()
    c = routes.app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
    r = c.get("/logout")
    assert r.status_code in (301, 302)  # 重定向到登录
    assert routes.encryption_key is None
    assert routes._api_cache == {}
