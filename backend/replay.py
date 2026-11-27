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
import math
import os
import random
import threading
import time
from pathlib import Path

from constants import (
    CMD_CAL_DUMP, CMD_CAL_PREFIX, CMD_CAL_RESET, CMD_PING, CMD_TARE, CMD_THROTTLE,
    CSV_ENCODING,
    DIR_CCW, DIR_CW, EV_CAL, EV_CAL_DEFAULTS, EV_CAL_RESET, EV_CAL_SET,
    EV_PONG, EV_TARE_DONE, EV_TARE_STARTED, EV_THROTTLE_SET, FILE_ENCODING,
    THR_MAX, THR_MIN,
)
from recorder import COLUMNS, DERIVED_KEYS

ENABLE_ENV = "NWA_BENCH_ALLOW_REPLAY"
ENV_FALSE_VALUES = ("", "0", "false", "False")

EXT_JSONL = ".jsonl"
EXT_CSV = ".csv"

MIN_RATE_HZ = 1.0
MAX_RATE_HZ = 200.0
IDLE_SLEEP_S = 0.25                  # Bounded, so close() stays responsive
EXHAUSTED_SLEEP_S = 0.2

# ── Simulator model ───────────────────────────────────────────────
# Roughly the bench's real setup: a 28" prop on a 12S pack. Thrust
# follows the T-Motor reference the engineer compared against (57.7 N at
# 3280 rpm). The DC current is DERIVED from shaft power through a
# plausible motor+ESC efficiency curve, so the CSV's power and efficiency
# columns come out physically consistent (the old constants implied
# ~1800 % efficiency at full throttle).
SIM_MAX_RPM = 4000.0
SIM_RPM_TAU_S = 0.9                  # First-order lag toward the commanded speed
SIM_THRUST_COEFF = 5.36              # N per (krpm)² — thrust and torque both go as rpm²
SIM_TORQUE_COEFF = 0.186             # N·m per (krpm)²: ~2 N·m, ~690 W shaft at 3280 rpm
SIM_TORQUE_ARM_M = 0.18
SIM_IDLE_CURRENT_A = 0.6             # ESC + motor at standstill
SIM_EFFICIENCY = (0.62, 0.50, -0.38) # eta(m) = a + b*m + c*m², m = rpm / max: ~70 % low, ~78 % mid, ~74 % full
SIM_PACK_VOLTAGE_V = 50.4            # 12S
SIM_PACK_RESISTANCE_OHM = 0.035
# AC side (motor phases). Phase RMS current tracks the DC current; the
# per-phase factors give a small, visible imbalance so the three cards
# aren't identical. Line voltage RMS scales with the ESC's modulation.
SIM_AC_CURRENT_RATIO = 0.82
SIM_AC_PHASE_IMBALANCE = (1.00, 0.97, 1.03)
SIM_AC_VOLTAGE_RATIO = 0.61          # Line-to-line RMS ≈ 0.61 × V_dc at full modulation
SIM_SETTLED_RPM_ABS = 60.0           # Whichever of these is larger counts as settled
SIM_SETTLED_RPM_FRAC = 0.01
SIM_TARE_S = 3.0                     # Matches the firmware: the current zeros are averaged over 3 s
SIM_TARE_SAMPLES = 20
SIM_TARE_CELLS = ("s1", "s2", "s3", "s4", "thrust")
# Current sensors in TARE_DONE: firmware key -> samples it gets in the 3 s window
SIM_TARE_CURRENTS = {"current": 60, "current2": 16, "current3": 16, "current4": 16}


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
        # Map the console's own CSV headers back to telemetry keys. The
        # current recorder.COLUMNS labels come first; the pre-AC-sensor
        # labels stay so runs recorded before the rename still replay.
        # Power/efficiency columns are computed from the others, not telemetry.
        column_to_key = {label: key for label, key in COLUMNS
                         if key != "ts" and key not in DERIVED_KEYS}
        column_to_key.update({
            "Current (A)": "current_a", "Voltage (V)": "voltage_v",
            "Current 2 (A)": "current_a2", "Voltage 2 (V)": "voltage_v2",
        })
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

        motor_frac = self.rpm / SIM_MAX_RPM          # Follows the motor, not the command
        a, b, c = SIM_EFFICIENCY
        eta = a + b * motor_frac + c * motor_frac ** 2
        shaft_w = 2 * math.pi * self.rpm / 60 * SIM_TORQUE_COEFF * n2
        current = (SIM_IDLE_CURRENT_A + shaft_w / (eta * SIM_PACK_VOLTAGE_V)
                   + random.gauss(0, 0.15))
        voltage = (SIM_PACK_VOLTAGE_V - current * SIM_PACK_RESISTANCE_OHM
                   + random.gauss(0, 0.02))

        ac_currents = [
            max(0.0, current * SIM_AC_CURRENT_RATIO * k * motor_frac ** 0.5
                + random.gauss(0, 0.1))
            for k in SIM_AC_PHASE_IMBALANCE
        ]
        ac_voltage = max(0.0, voltage * SIM_AC_VOLTAGE_RATIO * motor_frac
                         + random.gauss(0, 0.05))

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
            "current_a2": round(ac_currents[0], 2),
            "voltage_v2": round(ac_voltage, 2),
            "current_a3": round(ac_currents[1], 2),
            "current_a4": round(ac_currents[2], 2),
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
        # Numbered like the firmware's frames, and re-numbered for a
        # replayed file too: a captured log's own seq values would read
        # as a reboot every time the file loops.
        self._seq = 0
        self._started = time.monotonic()
        self._tare_done_at: float | None = None

    def readline(self) -> bytes:
        # Answer commands first: the firmware emits its event line
        # immediately, and the dashboard's throttle "confirmed" readout
        # is timed against exactly that.
        with self._lock:
            if self._pending:
                return self._pending.pop(0)
            if self._tare_done_at is not None and time.monotonic() >= self._tare_done_at:
                self._tare_done_at = None
                return self._tare_done_line()

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

        frame["seq"] = self._seq
        self._seq = (self._seq + 1) % (2 ** 32)
        return (json.dumps(frame) + "\n").encode()

    def _tare_done_line(self) -> bytes:
        cells = {k: {"n": SIM_TARE_SAMPLES, "sd_kg": round(random.uniform(0.0002, 0.0006), 5)}
                 for k in SIM_TARE_CELLS}
        # A simulated sensor at rest is always settled.
        currents = {k: {"n": n, "zero_v": 0.0, "sd_a": round(random.uniform(0.02, 0.06), 3),
                        "drift_a": round(random.uniform(-0.05, 0.05), 3)}
                    for k, n in SIM_TARE_CURRENTS.items()}
        return (json.dumps({"event": EV_TARE_DONE, "ok": True, "ms": int(SIM_TARE_S * 1000),
                            "samples": SIM_TARE_SAMPLES, "cells": cells, "currents": currents,
                            "failed": [], "unsettled": []})
                + "\n").encode()

    def write(self, data: bytes) -> int:
        text = data.decode(FILE_ENCODING, errors="ignore").strip()
        if not text:
            return 0
        with self._lock:
            if text == CMD_TARE:
                # Two lines, like the firmware: STARTED now, DONE once
                # the (simulated) samples are in.
                self._pending.append(json.dumps(
                    {"event": EV_TARE_STARTED, "samples": SIM_TARE_SAMPLES}).encode() + b"\n")
                self._tare_done_at = time.monotonic() + SIM_TARE_S
            elif text.startswith(CMD_PING):
                try:
                    n = int(text[1:])
                except ValueError:
                    return len(data)
                self._pending.append(json.dumps({
                    "event": EV_PONG, "n": n,
                    "up_ms": int((time.monotonic() - self._started) * 1000),
                    "rx_ovf": 0, "rx_err": 0, "bad_cmd": 0, "late": 0,
                }).encode() + b"\n")
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
