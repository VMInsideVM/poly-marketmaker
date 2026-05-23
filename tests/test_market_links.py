"""tests/test_market_links.py"""

from engine.market_links import market_url, enrich_with_market_meta


def test_market_url_prefers_market_slug():
    assert (
        market_url({"market_slug": "abc", "event_slug": "xyz"})
        == "https://polymarket.com/market/abc"
    )


def test_market_url_falls_back_to_event_slug():
    assert (
        market_url({"market_slug": "", "event_slug": "xyz"})
        == "https://polymarket.com/event/xyz"
    )


def test_market_url_empty_when_no_slug():
    assert market_url({"market_slug": "", "event_slug": ""}) == ""


def test_market_url_empty_entry():
    assert market_url(None) == ""
    assert market_url({}) == ""


def test_enrich_hit_adds_name_and_url():
    rows = [{"market": "0xc1"}]
    meta = {"0xc1": {"name": "市场A", "market_slug": "a", "event_slug": ""}}
    enrich_with_market_meta(rows, meta, "market")
    assert rows[0]["market_name"] == "市场A"
    assert rows[0]["market_url"] == "https://polymarket.com/market/a"


def test_enrich_miss_blank_url_and_name():
    rows = [{"market": "0xUNKNOWN"}]
    enrich_with_market_meta(rows, {}, "market")
    assert rows[0]["market_url"] == ""
    assert rows[0]["market_name"] == ""


def test_enrich_does_not_overwrite_existing_name():
    rows = [{"market_id": "0xc1", "market_name": "持仓Title"}]
    meta = {"0xc1": {"name": "扫描名", "market_slug": "a", "event_slug": ""}}
    enrich_with_market_meta(rows, meta, "market_id")
    assert rows[0]["market_name"] == "持仓Title"
    assert rows[0]["market_url"] == "https://polymarket.com/market/a"


def test_enrich_respects_id_key():
    rows = [{"market_id": "0xc1"}]
    meta = {"0xc1": {"name": "X", "market_slug": "s", "event_slug": ""}}
    enrich_with_market_meta(rows, meta, "market_id")
    assert rows[0]["market_url"] == "https://polymarket.com/market/s"


def test_market_url_strips_whitespace_slug():
    assert (
        market_url({"market_slug": "  abc  ", "event_slug": ""})
        == "https://polymarket.com/market/abc"
    )


def test_market_url_whitespace_only_market_slug_falls_back_to_event():
    assert (
        market_url({"market_slug": "   ", "event_slug": "xyz"})
        == "https://polymarket.com/event/xyz"
    )


def test_enrich_multiple_rows_mixed_hit_miss():
    rows = [{"market": "0xc1"}, {"market": "0xUNKNOWN"}]
    meta = {"0xc1": {"name": "市场A", "market_slug": "a", "event_slug": ""}}
    enrich_with_market_meta(rows, meta, "market")
    assert rows[0]["market_name"] == "市场A"
    assert rows[0]["market_url"] == "https://polymarket.com/market/a"
    assert rows[1]["market_name"] == ""
    assert rows[1]["market_url"] == ""
