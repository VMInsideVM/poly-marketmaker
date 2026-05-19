"""tests/test_log_buffer.py"""

import logging
import pytest
from config import LOG_BUFFER_SIZE
from utils.log_buffer import BufferLogHandler, get_logs, clear_logs


def _record(msg, level=logging.INFO, name="engine.test", args=()):
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


@pytest.fixture(autouse=True)
def _clean():
    clear_logs()
    yield
    clear_logs()


def test_emit_appends_entry_with_fields():
    h = BufferLogHandler()
    h.emit(
        _record(
            "hello %s", args=("world",), level=logging.WARNING, name="engine.monitor"
        )
    )
    logs = get_logs()
    assert len(logs) == 1
    e = logs[0]
    assert e["message"] == "hello world"
    assert e["level"] == "WARNING"
    assert e["logger"] == "engine.monitor"
    assert isinstance(e["ts"], float)


def test_get_logs_is_time_order_oldest_first():
    h = BufferLogHandler()
    h.emit(_record("first"))
    h.emit(_record("second"))
    msgs = [e["message"] for e in get_logs()]
    assert msgs == ["first", "second"]


def test_ring_buffer_evicts_oldest_beyond_maxlen():
    h = BufferLogHandler()
    for i in range(LOG_BUFFER_SIZE + 5):
        h.emit(_record(f"m{i}"))
    logs = get_logs()
    assert len(logs) == LOG_BUFFER_SIZE
    assert logs[0]["message"] == "m5"
    assert logs[-1]["message"] == f"m{LOG_BUFFER_SIZE + 4}"


def test_clear_logs_empties_buffer():
    h = BufferLogHandler()
    h.emit(_record("x"))
    clear_logs()
    assert get_logs() == []


def test_emit_never_raises_on_bad_record():
    h = BufferLogHandler()
    bad = _record("%d", args=("x",))  # getMessage() will raise TypeError (wrong type)
    h.emit(bad)  # must NOT raise
    assert get_logs() == []
