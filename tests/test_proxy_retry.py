"""tests/test_proxy_retry.py — 代理连接瞬时失败的自动重试(容错烂代理)。

根因:钱包用的轮换代理约 30% 连接超时(实测),而一轮下单要连续多个代理调用,
任一超时即整轮放弃 -> 几乎挂不出单。对策:仅对"连接建立阶段"失败(请求从未送达
Polymarket)自动退避重试;读超时/连接后错误不重试(POST 可能已提交,避免重复下单)。
"""

import httpx
import pytest
import requests

import api.proxy as proxy


def test_retry_on_connect_error_retries_then_succeeds():
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise requests.exceptions.ProxyError("Unable to connect to proxy")
        return "ok"

    assert proxy._retry_on_connect_error(fn, attempts=3, backoff=0) == "ok"
    assert len(calls) == 3


def test_retry_exhausts_and_raises_last_connect_error():
    calls = []

    def fn():
        calls.append(1)
        raise requests.exceptions.ConnectTimeout("connect timed out")

    with pytest.raises(requests.exceptions.ConnectTimeout):
        proxy._retry_on_connect_error(fn, attempts=3, backoff=0)
    assert len(calls) == 3


def test_retry_does_not_retry_read_timeout():
    # 读超时 = 请求可能已送达(POST 可能已下单)-> 绝不重试,立即上抛。
    calls = []

    def fn():
        calls.append(1)
        raise httpx.ReadTimeout("read timed out")

    with pytest.raises(httpx.ReadTimeout):
        proxy._retry_on_connect_error(fn, attempts=3, backoff=0)
    assert len(calls) == 1


def test_http_get_retries_transient_proxy_error_and_stays_on_proxy(monkeypatch):
    calls = []

    class Resp:
        pass

    def fake_get(url, **kw):
        calls.append(kw.get("proxies"))
        if len(calls) < 2:
            raise requests.exceptions.ProxyError("boom")
        return Resp()

    monkeypatch.setattr(proxy.requests, "get", fake_get)
    monkeypatch.setattr(proxy.time, "sleep", lambda *_: None)
    with proxy.use_proxy("http://p:1"):
        r = proxy.http_get("http://x", timeout=5)
    assert isinstance(r, Resp)
    assert len(calls) == 2
    # 重试仍走代理,绝不直连(不泄露真实 IP)
    assert calls[-1] == {"http": "http://p:1", "https": "http://p:1"}


def test_dispatcher_retries_httpx_connect_error():
    calls = []

    class FakeClient:
        def request(self, **kw):
            calls.append(1)
            if len(calls) < 2:
                raise httpx.ConnectError("cannot connect to proxy")
            return "OK"

    disp = proxy._ProxyDispatchingClient(FakeClient())
    r = disp.request(method="GET", url="http://x")
    assert r == "OK"
    assert len(calls) == 2


def test_dispatcher_does_not_retry_read_timeout_on_post():
    # POST(下单)读超时不重试 —— 请求可能已到 Polymarket,重试会重复下单。
    calls = []

    class FakeClient:
        def request(self, **kw):
            calls.append(kw.get("method"))
            raise httpx.ReadTimeout("read timed out")

    disp = proxy._ProxyDispatchingClient(FakeClient())
    with pytest.raises(httpx.ReadTimeout):
        disp.request(method="POST", url="http://x")
    assert calls == ["POST"]
