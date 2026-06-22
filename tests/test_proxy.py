"""tests/test_proxy.py — 代理串解析 + contextvar 注入(不触真网)。"""

from unittest.mock import patch
from api.proxy import parse_proxy, http_get, use_proxy


def test_blank_returns_none():
    assert parse_proxy("") is None
    assert parse_proxy(None) is None
    assert parse_proxy("   ") is None


def test_host_port_user_pass():
    assert (
        parse_proxy("gate.kookeey.info:1000:user:pass")
        == "http://user:pass@gate.kookeey.info:1000"
    )


def test_host_port_only_no_auth():
    assert parse_proxy("1.2.3.4:8080") == "http://1.2.3.4:8080"


def test_password_with_colon_kept():
    # 密码含 ':' 用 split(':',3) 兜住,且 URL 编码(':' -> %3A)。
    assert parse_proxy("host:1000:user:pa:ss") == "http://user:pa%3Ass@host:1000"


def test_already_a_url_passthrough():
    assert parse_proxy("http://u:p@h:1000") == "http://u:p@h:1000"
    assert parse_proxy("https://u:p@h:1000") == "https://u:p@h:1000"


def test_strips_whitespace():
    assert parse_proxy("  1.2.3.4:8080  ") == "http://1.2.3.4:8080"


def test_http_get_no_proxy_outside_context():
    with patch("api.proxy.requests.get") as g:
        http_get("http://x")
    assert "proxies" not in g.call_args.kwargs


def test_http_get_injects_proxies_inside_use_proxy():
    p = "http://u:p@h:1000"
    with patch("api.proxy.requests.get") as g:
        with use_proxy(p):
            http_get("http://x")
    assert g.call_args.kwargs["proxies"] == {"http": p, "https": p}


def test_http_get_none_proxy_no_injection():
    with patch("api.proxy.requests.get") as g:
        with use_proxy(None):
            http_get("http://x")
    assert "proxies" not in g.call_args.kwargs


def test_use_proxy_resets_after_context():
    from api.proxy import current_proxy

    with use_proxy("http://u:p@h:1000"):
        assert current_proxy.get() == "http://u:p@h:1000"
    assert current_proxy.get() is None


def test_dispatcher_none_proxy_uses_default_client():
    from api.proxy import _ProxyDispatchingClient
    from unittest.mock import MagicMock

    default = MagicMock()
    disp = _ProxyDispatchingClient(default)
    disp.request(method="GET", url="http://x")
    default.request.assert_called_once_with(method="GET", url="http://x")


def test_dispatcher_proxied_creates_and_caches_proxy_client():
    from api.proxy import _ProxyDispatchingClient
    from unittest.mock import MagicMock

    default = MagicMock()
    disp = _ProxyDispatchingClient(default)
    with patch("api.proxy.httpx.Client") as Client:
        with use_proxy("http://u:p@h:1"):
            disp.request(method="GET", url="http://x")
            disp.request(method="GET", url="http://y")  # 第二次复用缓存 client
    Client.assert_called_once_with(http2=True, proxy="http://u:p@h:1")
    assert Client.return_value.request.call_count == 2
    default.request.assert_not_called()


def test_install_clob_proxy_swaps_global_and_idempotent():
    import api.proxy as P
    from py_clob_client_v2.http_helpers import helpers

    saved, saved_flag = helpers._http_client, P._installed
    try:
        P._installed = False
        P.install_clob_proxy()
        first = helpers._http_client
        assert isinstance(first, P._ProxyDispatchingClient)
        P.install_clob_proxy()  # 幂等:不二次包裹
        assert helpers._http_client is first
    finally:
        helpers._http_client, P._installed = saved, saved_flag


def test_proxied_client_sets_context_on_call_and_passes_through_attrs():
    from api.proxy import _ProxiedClient, current_proxy

    seen = {}

    class Target:
        attr = 42

        def do(self, x):
            seen["proxy"] = current_proxy.get()
            return x + 1

    pc = _ProxiedClient(Target(), "http://u:p@h:1")
    assert pc.do(41) == 42
    assert seen["proxy"] == "http://u:p@h:1"  # 调用期 contextvar 已设为本钱包代理
    assert pc.attr == 42  # 非可调用属性原样透传
    assert current_proxy.get() is None  # 调用后复位


def test_proxied_client_none_proxy_runs_direct():
    from api.proxy import _ProxiedClient, current_proxy

    seen = {}

    class Target:
        def do(self):
            seen["proxy"] = current_proxy.get()

    _ProxiedClient(Target(), None).do()
    assert seen["proxy"] is None
