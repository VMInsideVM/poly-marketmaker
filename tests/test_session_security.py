"""tests/test_session_security.py — 会话 cookie 的安全属性。"""

import hashlib
from datetime import timedelta

import web.routes as routes
from models.database import Database
from utils.crypto import derive_key


def test_httponly_and_samesite_always_on():
    # 两种模式都要开:防 XSS 读取、防跨站带 cookie
    assert routes.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert routes.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_secure_flag_follows_server_mode():
    # 本地是 http,开了 Secure 浏览器就不会回传 cookie,直接登不进去;
    # 服务器上全程 https,必须开。
    # 比对 routes.SERVER_MODE(即 app.config 实际采用的那个值),不要重新 import
    # config —— tests/test_server_mode.py 会 reload config,值可能已被改过。
    assert routes.app.config["SESSION_COOKIE_SECURE"] is routes.SERVER_MODE


def test_session_lifetime_is_seven_days():
    assert routes.app.config["PERMANENT_SESSION_LIFETIME"] == timedelta(days=7)


def test_proxyfix_installed():
    # Caddy 反代后 remote_addr 恒为 127.0.0.1,登录限速必须能拿到真实 IP
    from werkzeug.middleware.proxy_fix import ProxyFix

    assert isinstance(routes.app.wsgi_app, ProxyFix)


def test_forwarded_for_becomes_remote_addr():
    # 不能像最初设想那样在共享的 routes.app 上动态注册新路由:pytest 全量跑时
    # 其他用例已经对这个模块级单例 app 发过请求,Flask/Werkzeug 会拒绝之后再改
    # 路由表(“has already handled its first request”)。改为直接操作
    # app.wsgi_app —— 它就是 ProxyFix 实例 —— 临时替换其内层可调用对象来
    # 捕获转换后的 environ,不触碰路由表。
    from werkzeug.test import EnvironBuilder

    captured = {}
    proxy_fix = routes.app.wsgi_app
    original_inner_app = proxy_fix.app

    def _fake_inner_app(environ, start_response):
        captured["ip"] = environ.get("REMOTE_ADDR")
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    proxy_fix.app = _fake_inner_app
    try:
        environ = EnvironBuilder(
            path="/", headers={"X-Forwarded-For": "203.0.113.9"}
        ).get_environ()
        proxy_fix(environ, lambda *a, **kw: None)
    finally:
        proxy_fix.app = original_inner_app

    assert captured["ip"] == "203.0.113.9"


def test_login_sets_permanent_session_cookie(tmp_path):
    # PERMANENT_SESSION_LIFETIME 只对 permanent=True 的 session 生效;Flask 默认
    # session 是非 permanent 的“关浏览器即失效”会话 cookie,不带 Expires/Max-Age。
    # 只测 config 字典里的 PERMANENT_SESSION_LIFETIME 测不出登录路由是否真的把
    # session 标成了 permanent —— 这里跑真实的 /login 流程,检查下发的 Set-Cookie
    # 头是否带过期时间。
    password = "test-password-123456"
    db = Database(str(tmp_path / "session_security.db"))
    db.init()
    salt = b"0123456789abcdef"
    key = derive_key(password, salt)
    hashed = hashlib.sha256(key).hexdigest()
    db.save_password(hashed, salt)

    original_db = routes.db
    original_manager = routes.manager
    original_key = routes.encryption_key
    routes.db = db
    routes.manager = None
    routes.encryption_key = None
    try:
        client = routes.app.test_client()
        resp = client.post("/login", data={"password": password})
        set_cookie_headers = resp.headers.get_all("Set-Cookie")
        session_cookie = next(
            h for h in set_cookie_headers if h.startswith("session=")
        )
        assert "Expires=" in session_cookie or "Max-Age=" in session_cookie
    finally:
        routes.db = original_db
        routes.manager = original_manager
        routes.encryption_key = original_key
        db.close()
