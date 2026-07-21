"""tests/test_proxy.py — 代理串解析 + contextvar 注入(不触真网)。"""

import pytest
from unittest.mock import patch
from api.proxy import ProxyUnreachable, parse_proxy, http_get, probe_proxy, use_proxy


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


def test_socks5_prefix_shorthand():
    # socks5 一律用 socks5h(远端 DNS):本地解析既会从真实 IP 发 DNS 查询,实测也连不通。
    assert (
        parse_proxy("socks5:181.215.29.13:12324:user:pass")
        == "socks5h://user:pass@181.215.29.13:12324"
    )


def test_socks5_prefix_without_auth():
    assert parse_proxy("socks5:1.2.3.4:12324") == "socks5h://1.2.3.4:12324"


def test_socks5_url_normalized_to_socks5h():
    assert parse_proxy("socks5://u:p@h:1000") == "socks5h://u:p@h:1000"
    assert parse_proxy("socks5h://u:p@h:1000") == "socks5h://u:p@h:1000"


class _Resp:
    def __init__(self, payload=""):
        self.text = payload

    def json(self):
        return {"ip": "9.9.9.9"}

    def raise_for_status(self):
        pass


def _fake_get(ok_scheme):
    """构造 requests.get 假实现:只有 proxies 用 ok_scheme 开头时才通。"""

    def get(url, **kw):
        proxy = (kw.get("proxies") or {}).get("https", "")
        if not proxy.startswith(ok_scheme):
            raise Exception("connect failed")
        return _Resp()

    return get


def test_probe_blank_is_direct_and_touches_no_network():
    with patch("api.proxy.requests.get") as g:
        assert probe_proxy("") == ("", None)
    g.assert_not_called()


def test_probe_http_proxy_stored_unchanged():
    with patch("api.proxy.requests.get", side_effect=_fake_get("http://")):
        stored, ip = probe_proxy("1.2.3.4:8080:u:p")
    assert stored == "1.2.3.4:8080:u:p"
    assert ip == "9.9.9.9"


def test_probe_falls_back_to_socks5_and_stores_prefix():
    with patch("api.proxy.requests.get", side_effect=_fake_get("socks5h://")):
        stored, _ = probe_proxy("181.215.29.13:12324:14a917ac7ad40:a811dafe34")
    assert stored == "socks5:181.215.29.13:12324:14a917ac7ad40:a811dafe34"


def test_probe_raises_when_neither_protocol_works():
    with patch("api.proxy.requests.get", side_effect=Exception("down")):
        with pytest.raises(ProxyUnreachable):
            probe_proxy("1.2.3.4:8080")


def test_probe_keeps_explicit_scheme_and_does_not_guess():
    # 已显式写明协议 -> 只验连通,不再试另一种(否则会把用户写死的协议改掉)。
    with patch("api.proxy.requests.get", side_effect=_fake_get("socks5h://")) as g:
        stored, _ = probe_proxy("socks5:1.2.3.4:12324")
    assert stored == "socks5:1.2.3.4:12324"
    assert all(
        (c.kwargs.get("proxies") or {}).get("https", "").startswith("socks5h://")
        for c in g.call_args_list
    )


def test_probe_survives_exit_ip_lookup_failure():
    calls = []

    def get(url, **kw):
        calls.append(url)
        if "ipify" in url:  # 出口 IP 只是回显,查不到不该让保存失败
            raise Exception("blocked")
        return _Resp()

    with patch("api.proxy.requests.get", side_effect=get):
        stored, ip = probe_proxy("1.2.3.4:8080")
    assert stored == "1.2.3.4:8080"
    assert ip is None


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
