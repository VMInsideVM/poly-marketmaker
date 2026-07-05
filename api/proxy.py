"""api/proxy.py — 每钱包 HTTP 代理:解析串 + contextvar 选路 + httpx/requests 注入。

py-clob-client-v2 的 HTTP 层是模块级全局 httpx.Client、不支持 per-instance 代理,
而同一 PolymarketAPI 实例又被采集线程(下单)与监控线程共用 -> 代理选择按「当前操作
的钱包」动态切换,用 contextvar。传输层(httpx 分发器 + requests 包装)读 contextvar,
操作边界(_tick/place_orders/采集器/构造)设 contextvar。
"""

import contextvars
import threading
import time
from contextlib import contextmanager
from urllib.parse import quote

import httpx
import requests

# 「当前操作的钱包」的代理 URL(或 None=直连)。传输层读它,操作边界设它。
current_proxy: contextvars.ContextVar = contextvars.ContextVar(
    "current_proxy", default=None
)


@contextmanager
def use_proxy(url):
    """在 with 块内把 current_proxy 设为 url(None=直连),退出复位。"""
    token = current_proxy.set(url)
    try:
        yield
    finally:
        current_proxy.reset(token)


# 仅这些「连接建立阶段」失败可安全重试:到代理的连接 / CONNECT 隧道从未建立,请求从未
# 送达 Polymarket,即使 POST 下单也不会重复提交。读超时 / 连接后错误不在此列(可能已送达)。
_CONNECT_ERRORS = (
    requests.exceptions.ConnectionError,  # 含 ProxyError / ConnectTimeout(requests)
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ProxyError,
)


def _retry_on_connect_error(fn, *, attempts=3, backoff=0.3):
    """调 fn();仅在代理连接建立阶段瞬时失败时退避重试,最多 attempts 次。

    烂 / 轮换代理常有约 30% 连接超时(实测),而一轮下单要连续多个代理调用、任一失败即
    整轮放弃 -> 几乎挂不出单。重试仍走同一代理(绝不直连,不泄露真实 IP)。只重试
    _CONNECT_ERRORS —— 请求从未送达,连 POST 下单也安全;读超时等「可能已送达」的错误
    立即上抛,避免重复下单。
    """
    last = None
    for i in range(attempts):
        try:
            return fn()
        except _CONNECT_ERRORS as e:
            last = e
            if i < attempts - 1 and backoff:
                time.sleep(backoff)
    raise last


def http_get(url, **kw):
    """requests.get 包装:current_proxy 非空时注入 proxies=(http/https 同一代理)。

    代理连接瞬时失败先退避重试(仍走代理,见 _retry_on_connect_error);重试用尽仍失败
    才抛给调用方跳过,绝不直连(不泄露真实 IP)。
    """
    proxy = current_proxy.get()
    if proxy:
        kw.setdefault("proxies", {"http": proxy, "https": proxy})
    return _retry_on_connect_error(lambda: requests.get(url, **kw))


class _ProxyDispatchingClient:
    """替换 py_clob_client_v2 的模块全局 ``_http_client``:按 ``current_proxy``
    选/缓存 per-proxy ``httpx.Client``。``helpers.request()`` 按模块全局名解析
    ``_http_client.request(**kw)``,故替换该名即对所有 CLOB 调用生效。

    None(直连)复用传入的原 client 以保持非代理钱包的现行为;每个代理 URL 懒建
    一个 http2 client 并缓存(多线程共用,建时加锁)。代理连接瞬时失败先退避重试
    (仍走代理);重试用尽才抛给调用方跳过,绝不直连。
    """

    def __init__(self, default_client):
        self._clients = {None: default_client}
        self._lock = threading.Lock()

    def _client_for(self, proxy):
        client = self._clients.get(proxy)
        if client is None:
            with self._lock:
                client = self._clients.get(proxy)
                if client is None:
                    client = httpx.Client(http2=True, proxy=proxy)
                    self._clients[proxy] = client
        return client

    def request(self, **kw):
        client = self._client_for(current_proxy.get())
        return _retry_on_connect_error(lambda: client.request(**kw))


class _ProxiedClient:
    """包装一个对象,使其每次**方法调用**都在 ``use_proxy(proxy)`` 内执行。

    用于 ClobClient:库的 HTTP 分发器读 ``current_proxy``,故只要调用期 contextvar
    被设为本钱包代理,该次 CLOB 网络调用即走对的代理——无论从采集线程、监控线程还是
    路由直接调用。非可调用属性(如 ``.builder``)原样透传,可正常读写其字段。
    """

    def __init__(self, target, proxy):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_proxy", proxy)

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr

        def wrapped(*a, **k):
            with use_proxy(self._proxy):
                return attr(*a, **k)

        return wrapped


_installed = False
_install_lock = threading.Lock()


def install_clob_proxy():
    """幂等:把 py_clob_client_v2 的全局 httpx client 换成按代理选路的分发器。

    PolymarketAPI 构造时调用一次即可(多次安全)。
    """
    global _installed
    if _installed:
        return
    with _install_lock:
        if _installed:
            return
        from py_clob_client_v2.http_helpers import helpers

        helpers._http_client = _ProxyDispatchingClient(helpers._http_client)
        _installed = True


def parse_proxy(raw) -> str | None:
    """把用户填的代理串转成 http(s) 代理 URL;空/None -> None。

    - ``host:port:user:pass`` -> ``http://user:pass@host:port``(凭证 URL 编码)
    - ``host:port``           -> ``http://host:port``
    - 已是 ``http://``/``https://`` 原样返回
    密码可能含 ``:`` -> ``split(":",3)`` 让前三段为 host/port/user、其余整体当密码。
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.lower().startswith(("http://", "https://")):
        return s
    parts = s.split(":", 3)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    host, port = parts[0], parts[1]
    if len(parts) == 2:
        return f"http://{host}:{port}"
    user = parts[2]
    pw = parts[3] if len(parts) >= 4 else ""
    auth = quote(user, safe="")
    if pw:
        auth += ":" + quote(pw, safe="")
    return f"http://{auth}@{host}:{port}"
