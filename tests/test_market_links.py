"""tests/test_market_links.py"""

from unittest.mock import MagicMock

from engine.market_links import market_url, enrich_with_market_meta, ensure_market_meta


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


def test_ensure_only_queries_missing_ids():
    db = MagicMock()
    db.get_market_meta.return_value = {
        "0xKNOWN": {"name": "K", "market_slug": "", "event_slug": ""}
    }
    fetch = MagicMock(return_value={})
    ensure_market_meta(["0xKNOWN", "0xNEW"], db, fetch)
    fetch.assert_called_once()
    called_ids = list(fetch.call_args.args[0])
    assert "0xNEW" in called_ids and "0xKNOWN" not in called_ids


def test_ensure_upserts_resolved_name():
    db = MagicMock()
    db.get_market_meta.return_value = {}
    fetch = MagicMock(
        return_value={"0xNEW": {"name": "Q", "market_slug": "s", "event_slug": "e"}}
    )
    ensure_market_meta(["0xNEW"], db, fetch)
    db.upsert_market_meta.assert_any_call("0xNEW", "Q", "s", "e")


def test_ensure_negative_caches_true_miss():
    db = MagicMock()
    db.get_market_meta.return_value = {}
    fetch = MagicMock(return_value={})  # Gamma 应答了但不含该 id
    ensure_market_meta(["0xMISS"], db, fetch)
    db.upsert_market_meta.assert_any_call("0xMISS", "", "", "")


def test_ensure_no_upsert_on_fetch_failure():
    db = MagicMock()
    db.get_market_meta.return_value = {}
    fetch = MagicMock(side_effect=Exception("gamma down"))
    ensure_market_meta(["0xNEW"], db, fetch)  # 不得抛出
    db.upsert_market_meta.assert_not_called()


def test_ensure_skips_fetch_when_all_present():
    db = MagicMock()
    db.get_market_meta.return_value = {
        "0xA": {"name": "A", "market_slug": "", "event_slug": ""}
    }
    fetch = MagicMock()
    ensure_market_meta(["0xA"], db, fetch)
    fetch.assert_not_called()


def test_ensure_ignores_empty_ids():
    db = MagicMock()
    db.get_market_meta.return_value = {}
    fetch = MagicMock(return_value={})
    ensure_market_meta(["", None], db, fetch)
    fetch.assert_not_called()
