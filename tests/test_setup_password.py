"""tests/test_setup_password.py — 首次设置密码的强度要求。"""

import pytest

import web.routes as routes


class _EmptyDB:
    """还没设过密码的库。save_password 记录调用。"""

    def __init__(self):
        self.saved = []

    def get_password(self):
        return None, None

    def save_password(self, hashed, salt):
        self.saved.append((hashed, salt))


@pytest.fixture
def db(monkeypatch):
    d = _EmptyDB()
    monkeypatch.setattr(routes, "db", d)
    routes.app.config["TESTING"] = True
    yield d
    # 设置成功的用例会设进程级密钥,清掉以免污染其他测试
    routes.set_encryption_key(None)


def _post(pw, confirm=None):
    return routes.app.test_client().post(
        "/setup", data={"password": pw, "confirm": confirm if confirm is not None else pw}
    )


def test_min_length_is_12():
    assert routes._MIN_PASSWORD_LEN == 12


def test_rejects_short_password(db):
    resp = _post("shortpw12")  # 9 位
    assert resp.status_code == 200
    assert "12" in resp.get_data(as_text=True)
    assert db.saved == []


def test_rejects_exactly_11(db):
    _post("a" * 11)
    assert db.saved == []


def test_accepts_12(db, monkeypatch):
    monkeypatch.setattr(routes, "init_manager", lambda m: None)
    monkeypatch.setattr(routes, "EngineManager", lambda d, key: object())
    resp = _post("a" * 12)
    assert resp.status_code in (301, 302)
    assert len(db.saved) == 1


def test_mismatch_still_rejected(db):
    _post("a" * 12, confirm="b" * 12)
    assert db.saved == []
