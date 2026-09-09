# Each recording streams to disk as it happens, in three files:
#
#   CSV/<name>_<timestamp>.csv        the run, one row per frame. Column
#       names are deliberately ones the Data Analysis tab auto-detects,
#       so a recorded run feeds straight back in.
#   CSV/<name>_<timestamp>.meta.json  operator, times, and the
#       CALIBRATION IN FORCE — without it a number in the CSV can't be
#       traced to the scale factors that produced it.
#   Recovery/<name>_<timestamp>.jsonl the raw frames. If the process is
#       killed, whatever reached the disk is still a replayable record.
#
# Writers flush on a time budget rather than per frame: a flush per
# frame at 20 Hz is needless syscall traffic, and an unflushed buffer
# costs at most one second of data.

from __future__ import annotations

import csv
import json
import threading
import time
from datetime import datetime

from app_paths import csv_dir, recovery_dir, safe_name
from constants import CSV_ENCODING, FILE_ENCODING, LOG_RECORDING, UNKNOWN_OPERATOR

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"     # Truncated to milliseconds in frame_row
FILE_STAMP_FORMAT = "%Y%m%d_%H%M%S"
DEFAULT_RUN_NAME = "run"
LIST_LIMIT = 40

# Column order shared by the streamed CSV and the .xlsx export, so the
# two views of one run never disagree about what column 7 is.
COLUMNS: list[tuple[str, str]] = [
    ("Timestamp",     "ts"),
    ("Torque (N·m)",  "torque"),
    ("Thrust (N)",    "thrust"),
    ("S1 (kg)",       "s1"),
    ("S2 (kg)",       "s2"),
    ("S3 (kg)",       "s3"),
    ("S4 (kg)",       "s4"),
    ("Stable",        "stable"),
    ("Direction",     "dir"),
    ("RPM",           "rpm"),
    ("RPM raw",       "rpm_raw"),
    ("Throttle (µs)", "throttle_us"),
    ("Current (A)",   "current_a"),
    ("Voltage (V)",   "voltage_v"),
    ("Current 2 (A)", "current_a2"),
    ("Voltage 2 (V)", "voltage_v2"),
]

FLUSH_INTERVAL_S = 1.0


def frame_row(frame: dict) -> list:
    """One telemetry frame as a row in COLUMNS order."""
    row = []
    for _, key in COLUMNS:
        if key == "ts":
            row.append(
                datetime.fromtimestamp(frame.get("ts", 0)).strftime(TIMESTAMP_FORMAT)[:-3]
            )
        elif key == "stable":
            row.append("Yes" if frame.get("stable") else "No")
        else:
            row.append(frame.get(key))
    return row


class SessionRecorder:
    """Streams one recording to disk. Not reentrant — one run at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = False
        self.frame_count = 0
        self.test_name = ""
        self.operator = ""
        self.started_at: float | None = None
        self.csv_path: str | None = None
        self.meta_path: str | None = None
        self.recovery_path: str | None = None
        self.error: str = ""
        self._csv_fh = None
        self._csv_writer = None
        self._jsonl_fh = None
        self._last_flush = 0.0
        self._calibration: dict = {}

    def start(self, test_name: str, operator: str, calibration: dict | None = None) -> dict:
        """Open the output files for a new run."""
        with self._lock:
            self._close_files()
            self.active = True
            self.frame_count = 0
            self.error = ""
            self.test_name = (test_name or "").strip()
            self.operator = operator or UNKNOWN_OPERATOR
            self.started_at = time.time()
            self._calibration = dict(calibration or {})

            stamp = datetime.now().strftime(FILE_STAMP_FORMAT)
            base = f"{safe_name(self.test_name, DEFAULT_RUN_NAME)}_{stamp}"

            try:
                folder = csv_dir()
                self.csv_path = str(folder / f"{base}.csv")
                self.meta_path = str(folder / f"{base}.meta.json")
                self.recovery_path = str(recovery_dir() / f"{base}.jsonl")

                self._csv_fh = open(self.csv_path, "w", newline="", encoding=CSV_ENCODING)
                self._csv_writer = csv.writer(self._csv_fh)
                self._csv_writer.writerow([label for label, _ in COLUMNS])

                self._jsonl_fh = open(self.recovery_path, "w", encoding=FILE_ENCODING)
                self._write_meta(finished=False)
                self._last_flush = time.monotonic()
                print(f"{LOG_RECORDING} Started → {self.csv_path}")
            except OSError as e:
                # A disk problem must not stop the bench — the run keeps
                # going, and status() reports the error.
                self.error = f"Could not open log files: {e}"
                self._close_files()
                print(f"{LOG_RECORDING} {self.error}")

            return self.status()

    def write(self, frame: dict) -> None:
        """Append one telemetry frame. Called from the serial reader thread."""
        if not self.active:
            return
        with self._lock:
            if not self.active:
                return
            self.frame_count += 1
            try:
                if self._csv_writer is not None:
                    self._csv_writer.writerow(frame_row(frame))
                if self._jsonl_fh is not None:
                    self._jsonl_fh.write(json.dumps(frame) + "\n")

                now = time.monotonic()
                if now - self._last_flush >= FLUSH_INTERVAL_S:
                    self._last_flush = now
                    self._flush()
            except (OSError, ValueError) as e:
                self.error = f"Write failed: {e}"
                print(f"{LOG_RECORDING} {self.error}")
                self._close_files()

    def stop(self) -> dict:
        """Close the files and finalise the sidecar metadata."""
        with self._lock:
            was_active = self.active
            self.active = False
            if was_active:
                self._flush()
                self._write_meta(finished=True)
            self._close_files()
            if was_active:
                print(f"{LOG_RECORDING} Stopped — {self.frame_count} frames → {self.csv_path}")
            return self.status()

    def status(self) -> dict:
        return {
            "recording": self.active,
            "frame_count": self.frame_count,
            "test_name": self.test_name,
            "operator": self.operator,
            "csv_path": self.csv_path,
            "recovery_path": self.recovery_path,
            "started_at": self.started_at,
            "error": self.error,
        }

    def _flush(self) -> None:
        for fh in (self._csv_fh, self._jsonl_fh):
            if fh is not None:
                try:
                    fh.flush()
                except OSError:
                    pass

    def _write_meta(self, finished: bool) -> None:
        if not self.meta_path:
            return
        meta = {
            "test_name": self.test_name,
            "operator": self.operator,
            "started_at": datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds")
                          if self.started_at else None,
            "finished_at": datetime.now().isoformat(timespec="seconds") if finished else None,
            "frame_count": self.frame_count,
            "csv_file": self.csv_path,
            "recovery_file": self.recovery_path,
            # What the ESP32 reported when this run started, verbatim
            "calibration": self._calibration,
        }
        try:
            with open(self.meta_path, "w", encoding=FILE_ENCODING) as fh:
                json.dump(meta, fh, indent=2)
        except OSError as e:
            print(f"{LOG_RECORDING} Could not write metadata: {e}")

    def _close_files(self) -> None:
        for attr in ("_csv_fh", "_jsonl_fh"):
            fh = getattr(self, attr)
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
            setattr(self, attr, None)
        self._csv_writer = None


def list_recordings(limit: int = LIST_LIMIT) -> list[dict]:
    """Recent recordings on disk, newest first — for the dashboard list."""
    try:
        files = sorted(csv_dir().glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []

    out = []
    for path in files[:limit]:
        meta = {}
        meta_path = path.with_name(path.stem + ".meta.json")
        try:
            with open(meta_path, "r", encoding=FILE_ENCODING) as fh:
                meta = json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
        try:
            size = path.stat().st_size
            mtime = path.stat().st_mtime
        except OSError:
            continue
        out.append({
            "file": path.name,
            "path": str(path),
            "size_bytes": size,
            "modified": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
            "test_name": meta.get("test_name", ""),
            "operator": meta.get("operator", ""),
            "frame_count": meta.get("frame_count"),
        })
    return out
