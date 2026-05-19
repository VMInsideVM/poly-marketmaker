# engine/monitor_status.py
"""Thread-safe in-process snapshot of per-order monitor processing.

One snapshot per wallet, overwritten each monitor tick. The 监控状态
page reads the merged view; nothing here touches trading decisions.
"""

import threading

_LOCK = threading.Lock()
# wallet_address -> {"ts": float, "rows": list[dict]}
_SNAPSHOTS: dict = {}


def set_snapshot(wallet: str, rows: list, ts: float) -> None:
    """Overwrite this wallet's snapshot (latest tick wins)."""
    with _LOCK:
        _SNAPSHOTS[wallet] = {"ts": ts, "rows": list(rows)}


def get_snapshot() -> dict:
    """Merged view across wallets: {"updated": max ts or 0, "rows": [...]}."""
    with _LOCK:
        snaps = list(_SNAPSHOTS.values())
    rows: list = []
    updated = 0.0
    for s in sorted(snaps, key=lambda s: s["ts"]):
        rows.extend(s["rows"])
        updated = max(updated, s["ts"])
    return {"updated": updated if updated else 0, "rows": rows}


def clear_snapshot() -> None:
    with _LOCK:
        _SNAPSHOTS.clear()
