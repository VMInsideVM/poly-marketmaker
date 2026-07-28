"""tests/test_login_rate_limit.py — 登录失败限速(纯逻辑 + 路由集成)。"""

import hashlib

import pytest

import web.routes as routes


@pytest.fixture(autouse=True)
def _clean_state():
    routes._login_fails.clear()
    yield
    routes._login_fails.clear()


class TestPureLogic:
    def test_fresh_ip_not_locked(self):
        assert routes.login_lock_remaining("1.2.3.4", now=1000.0) == 0

    def test_below_limit_not_locked(self):
        for _ in range(routes._LOGIN_FAIL_LIMIT - 1):
            routes.record_login_failure("1.2.3.4", now=1000.0)
        assert routes.login_lock_remaining("1.2.3.4", now=1000.0) == 0

    def test_locks_at_limit(self):
        for _ in range(routes._LOGIN_FAIL_LIMIT):
            routes.record_login_failure("1.2.3.4", now=1000.0)
        assert routes.login_lock_remaining("1.2.3.4", now=1000.0) == routes._LOGIN_LOCK_SEC

    def test_lock_counts_down(self):
        for _ in range(routes._LOGIN_FAIL_LIMIT):
            routes.record_login_failure("1.2.3.4", now=1000.0)
        remaining = routes.login_lock_remaining("1.2.3.4", now=1000.0 + 300)
        assert remaining == routes._LOGIN_LOCK_SEC - 300

    def test_lock_expires_and_counter_resets(self):
        for _ in range(routes._LOGIN_FAIL_LIMIT):
            routes.record_login_failure("1.2.3.4", now=1000.0)
        after = 1000.0 + routes._LOGIN_LOCK_SEC + 1
        assert routes.login_lock_remaining("1.2.3.4", now=after) == 0
        # 到期后计数清零,下一次失败不应立刻再次锁定
        routes.record_login_failure("1.2.3.4", now=after)
        assert routes.login_lock_remaining("1.2.3.4", now=after) == 0

    def test_success_clears(self):
        for _ in range(routes._LOGIN_FAIL_LIMIT - 1):
            routes.record_login_failure("1.2.3.4", now=1000.0)
        routes.clear_login_failures("1.2.3.4")
        routes.record_login_failure("1.2.3.4", now=1000.0)
        assert routes.login_lock_remaining("1.2.3.4", now=1000.0) == 0

    def test_ips_are_independent(self):
        for _ in range(routes._LOGIN_FAIL_LIMIT):
            routes.record_login_failure("1.2.3.4", now=1000.0)
        assert routes.login_lock_remaining("5.6.7.8", now=1000.0) == 0


RIGHT_PASSWORD = "correct-horse-battery"


@pytest.fixture
def client(monkeypatch):
    """把 derive_key 换成快速假实现。

    真 derive_key 是 600k 次 PBKDF2(约 0.5 秒),这组测试要发十几次登录请求,
    用真的会让测试跑十几秒。这里关心的是限速逻辑,不是 KDF 本身
    (KDF 有 tests/test_crypto.py 覆盖)。
    """
    right_hash = hashlib.sha256(b"RIGHT").hexdigest()

    class _DB:
        def get_password(self):
            return right_hash, b"s" * 16

    monkeypatch.setattr(routes, "db", _DB())
    monkeypatch.setattr(
        routes,
        "derive_key",
        lambda pw, salt: b"RIGHT" if pw == RIGHT_PASSWORD else b"WRONG",
    )
    routes.app.config["TESTING"] = True
    yield routes.app.test_client()
    # 登录成功的用例会设进程级密钥,清掉以免污染其他测试
    routes.set_encryption_key(None)


class TestRouteIntegration:
    def _post(self, client, password, ip="203.0.113.7"):
        return client.post(
            "/login",
            data={"password": password},
            headers={"X-Forwarded-For": ip},
            follow_redirects=False,
        )

    def test_failures_counted_per_forwarded_ip(self, client):
        self._post(client, "wrong", ip="203.0.113.7")
        # 计数记在真实 IP 上,而不是反代的 127.0.0.1
        assert "203.0.113.7" in routes._login_fails
        assert "127.0.0.1" not in routes._login_fails

    def test_locked_out_after_limit(self, client):
        for _ in range(routes._LOGIN_FAIL_LIMIT):
            self._post(client, "wrong")
        resp = self._post(client, "wrong")
        assert resp.status_code == 200  # 停在登录页
        assert "登录失败次数过多" in resp.get_data(as_text=True)

    def test_correct_password_rejected_while_locked(self, client, monkeypatch):
        for _ in range(routes._LOGIN_FAIL_LIMIT):
            self._post(client, "wrong")
        # 锁定期内即使密码正确也不放行,且不应去派生密钥
        called = []
        monkeypatch.setattr(
            routes, "derive_key", lambda p, s: called.append(1) or b"x" * 32
        )
        resp = self._post(client, RIGHT_PASSWORD)
        assert resp.status_code == 200
        assert called == []

    def test_success_clears_counter(self, client, monkeypatch):
        monkeypatch.setattr(routes, "init_manager", lambda m: None)
        monkeypatch.setattr(routes, "EngineManager", lambda db, key: object())
        self._post(client, "wrong")
        assert routes._login_fails
        resp = self._post(client, RIGHT_PASSWORD)
        assert resp.status_code in (301, 302)  # 登录成功,重定向到面板
        assert routes._login_fails == {}
