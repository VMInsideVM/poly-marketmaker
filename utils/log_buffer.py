# utils/log_buffer.py
"""In-memory ring buffer logging handler for the 运行日志 page."""

import logging
from collections import deque
from config import LOG_BUFFER_SIZE

_BUFFER: deque = deque(maxlen=LOG_BUFFER_SIZE)


class BufferLogHandler(logging.Handler):
    """Appends each log record to a bounded in-memory deque."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _BUFFER.append(
                {
                    "ts": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                }
            )
        except Exception:
            self.handleError(record)


def get_logs() -> list:
    """Snapshot of buffered log entries, oldest first."""
    return list(_BUFFER)


def clear_logs() -> None:
    _BUFFER.clear()
