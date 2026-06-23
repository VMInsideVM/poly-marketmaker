"""tests/test_help_route.py — 使用说明页面路由渲染。"""

import pytest
from web import routes


@pytest.fixture
def client():
    routes.app.config["TESTING"] = True
    with routes.app.test_client() as c:
        with c.session_transaction() as s:
            s["logged_in"] = True
        yield c


def test_help_page_renders(client):
    r = client.get("/help")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "使用说明" in body
    assert "强平阈值" in body  # 参数详解渲染到位


def test_help_requires_login():
    routes.app.config["TESTING"] = True
    with routes.app.test_client() as c:
        r = c.get("/help")
        assert r.status_code in (301, 302)  # 未登录跳转
