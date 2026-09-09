# Append-only audit trails, answering two questions after the fact:
#   "who commanded that throttle, and when?"          → throttle log
#   "when did this scale factor change, and to what?" → calibration log
#
# Plain JSONL under Logs/, one file per month. Nothing here ever
# rewrites or truncates an existing line — a log that can be edited in
# place is not worth keeping.
#
# Dragging the throttle slider emits a command roughly every 80 ms, so a
# manual throttle is buffered and written only once it stops changing
# (flush_pending_throttle, driven by a 0.5 s task in main.py). Profile
# phases, cuts and calibration writes are discrete and written at once.

from __future__ import annotations

import json
import threading
import time
from datetime import datetime

from app_paths import logs_dir
from constants import (
    AUDIT_CALIBRATION, AUDIT_EV_CUT, AUDIT_EV_RESET, AUDIT_EV_SET,
    AUDIT_THROTTLE, CAL_SOURCE_DASHBOARD, FILE_ENCODING, LOG_AUDIT,
    THROTTLE_SOURCE_CUT, THROTTLE_SOURCE_MANUAL,
)

# How long a manual throttle value must stay unchanged before it counts
# as the operator's intent rather than a point they dragged through.
THROTTLE_SETTLE_S = 0.6

LOG_MONTH_FORMAT = "%Y-%m"

_lock = threading.Lock()

# Buffered manual throttle: {"us", "username", "source", "at"}
_pending_throttle: dict | None = None
_last_logged_us: int | None = None


def _log_path(category: str) -> str:
    return str(logs_dir() / f"{category}-{datetime.now():{LOG_MONTH_FORMAT}}.jsonl")


def _append(category: str, record: dict) -> None:
    """Append one JSON line. Never raises — a failed log must not break a test."""
    record = {"ts": datetime.now().isoformat(timespec="milliseconds"), **record}
    try:
        with _lock:
            with open(_log_path(category), "a", encoding=FILE_ENCODING) as fh:
                fh.write(json.dumps(record) + "\n")
    except OSError as e:
        print(f"{LOG_AUDIT} Could not write {category} log: {e}")


def note_throttle(username: str, us: int, source: str = THROTTLE_SOURCE_MANUAL) -> None:
    """Buffer a manual throttle command; written once it settles.

    A cut to idle is logged immediately instead — it's a safety action,
    and waiting even half a second would be wrong if the process dies.
    """
    global _pending_throttle, _last_logged_us

    if source == THROTTLE_SOURCE_CUT:
        _pending_throttle = None
        _last_logged_us = us
        _append(AUDIT_THROTTLE, {"event": AUDIT_EV_CUT, "user": username, "us": us})
        return

    _pending_throttle = {"us": us, "username": username,
                         "source": source, "at": time.monotonic()}


def flush_pending_throttle(force: bool = False) -> None:
    """Write the buffered throttle value if it has stopped moving."""
    global _pending_throttle, _last_logged_us

    p = _pending_throttle
    if p is None:
        return
    if not force and (time.monotonic() - p["at"]) < THROTTLE_SETTLE_S:
        return

    _pending_throttle = None
    if p["us"] == _last_logged_us:
        return                                    # Settled back on the value already on record
    _last_logged_us = p["us"]
    _append(AUDIT_THROTTLE, {"event": AUDIT_EV_SET, "user": p["username"],
                             "us": p["us"], "source": p["source"]})


def log_profile(username: str, event: str, **fields) -> None:
    """Record a profile lifecycle event (start / stop / done / abort)."""
    _append(AUDIT_THROTTLE, {"event": f"profile_{event}", "user": username, **fields})


def log_serial(event: str, **fields) -> None:
    """Record serial link lifecycle — connect, disconnect, unexpected drop."""
    _append(AUDIT_THROTTLE, {"event": f"serial_{event}", **fields})


def log_calibration(username: str, key: str, old, new,
                    source: str = CAL_SOURCE_DASHBOARD) -> None:
    """Record one calibration change, with the value it replaced."""
    _append(AUDIT_CALIBRATION, {"event": AUDIT_EV_SET, "user": username, "key": key,
                                "old": old, "new": new, "source": source})


def log_calibration_reset(username: str, previous: dict) -> None:
    """Record a reset-to-firmware-defaults, keeping what was wiped."""
    _append(AUDIT_CALIBRATION, {"event": AUDIT_EV_RESET, "user": username,
                                "previous": previous})


def read_recent(category: str, limit: int = 200) -> list[dict]:
    """Most recent entries from this month's log, newest first."""
    path = _log_path(category)
    try:
        with open(path, "r", encoding=FILE_ENCODING) as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue                              # A torn final line from a hard kill
        if len(out) >= limit:
            break
    return out
