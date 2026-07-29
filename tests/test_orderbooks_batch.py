"""tests/test_orderbooks_batch.py — 批量取盘口 get_orderbooks。

CLOB 的 POST /books 一次返回多个盘口,把 Step3/离场每单一次的 GET /book 折成
一次请求(实测 50 个盘口 0.57s,而单发 0.48s/个)。
"""

from unittest.mock import MagicMock, patch

import pytest

from api.polymarket_api import PolymarketAPI


@pytest.fixture
def api():
    with (
        patch("api.polymarket_api.derive_deposit_address", return_value="0xFunder"),
        patch("api.polymarket_api.ClobClient") as mock_clob,
    ):
        client = MagicMock()
        client.get_address.return_value = "0xEOA"
        mock_clob.return_value = client
        yield PolymarketAPI("0xprivkey")


def _book(asset_id, best_bid):
    return {
        "asset_id": asset_id,
        "tick_size": "0.01",
        "bids": [{"price": str(best_bid), "size": "100"}],
        "asks": [{"price": "0.99", "size": "100"}],
    }


def test_missing_token_maps_to_none_not_shifted(api):
    """返回里缺的 token 记 None,后面的 token 绝不能顶上它的位置。

    实测 CLOB /books 对失效 token **静默丢弃**:HTTP 200,返回条数 < 请求条数。
    若按下标 zip(token_ids, resp),C 的盘口就会被安到 B 头上 —— Step3 拿着别人的
    盘口判 B 该不该撤单,离场拿着别人的买一给 B 定卖价。必须按 asset_id 对齐。
    """
    api.client.get_order_books.return_value = [_book("A", 0.10), _book("C", 0.30)]

    got = api.get_orderbooks(["A", "B", "C"])

    assert set(got) == {"A", "B", "C"}
    assert got["A"]["bids"][0]["price"] == "0.1"
    assert got["B"] is None, "缺的 token 必须是 None(取数失败),不是别人的盘口"
    assert got["C"]["bids"][0]["price"] == "0.3", "C 的盘口不能被挪到 B 上"


def test_runs_inside_wallet_proxy_context(api):
    """取数期间 current_proxy 必须是本钱包的代理,否则批量盘口走真实 IP。

    /books 是 POST,与单发 GET /book 共用 helpers.request -> 被替换的全局
    _http_client,所以只要 contextvar 设对就走对代理。漏进 _PROXIED_METHODS 的话
    contextvar 是 None,分发器取 None 键 = 原始直连 client —— 本项目对代理的红线是
    「绝不回退直连」,这条断言就是那条红线。
    """
    from api.proxy import current_proxy

    api.proxy_url = "http://u:p@host:1000"
    seen = {}

    def _capture(_params):
        seen["proxy"] = current_proxy.get()
        return []

    api.client.get_order_books.side_effect = _capture

    api.get_orderbooks(["A"])

    assert seen["proxy"] == "http://u:p@host:1000"
    assert current_proxy.get() is None, "调用后必须复位,不能污染调用线程"


def test_splits_into_batches(api):
    """token 数超过单批上限时必须分批发,每批都不超上限。

    实测边界:一次传 500 个 HTTP 200,传 1000 / 2000 都是 HTTP 400。整批同生共死,
    所以批要留足余量而不是贴着 500 发。
    """
    from api.polymarket_api import _BOOKS_BATCH_SIZE

    tokens = [f"T{i}" for i in range(250)]
    calls = []

    def _capture(params):
        calls.append(len(params))
        return [_book(p.token_id, 0.5) for p in params]

    api.client.get_order_books.side_effect = _capture

    got = api.get_orderbooks(tokens)

    assert len(calls) > 1, "250 个 token 必须分批,不能一次全塞进去"
    assert max(calls) <= _BOOKS_BATCH_SIZE
    assert sum(calls) == 250, "分批不能漏 token"
    assert set(got) == set(tokens)
    assert all(got[t] is not None for t in tokens), "分批结果必须全部合并回来"


def test_failed_batch_maps_to_none_others_survive(api):
    """一批请求失败只让那批记 None,其余批照常返回。

    /books 整批同生共死:一个批次抛异常时整批没有结果。若不隔离,一次抖动就让整轮
    盘口全空 —— Step3 全跳过、离场全部 ⚠️裸奔。
    """
    from api.polymarket_api import _BOOKS_BATCH_SIZE

    tokens = [f"T{i}" for i in range(_BOOKS_BATCH_SIZE + 10)]
    state = {"n": 0}

    def _capture(params):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("boom")
        return [_book(p.token_id, 0.5) for p in params]

    api.client.get_order_books.side_effect = _capture

    got = api.get_orderbooks(tokens)

    first = tokens[:_BOOKS_BATCH_SIZE]
    rest = tokens[_BOOKS_BATCH_SIZE:]
    assert all(got[t] is None for t in first), "失败批的 token 记 None"
    assert all(got[t] is not None for t in rest), "其余批不受影响"
