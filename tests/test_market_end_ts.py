"""tests/test_market_end_ts.py — pure parsing of CLOB market end date."""

from datetime import datetime, timezone

from api.polymarket_api import _end_ts_from_market


def test_parses_iso_z():
    ts = _end_ts_from_market({"end_date_iso": "2099-01-01T00:00:00Z"})
    assert ts is not None
    assert ts > 4_000_000_000  # well past 2096


def test_parses_iso_offset():
    ts = _end_ts_from_market({"end_date_iso": "2099-01-01T00:00:00+00:00"})
    assert ts is not None
    assert ts > 4_000_000_000


def test_parses_date_only():
    ts = _end_ts_from_market({"end_date_iso": "2099-01-01"})
    assert ts is not None
    assert ts > 4_000_000_000


def test_none_when_field_missing():
    assert _end_ts_from_market({}) is None
    assert _end_ts_from_market({"end_date_iso": ""}) is None


def test_none_when_unparseable():
    assert _end_ts_from_market({"end_date_iso": "not-a-date"}) is None


def test_none_when_not_a_dict():
    assert _end_ts_from_market(None) is None


def test_parses_end_date_fallback_key():
    # falls back to `end_date` when `end_date_iso` is absent
    ts = _end_ts_from_market({"end_date": "2099-01-01T00:00:00Z"})
    assert ts is not None
    assert ts > 4_000_000_000


def test_prefers_end_date_iso_over_end_date():
    # end_date_iso wins when both present
    ts_iso = _end_ts_from_market(
        {"end_date_iso": "2099-01-01", "end_date": "2000-01-01"}
    )
    ts_only = _end_ts_from_market({"end_date_iso": "2099-01-01"})
    assert ts_iso == ts_only


def test_z_and_offset_are_same_instant():
    # the Z->+00:00 rewrite must be transparent
    ts_z = _end_ts_from_market({"end_date_iso": "2099-01-01T00:00:00Z"})
    ts_offset = _end_ts_from_market({"end_date_iso": "2099-01-01T00:00:00+00:00"})
    assert ts_z == ts_offset


def test_date_only_interpreted_as_utc_not_local():
    # a naive (date-only / offset-less) value must be treated as UTC, not the
    # host's local timezone — otherwise settlement-day math is off by hours.
    ts = _end_ts_from_market({"end_date_iso": "2099-01-01"})
    expected = datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp()
    assert ts == expected
