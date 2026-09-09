# A stand-in for pyserial's Serial that produces telemetry without an
# ESP32 attached. Shaped as a drop-in for the real port object rather
# than a separate injection endpoint, so replayed data travels the EXACT
# same path as live data: serial_reader_thread parses it, health
# counters see it, history and recordings capture it, and commands
# written back are answered with the firmware's own event lines.
#
# Two sources: replay a captured .jsonl (from Recovery/) or a recorded
# .csv, or simulate a bench — throttle in, rpm/thrust/torque out, with
# lag and noise.
#
# GATED OFF BY DEFAULT. Fabricated telemetry that looks identical to
# real telemetry is exactly the wrong thing to have available by
# accident on a machine that produces test reports.

from __future__ import annotations

import csv
import json
import os
import random
import threading
import time
from pathlib import Path

from constants import (
    CMD_CAL_DUMP, CMD_CAL_PREFIX, CMD_CAL_RESET, CMD_TARE, CMD_THROTTLE,
    CSV_ENCODING,
    DIR_CCW, DIR_CW, EV_CAL, EV_CAL_DEFAULTS, EV_CAL_RESET, EV_CAL_SET,
    EV_TARE_DONE, EV_THROTTLE_SET, FILE_ENCODING, THR_MAX, THR_MIN,
)

ENABLE_ENV = "NWA_BENCH_ALLOW_REPLAY"
ENV_FALSE_VALUES = ("", "0", "false", "False")

EXT_JSONL = ".jsonl"
EXT_CSV = ".csv"

MIN_RATE_HZ = 1.0
MAX_RATE_HZ = 200.0
IDLE_SLEEP_S = 0.25                  # Bounded, so close() stays responsive
EXHAUSTED_SLEEP_S = 0.2

# ── Simulator model ───────────────────────────────────────────────
SIM_MAX_RPM = 16000.0
SIM_RPM_TAU_S = 0.9                  # First-order lag toward the commanded speed
SIM_THRUST_COEFF = 3.9               # Per (krpm)² — thrust and torque both go as rpm²
SIM_TORQUE_COEFF = 0.052
SIM_TORQUE_ARM_M = 0.18
SIM_IDLE_CURRENT_A = 0.6
SIM_MAX_CURRENT_A = 52.0
SIM_CURRENT_EXPONENT = 2.1
SIM_PACK_VOLTAGE_V = 24.6
SIM_PACK_RESISTANCE_OHM = 0.035
SIM_SETTLED_RPM_ABS = 60.0           # Whichever of these is larger counts as settled
SIM_SETTLED_RPM_FRAC = 0.01


def replay_enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "") not in ENV_FALSE_VALUES


def load_frames(path: str) -> list[dict]:
    """Read frames from a .jsonl recovery log or a recorded .csv."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"No such file: {path}")

    if p.suffix.lower() == EXT_JSONL:
        frames = []
        with p.open("r", encoding=FILE_ENCODING) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    frames.append(json.loads(line))
                except json.JSONDecodeError:
                    continue                       # Torn last line from a hard kill
        return frames

    if p.suffix.lower() == EXT_CSV:
        # Map the console's own CSV headers back to telemetry keys.
        column_to_key = {
            "Torque (N·m)": "torque", "Thrust (N)": "thrust",
            "S1 (kg)": "s1", "S2 (kg)": "s2", "S3 (kg)": "s3", "S4 (kg)": "s4",
            "Stable": "stable", "Direction": "dir",
            "RPM": "rpm", "RPM raw": "rpm_raw", "Throttle (µs)": "throttle_us",
            "Current (A)": "current_a", "Voltage (V)": "voltage_v",
            "Current 2 (A)": "current_a2", "Voltage 2 (V)": "voltage_v2",
        }
        frames = []
        with p.open("r", encoding=CSV_ENCODING, newline="") as fh:
            for row in csv.DictReader(fh):
                frame = {}
                for column, key in column_to_key.items():
                    raw = row.get(column)
                    if raw in (None, ""):
                        continue
                    if key == "stable":
                        frame[key] = raw.strip().lower() in ("yes", "true", "1")
                    elif key == "dir":
                        frame[key] = raw
                    else:
                        try:
                            frame[key] = float(raw)
                        except ValueError:
                            pass
                if frame:
                    frames.append(frame)
        return frames

    raise ValueError(f"Replay source must be a {EXT_JSONL} or {EXT_CSV} file")


class _Simulator:
    """Synthesises a bench that actually responds to the throttle.

    Not a random-number generator: rpm, thrust, torque and current are
    all derived from one commanded throttle with first-order lag, so
    the dashboard's ramps, plateaus and stability flag behave the way
    they do on the real rig. That is what makes it useful for checking
    the UI rather than just filling it with motion.
    """

    def __init__(self, direction: str = DIR_CW):
        self.throttle_us = THR_MIN
        self.rpm = 0.0
        self.direction = direction
        self._t0 = time.monotonic()

    def set_throttle(self, us: int) -> None:
        self.throttle_us = max(THR_MIN, min(THR_MAX, int(us)))

    def next_frame(self, dt: float) -> dict:
        frac = (self.throttle_us - THR_MIN) / (THR_MAX - THR_MIN)
        target_rpm = SIM_MAX_RPM * frac

        # A real motor doesn't step to a new speed, and an instant jump
        # would hide the ramp behaviour the phase markers exist to show.
        self.rpm += (target_rpm - self.rpm) * min(1.0, dt / SIM_RPM_TAU_S)

        rpm_noise = random.gauss(0, 25 + self.rpm * 0.001)
        rpm_raw = max(0.0, self.rpm + rpm_noise * 4)

        n2 = (self.rpm / 1000.0) ** 2
        thrust = SIM_THRUST_COEFF * n2 + random.gauss(0, 0.25)
        torque = SIM_TORQUE_COEFF * n2 + random.gauss(0, 0.004)
        if self.direction == DIR_CCW:
            torque = -torque

        # Torque is carried differentially by S3/S4; S1/S2 mostly react
        # the mount.
        arm = torque / SIM_TORQUE_ARM_M
        s3 = arm / 2 + random.gauss(0, 0.01)
        s4 = -arm / 2 + random.gauss(0, 0.01)
        s1 = thrust / 40 + random.gauss(0, 0.01)
        s2 = -thrust / 45 + random.gauss(0, 0.01)

        current = (SIM_IDLE_CURRENT_A + SIM_MAX_CURRENT_A * frac ** SIM_CURRENT_EXPONENT
                   + random.gauss(0, 0.15))
        voltage = (SIM_PACK_VOLTAGE_V - current * SIM_PACK_RESISTANCE_OHM
                   + random.gauss(0, 0.02))

        settled = abs(target_rpm - self.rpm) < max(
            SIM_SETTLED_RPM_ABS, target_rpm * SIM_SETTLED_RPM_FRAC)

        return {
            "torque": round(torque, 4),
            "thrust": round(thrust, 3),
            "s1": round(s1, 3), "s2": round(s2, 3),
            "s3": round(s3, 3), "s4": round(s4, 3),
            "stable": bool(settled),
            "dir": DIR_CW if torque >= 0 else DIR_CCW,
            "rpm": round(self.rpm, 1),
            "rpm_raw": round(rpm_raw, 1),
            "current_a": round(max(0.0, current), 2),
            "voltage_v": round(voltage, 2),
            "current_a2": 0.0,
            "voltage_v2": 0.0,
            "throttle_us": self.throttle_us,
        }


class ReplaySerial:
    """Quacks like serial.Serial, for the parts serial_reader_thread uses.

    Only readline / write / close / is_open are needed — that's the
    whole surface the reader thread and the command writers touch.
    """

    def __init__(self, frames: list[dict] | None = None, rate_hz: float = 20.0,
                 loop: bool = True, direction: str = DIR_CW,
                 calibration: dict | None = None):
        self.is_open = True
        self.rate_hz = max(MIN_RATE_HZ, min(MAX_RATE_HZ, rate_hz))
        self._period = 1.0 / self.rate_hz
        self._frames = frames
        self._index = 0
        self._loop = loop
        self._sim = None if frames else _Simulator(direction)
        self._pending: list[bytes] = []             # Event lines queued in reply to a command
        self._lock = threading.Lock()
        self._next_at = time.monotonic()
        # Stands in for the device's NVS table. Handed in by main.py
        # rather than defined here — the firmware and CAL_FIELD_SPEC are
        # already two copies of it and a third would be worse.
        self._cal_defaults = dict(calibration or {})
        self._cal_values = dict(self._cal_defaults)

    def readline(self) -> bytes:
        # Answer commands first: the firmware emits its event line
        # immediately, and the dashboard's throttle "confirmed" readout
        # is timed against exactly that.
        with self._lock:
            if self._pending:
                return self._pending.pop(0)

        now = time.monotonic()
        if now < self._next_at:
            time.sleep(min(self._next_at - now, IDLE_SLEEP_S))
            return b""                                   # Same empty-on-timeout contract as pyserial
        self._next_at += self._period

        if not self.is_open:
            return b""

        if self._sim is not None:
            frame = self._sim.next_frame(self._period)
        else:
            if self._index >= len(self._frames):
                if not self._loop:
                    # Go quiet rather than closing, so the port stays
                    # open and the health block's `stalled` flag surfaces
                    # it — same as a real board that stopped talking.
                    time.sleep(EXHAUSTED_SLEEP_S)
                    return b""
                self._index = 0
            frame = dict(self._frames[self._index])
            self._index += 1
            frame.pop("ts", None)                        # Re-stamped by the reader, as with a live frame

        return (json.dumps(frame) + "\n").encode()

    def write(self, data: bytes) -> int:
        text = data.decode(FILE_ENCODING, errors="ignore").strip()
        if not text:
            return 0
        with self._lock:
            if text == CMD_TARE:
                self._pending.append(
                    json.dumps({"event": EV_TARE_DONE}).encode() + b"\n"
                )
            elif text.startswith(CMD_THROTTLE):
                try:
                    us = int(text[1:])
                except ValueError:
                    return len(data)
                us = max(THR_MIN, min(THR_MAX, us))
                if self._sim is not None:
                    self._sim.set_throttle(us)
                self._pending.append(
                    json.dumps({"event": EV_THROTTLE_SET, "us": us}).encode() + b"\n"
                )
            elif text.startswith(CMD_CAL_PREFIX):
                self._handle_calibration(text)
        return len(data)

    def _handle_calibration(self, text: str) -> None:
        """Mimic the firmware's calibration command replies.

        Same three shapes main.py's relay expects: a dump ("C"), a
        single set ("C<key>=<value>"), and a reset ("C!"). Split into
        two lines for a dump exactly as the firmware does.
        """
        def queue(obj):
            self._pending.append(json.dumps(obj).encode() + b"\n")

        if text == CMD_CAL_DUMP:
            queue({"event": EV_CAL, "values": dict(self._cal_values)})
            queue({"event": EV_CAL_DEFAULTS, "defaults": dict(self._cal_defaults)})
            return

        if text == CMD_CAL_RESET:
            self._cal_values = dict(self._cal_defaults)
            queue({"event": EV_CAL_RESET, "ok": True})
            queue({"event": EV_CAL, "values": dict(self._cal_values)})
            queue({"event": EV_CAL_DEFAULTS, "defaults": dict(self._cal_defaults)})
            return

        key, _, raw = text[1:].partition("=")
        if not key or not raw:
            return
        try:
            value = float(raw)
        except ValueError:
            queue({"event": EV_CAL_SET, "key": key, "ok": False, "error": "parse"})
            return
        if key not in self._cal_defaults:
            queue({"event": EV_CAL_SET, "key": key, "ok": False, "error": "unknown_key"})
            return
        self._cal_values[key] = value
        queue({"event": EV_CAL_SET, "key": key, "value": value, "ok": True})

    def close(self) -> None:
        self.is_open = False
