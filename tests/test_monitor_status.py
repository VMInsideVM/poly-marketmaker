"""tests/test_monitor_status.py"""

import pytest
from engine.monitor_status import set_snapshot, get_snapshot, clear_snapshot
from engine import monitor_status


@pytest.fixture(autouse=True)
def _clean():
    clear_snapshot()
    yield
    clear_snapshot()


def test_empty_returns_zero_updated_and_no_rows():
    assert get_snapshot() == {"updated": 0, "rows": []}


def test_set_then_get_roundtrip():
    set_snapshot("0xAB", [{"market": "m1", "stage": "Step3"}], 100.0)
    snap = get_snapshot()
    assert snap["updated"] == 100.0
    assert snap["rows"] == [{"market": "m1", "stage": "Step3"}]


def test_same_wallet_second_set_overwrites_not_appends():
    set_snapshot("0xAB", [{"market": "old"}], 100.0)
    set_snapshot("0xAB", [{"market": "new"}], 200.0)
    snap = get_snapshot()
    assert snap["rows"] == [{"market": "new"}]
    assert snap["updated"] == 200.0


def test_multi_wallet_merge_and_updated_is_max_ts():
    set_snapshot("0xAB", [{"market": "a"}], 100.0)
    set_snapshot("0xCD", [{"market": "b"}], 250.0)
    snap = get_snapshot()
    markets = sorted(r["market"] for r in snap["rows"])
    assert markets == ["a", "b"]
    assert len(snap["rows"]) == 2
    assert snap["updated"] == 250.0


def test_clear_snapshot_empties():
    set_snapshot("0xAB", [{"market": "a"}], 100.0)
    clear_snapshot()
    assert get_snapshot() == {"updated": 0, "rows": []}


def test_get_snapshot_returns_independent_row_copies():
    # Mutating rows returned by get_snapshot must NOT corrupt the stored snapshot,
    # because the Flask route enriches (mutates) the returned rows in place.
    monitor_status.set_snapshot("0xW", [{"market": "0xc1", "side": "BUY"}], 100.0)
    first = monitor_status.get_snapshot()
    first["rows"][0]["market_name"] = "INJECTED"
    first["rows"][0]["market"] = "TAMPERED"
    second = monitor_status.get_snapshot()
    assert "market_name" not in second["rows"][0]
    assert second["rows"][0]["market"] == "0xc1"


def test_get_snapshot_merges_and_reports_max_ts():
    monitor_status.set_snapshot("0xA", [{"market": "a"}], 100.0)
    monitor_status.set_snapshot("0xB", [{"market": "b"}], 200.0)
    snap = monitor_status.get_snapshot()
    assert snap["updated"] == 200.0
    assert {r["market"] for r in snap["rows"]} == {"a", "b"}
