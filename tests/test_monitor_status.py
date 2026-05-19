"""tests/test_monitor_status.py"""

import pytest
from engine.monitor_status import set_snapshot, get_snapshot, clear_snapshot


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
