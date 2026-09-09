# NWA Bench Console backend — FastAPI + pyserial + WebSocket.
#
# Reads JSON lines from the ESP32 over serial, broadcasts them to every
# dashboard client, relays tare/throttle/calibration commands back, and
# streams each recording to disk as it arrives.
#
# Every endpoint except /auth/* requires a signed-in operator — see
# auth.py for why (attribution, not secrecy).

import asyncio
import hmac                             # Constant-time compare for the shell's shutdown token
import json
import os
import sys
import threading                        # Serial reads are blocking; they run off the event loop
import time
from collections import deque
from datetime import datetime

# A Windows console defaults to cp1252, which cannot encode the
# characters this domain uses — "N·m", "µs", the em dashes already in
# these log lines. Any print() containing one raised UnicodeEncodeError
# from inside the request handler, so /recording/stop returned a 500 and
# the operator lost the export, with a traceback pointing at print()
# rather than anything to do with recording.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass                            # A pipe that doesn't support it — prints still work

import serial
import serial.tools.list_ports
from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import app_paths
import audit
import recorder
import replay
from analysis import router as analysis_router
from auth import router as auth_router, require_auth, websocket_user
from constants import (
    ACK_ERROR, ACK_SENT, ACTION_TARE, ACTION_THROTTLE,
    AUDIT_CALIBRATION, AUDIT_THROTTLE, CAL_VALUE_FORMAT,
    CMD_CAL_DUMP, CMD_CAL_RESET, CMD_CAL_SET, CMD_TARE, CMD_THROTTLE,
    APP_VERSION, DEFAULT_BAUD, DIR_CW, ERR_ALREADY_CONNECTED,
    ERR_PROFILE_RUNNING_CAL, ERR_SHUTDOWN_BAD_TOKEN, ERR_SHUTDOWN_DISABLED,
    SHUTDOWN_TOKEN_ENV, SHUTDOWN_TOKEN_HEADER,
    ERR_SERIAL_CONNECT_FIRST, ERR_SERIAL_DOWN, ERR_SERIAL_WRITE,
    ESP32_PORT_KEYWORDS, EVENT_KEY, EV_CAL, EV_CAL_DEFAULTS, EV_CAL_SET,
    LOG_PROFILE, LOG_RECORDING, LOG_SERIAL, LOG_SERVER, LOG_WS,
    MSG_ACK, MSG_HISTORY, MSG_PROFILE,
    PHASE_ABORTED, PHASE_DONE, PHASE_ERROR, PHASE_HOLDING, PHASE_IDLE,
    PHASE_RAMPING, PHASE_RAMPING_DOWN,
    SERIAL_ENCODING, SERIAL_READ_TIMEOUT_S, THR_MAX, THR_MIN,
    THROTTLE_SOURCE_CUT, THROTTLE_SOURCE_MANUAL,
    UNKNOWN_OPERATOR, WS_UNAUTHORIZED,
)

# ================================================================
# CONFIG
# ================================================================
HISTORY_SIZE = 600                      # 30 s × ~20 Hz; must match the frontend's own cap

# The 17 telemetry frame fields are documented in the protocol section
# of CLAUDE.md. Event lines the firmware sends in direct response to a
# command are a different shape and are routed separately below, so they
# never pollute history, latest_data, or a recording.

# ── Automated throttle profile (ramp → hold → next step → ramp down) ──
# Sequencing runs HERE, not in the browser: a browser timer is throttled
# to ~1 Hz once its tab is backgrounded, and closing the tab mid-run
# would strand the motor at whatever throttle it had reached. Server-side
# it survives a page refresh, a closed tab and a dead browser, and the
# `finally` in _run_profile() returns the throttle to THR_MIN on ANY
# exit path.
PROFILE_TICK_S        = 0.05            # 20 Hz while ramping — matches the firmware's telemetry pace
PROFILE_MAX_STEPS     = 40
PROFILE_MAX_HOLD_S    = 600.0           # 10 min on a single step
PROFILE_MIN_RAMP_RATE = 1.0             # µs/s — below this a full-range ramp takes >16 min
PROFILE_MAX_RAMP_RATE = 5000.0          # µs/s — at this, a full-range ramp is effectively instant
DEFAULT_RAMP_RATE     = 100.0

AUTO_RECONNECT_INTERVAL_S = 2.0
AUDIT_FLUSH_INTERVAL_S    = 0.5
STALL_THRESHOLD_S         = 2.0         # ~40 missed frames at 20 Hz — past a hiccup
BROADCAST_TIMEOUT_S       = 2.0
CAL_DUMP_TIMEOUT_S        = 2.5
CAL_DUMP_POLL_S           = 0.02
CAL_DEFAULTS_GRACE_S      = 0.12        # The defaults line follows the values line
INITIAL_CAL_REQUEST_DELAY_S = 1.5       # Opening the port can toggle DTR/RTS and reboot the chip
AUDIT_READ_MAX = 1000

# ================================================================
# APPLICATION STATE
# ================================================================
app = FastAPI(title="NWA Bench Console API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],                # Local dashboard dev
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
# Gated behind the same session check as everything else — an uploaded
# rig log is the operator's test data, not public.
app.include_router(analysis_router, dependencies=[Depends(require_auth)])

connected_clients: list[WebSocket] = []
latest_data: dict = {}
history: deque = deque(maxlen=HISTORY_SIZE)

serial_conn: serial.Serial | None = None
serial_status: dict = {"connected": False, "port": "", "error": ""}
reader_thread: threading.Thread | None = None
stop_reader_flag = threading.Event()    # Tells the reader thread to exit cleanly

# "connected" only means the OS handle is open. A board that has hung,
# browned out, or is wired to a dead TX line looks exactly as connected
# as a healthy one — readline() just keeps timing out. Frame age answers
# "is data still arriving", the rolling rate answers "at the pace the
# firmware promises", and the malformed counter tells a quiet link from
# a noisy one.
HEALTH_RATE_WINDOW_S = 5.0
serial_health: dict = {
    "frames_total":    0,
    "events_total":    0,                # TARE_DONE, CAL, ...
    "malformed_total": 0,                # Lines that started with { but didn't parse
    "last_frame_at":   None,             # Unix time
    "connected_at":    None,             # Unix time
    "frame_rate_hz":   0.0,
}
_frame_times: deque = deque(maxlen=400)  # Monotonic timestamps backing frame_rate_hz


def _reset_serial_health() -> None:
    serial_health.update(frames_total=0, events_total=0, malformed_total=0,
                         last_frame_at=None, connected_at=time.time(),
                         frame_rate_hz=0.0)
    _frame_times.clear()


def _note_frame() -> None:
    """Record one telemetry frame for the health counters."""
    now = time.monotonic()
    _frame_times.append(now)
    while _frame_times and now - _frame_times[0] > HEALTH_RATE_WINDOW_S:
        _frame_times.popleft()
    span = now - _frame_times[0] if len(_frame_times) > 1 else 0.0
    serial_health["frames_total"] += 1
    serial_health["last_frame_at"] = time.time()
    serial_health["frame_rate_hz"] = round((len(_frame_times) - 1) / span, 1) if span > 0 else 0.0


def health_snapshot() -> dict:
    """Serial health for /status, with the derived staleness verdict."""
    last = serial_health["last_frame_at"]
    age = (time.time() - last) if last else None
    connected = bool(serial_conn and serial_conn.is_open)
    stalled = bool(connected and (age is None or age > STALL_THRESHOLD_S))
    return {
        **serial_health,
        "last_frame_age_s": round(age, 2) if age is not None else None,
        "stalled": stalled,
    }

# True only when the operator explicitly clicked Disconnect. The
# auto-reconnect loop checks it so it doesn't reopen a port they
# deliberately closed; any *unexpected* drop (unplug, USB hiccup, ESP32
# reset) leaves it False, so auto-reconnect kicks back in on its own.
user_requested_disconnect: bool = False

recording: bool = False

# Frames stream straight to disk as they arrive; nothing is held in
# memory. The `recorded_frames` list that used to sit here existed only
# so Stop could build an .xlsx download — at 20 Hz an hour-long run held
# ~72,000 dicts to be serialised once.
session_recorder = recorder.SessionRecorder()

# ================================================================
# RUNTIME CALIBRATION
#
#   A pass-through to the firmware's NVS-backed runtime table: ask the
#   device what it's using ("C"), write individual values
#   ("C<key>=<value>"), cache whatever it reports back.
#
#   THE DEVICE IS THE SOURCE OF TRUTH, not this cache — nothing here
#   ever invents or persists a value. If the ESP32 is swapped or
#   reflashed, the next dump replaces the cache wholesale.
#
#   CAL_FIELD_SPEC is a hand-maintained mirror of the firmware's
#   CAL_FIELDS and must stay in sync with esp32/main.c. The firmware
#   validates independently and is the authority, so a mismatch surfaces
#   as a visible out_of_range ack rather than silently; validating here
#   too keeps a fat-fingered value off the wire in the first place.
# ================================================================
CAL_FIELD_SPEC: list[dict] = [
    # key,             group,          label,                        unit,        min,    max
    {"key": "thrust_scale",   "group": "Load cells",  "label": "Thrust cell scale",     "unit": "counts/kg", "min": 1.0,    "max": 1.0e7},
    {"key": "cal_kg_s1",      "group": "Load cells",  "label": "S1 (top) scale",        "unit": "counts/kg", "min": 1.0,    "max": 1.0e7},
    {"key": "cal_kg_s2",      "group": "Load cells",  "label": "S2 (bottom) scale",     "unit": "counts/kg", "min": 1.0,    "max": 1.0e7},
    {"key": "cal_kg_s3",      "group": "Load cells",  "label": "S3 (right) scale",      "unit": "counts/kg", "min": 1.0,    "max": 1.0e7},
    {"key": "cal_kg_s4",      "group": "Load cells",  "label": "S4 (left) scale",       "unit": "counts/kg", "min": 1.0,    "max": 1.0e7},
    {"key": "tq_k1_cw",       "group": "Torque CW",   "label": "K1 (S1 weight)",        "unit": "",          "min": -100.0, "max": 100.0},
    {"key": "tq_k2_cw",       "group": "Torque CW",   "label": "K2 (S2 weight)",        "unit": "",          "min": -100.0, "max": 100.0},
    {"key": "tq_k3_cw",       "group": "Torque CW",   "label": "K3 (S3 weight)",        "unit": "",          "min": -100.0, "max": 100.0},
    {"key": "tq_k4_cw",       "group": "Torque CW",   "label": "K4 (S4 weight)",        "unit": "",          "min": -100.0, "max": 100.0},
    {"key": "tq_k1_ccw",      "group": "Torque CCW",  "label": "K1 (S1 weight)",        "unit": "",          "min": -100.0, "max": 100.0},
    {"key": "tq_k2_ccw",      "group": "Torque CCW",  "label": "K2 (S2 weight)",        "unit": "",          "min": -100.0, "max": 100.0},
    {"key": "tq_k3_ccw",      "group": "Torque CCW",  "label": "K3 (S3 weight)",        "unit": "",          "min": -100.0, "max": 100.0},
    {"key": "tq_k4_ccw",      "group": "Torque CCW",  "label": "K4 (S4 weight)",        "unit": "",          "min": -100.0, "max": 100.0},
    {"key": "current_scale",  "group": "Power sensor 1", "label": "Current scale",      "unit": "A/V",       "min": 0.001,  "max": 1.0e5},
    {"key": "voltage_scale",  "group": "Power sensor 1", "label": "Voltage scale",      "unit": "V/V",       "min": 0.001,  "max": 1.0e5},
    {"key": "current2_scale", "group": "Power sensor 2", "label": "Current scale",      "unit": "A/V",       "min": 0.001,  "max": 1.0e5},
    {"key": "voltage2_scale", "group": "Power sensor 2", "label": "Voltage scale",      "unit": "V/V",       "min": 0.001,  "max": 1.0e5},
]
CAL_KEYS = {f["key"]: f for f in CAL_FIELD_SPEC}

# Cache of the last calibration dump reported by the device.
calibration: dict = {
    "values":   {},        # key -> float currently in use on the ESP32
    "defaults": {},        # key -> float compiled into the firmware
    "updated":  None,      # Unix time the values were last reported
}
# Bumped every time a "values" dump lands. /calibration waits on this
# to change rather than on a fixed sleep, so the request returns as
# soon as the device answers instead of always paying a worst-case wait.
calibration_seq: int = 0

# Gap between consecutive "C<key>=<value>" lines on a multi-field save.
# The firmware's UART RX buffer is 256 bytes, drained on a 20 ms tick;
# seventeen ~25-byte lines back to back is ~425 bytes, enough to overrun
# it if the task is briefly starved — and a truncated line is a silently
# dropped calibration. Spacing them also gives each NVS commit time.
CAL_WRITE_GAP_S = 0.04

# Captured once at startup.
#
# BUG FIX: start_serial() used to call asyncio.get_event_loop() for the
# loop it hands the reader thread. That works from the async startup
# handler, but /connect is a plain `def` endpoint that FastAPI runs in a
# worker thread, where get_event_loop() either raises (3.12+) or
# silently creates a loop nobody runs — and the reader thread then
# schedules broadcast() onto a dead loop, so the dashboard shows
# "connected" while receiving nothing.
main_loop: asyncio.AbstractEventLoop | None = None

# Last throttle actually written to the ESC by any path (manual slider,
# profile ramp, safety cut). A profile's first ramp starts here.
#
# BUG FIX [2026-09]: _run_profile used to seed its ramp with a hardcoded
# THR_MIN. Starting a profile while the motor was already at, say,
# 1500µs made the very first write ~1000µs — an abrupt deceleration
# nothing asked for, and the ramp took the time budgeted for
# 1000→target while the motor travelled a different distance.
last_throttle_us: int = THR_MIN

profile_task: asyncio.Task | None = None
profile_state: dict = {
    "running": False,
    "phase": PHASE_IDLE,
    "step_index": -1,         # 0-based index of the step being executed
    "step_count": 0,
    "target_us": THR_MIN,
    "commanded_us": THR_MIN,  # What was most recently written to the ESC
    "hold_remaining_s": 0.0,
    "ramp_rate_us_per_s": 0.0,
    "steps": [],
    "message": "",
}

# ================================================================
# SERIAL PORT LISTING + AUTO-DETECTION
# ================================================================
def list_available_ports() -> list[dict]:
    """Every serial port currently visible to the OS, with descriptions."""
    return [
        {"device": p.device, "description": p.description or "Unknown device"}
        for p in serial.tools.list_ports.comports()
    ]

def find_esp32_port() -> str | None:
    """The first visible port that looks like an ESP32, or None."""
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "").lower()
        if any(kw in desc for kw in ESP32_PORT_KEYWORDS):
            return p.device
    return None

# ================================================================
# SERIAL READER THREAD
# Daemon thread; pushes frames into latest_data + history, and exits
# when stop_reader_flag is set.
# ================================================================
def _cache_calibration_event(frame: dict) -> None:
    """Fold a firmware calibration event line into the module-level cache.

    Runs on the serial reader thread. Only float values the backend
    knows about are kept — an unrecognised key from a newer firmware is
    ignored rather than rendered as a nameless box in the panel.
    """
    global calibration_seq

    def _clean(obj) -> dict:
        if not isinstance(obj, dict):
            return {}
        out = {}
        for k, v in obj.items():
            if k in CAL_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = float(v)
        return out

    event = frame.get(EVENT_KEY)
    if event == EV_CAL:
        values = _clean(frame.get("values"))
        if values:
            calibration["values"] = values
            calibration["updated"] = time.time()
            calibration_seq += 1                              # Wakes any /calibration request waiting on a dump
    elif event == EV_CAL_DEFAULTS:
        defaults = _clean(frame.get("defaults"))
        if defaults:
            calibration["defaults"] = defaults
    elif event == EV_CAL_SET and frame.get("ok") and frame.get("key") in CAL_KEYS:
        # Keeps the cache honest between the write and the follow-up
        # dump, so a UI reading /calibration in that window doesn't show
        # the pre-write value and look like the save was lost.
        try:
            calibration["values"][frame["key"]] = float(frame["value"])
        except (KeyError, TypeError, ValueError):
            pass

def serial_reader_thread(loop: asyncio.AbstractEventLoop, port: str, baud: int,
                         conn=None):
    """Blocking serial read loop; runs in a background thread until stopped or disconnected.

    `conn` lets a replay/simulated port (replay.ReplaySerial) stand in
    for the real hardware. Everything downstream — parsing, health
    counters, history, recording, broadcast — is identical either way,
    which is the point: replay exercises the real path, not a parallel
    one that could drift out of step with it.
    """
    global serial_conn, serial_status, latest_data

    try:
        serial_conn = (conn if conn is not None
                       else serial.Serial(port, baud, timeout=SERIAL_READ_TIMEOUT_S))
        serial_status.update(connected=True, port=port, error="")
        _reset_serial_health()                               # Counters are per-connection, not per-process
        audit.log_serial("connected", port=port, baud=baud)
        print(f"{LOG_SERIAL} Connected on {port} @ {baud} baud")

        # Ask for the runtime calibration table shortly after the port
        # opens, so the settings panel and the "what was this run
        # calibrated with" record are populated without anyone opening
        # the panel. Delayed rather than immediate: opening the port can
        # toggle DTR/RTS, which on an auto-reset adapter reboots the
        # chip, and a command sent into that reboot is simply lost.
        cal_request_at = time.monotonic() + INITIAL_CAL_REQUEST_DELAY_S
        cal_requested = False

        while not stop_reader_flag.is_set():
            if not cal_requested and time.monotonic() >= cal_request_at:
                cal_requested = True                          # One attempt; /calibration re-asks on demand
                try:
                    serial_conn.write(f"{CMD_CAL_DUMP}\n".encode())
                except (serial.SerialException, OSError) as e:
                    print(f"{LOG_SERIAL} Initial calibration request failed: {e}")

            raw = serial_conn.readline()                     # Blocks until newline or the read timeout
            if not raw:
                continue

            line = raw.decode(SERIAL_ENCODING, errors="ignore").strip()
            if not line.startswith("{"):                     # Boot messages, logs
                continue

            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                # Counted, not silently dropped: a rising malformed
                # count is the signature of a baud mismatch or a noisy
                # cable, which otherwise presents only as "the numbers
                # look jumpy".
                serial_health["malformed_total"] += 1
                continue

            # The firmware emits two shapes of JSON on the same wire:
            # telemetry frames, and one-off event acks sent only in
            # direct response to a command. Both used to be treated
            # identically, so an event line overwrote latest_data, was
            # appended to history, landed in a recording as a blank row,
            # and injected a false zero into the live chart. Forward
            # events immediately — the frontend listens for them — but
            # keep them out of the telemetry stores below.
            if EVENT_KEY in frame:
                serial_health["events_total"] += 1
                # Calibration dumps are events too. The device sends
                # values and defaults as two separate lines, so this
                # merges rather than replaces.
                _cache_calibration_event(frame)
                asyncio.run_coroutine_threadsafe(broadcast(frame), loop)
                continue

            frame["ts"] = time.time()
            latest_data = frame
            history.append(frame)
            _note_frame()

            if recording:
                session_recorder.write(frame)                # Straight to disk, so a crash can't lose the run

            asyncio.run_coroutine_threadsafe(broadcast(frame), loop)

    except serial.SerialException as e:
        serial_status.update(connected=False, error=str(e))
        audit.log_serial("error", port=port, error=str(e))
        print(f"{LOG_SERIAL} Error: {e}")
    finally:
        if serial_conn:
            try:
                serial_conn.close()                          # Always release the OS handle
            except Exception:
                pass
        serial_conn = None                                    # So /connect can reopen cleanly
        serial_status["connected"] = False
        # Distinguishes "operator pressed Disconnect" from "the link
        # dropped underneath us" — the second is a hardware fault worth
        # chasing, the first isn't.
        audit.log_serial("closed", port=port,
                         requested=bool(stop_reader_flag.is_set()),
                         frames=serial_health["frames_total"])
        print(f"{LOG_SERIAL} Reader thread stopped")

def start_serial(port: str, baud: int = DEFAULT_BAUD, conn=None) -> bool:
    """Start the reader thread on a port. False if already connected."""
    global reader_thread
    if serial_conn is not None:
        return False
    if main_loop is None:                                    # Startup hasn't run — nothing to broadcast onto
        print(f"{LOG_SERIAL} start_serial called before startup captured the event loop")
        return False
    stop_reader_flag.clear()
    reader_thread = threading.Thread(
        target=serial_reader_thread,
        args=(main_loop, port, baud, conn),                  # Always the real, running loop
        daemon=True
    )
    reader_thread.start()
    return True

def stop_serial():
    """Signal the reader thread to stop and close the serial port."""
    global serial_conn
    stop_reader_flag.set()
    if serial_conn:
        try:
            serial_conn.close()                              # Force-close to unblock a pending readline()
        except Exception:
            pass

# ================================================================
# WEBSOCKET BROADCASTER
# ================================================================
async def broadcast(data: dict):
    """Send a JSON frame to every connected WebSocket client."""
    if not connected_clients:
        return
    payload = json.dumps(data)
    dead: list[WebSocket] = []
    for ws in connected_clients:
        try:
            # The timeout matters more than it looks: this coroutine is
            # scheduled from the serial reader thread, and a client with
            # a stalled TCP connection (asleep laptop, dropped WiFi, a
            # tab that stopped reading) can make a plain send_text() hang
            # indefinitely. At several frames a second, one stuck client
            # would back up every future frame behind it — a creeping
            # freeze that looks exactly like the firmware hanging.
            await asyncio.wait_for(ws.send_text(payload), timeout=BROADCAST_TIMEOUT_S)
        except (Exception, asyncio.TimeoutError):
            dead.append(ws)
    for ws in dead:
        connected_clients.remove(ws)

# ================================================================
# THROTTLE WRITE HELPER
#   The single place an "S<us>" line reaches the ESC, shared by the
#   manual WebSocket path and the profile sequencer.
# ================================================================
def write_throttle_us(us: int) -> bool:
    """Clamp and write a throttle command. False if serial is down."""
    global last_throttle_us
    us = max(THR_MIN, min(THR_MAX, int(us)))                  # Firmware clamps again independently
    if not (serial_conn and serial_conn.is_open):
        return False
    try:
        serial_conn.write(f"{CMD_THROTTLE}{us}\n".encode())
        last_throttle_us = us                                  # Where a profile's first ramp starts
        return True
    except (serial.SerialException, OSError) as e:
        # A write can fail on a yanked cable even though is_open still
        # reads True — a hard failure, so a running profile aborts
        # instead of "commanding" a motor that stopped listening.
        print(f"{LOG_SERIAL} Throttle write failed: {e}")
        return False


# ================================================================
# AUTOMATED THROTTLE PROFILE SEQUENCER
#
#   A profile is a list of {"us": <target>, "hold_s": <dwell>} steps:
#   ramp from wherever we are to step 1's target, hold, ramp to step 2,
#   hold, … then ramp back down to THR_MIN.
#
#   Ramping is computed from WALL-CLOCK ELAPSED TIME, not by counting
#   ticks. If the event loop stalls for a moment, an elapsed-time ramp
#   still reaches the target when it should; a tick-counting ramp would
#   silently stretch and hold the motor at an intermediate throttle for
#   longer than asked.
#
#   SAFETY: the `finally` block drops the throttle to THR_MIN on every
#   exit path. The one exception is a manual takeover, where the
#   operator's own value is applied instead and must not be stomped on.
# ================================================================
async def _broadcast_profile_state():
    """Push the current profile state to all dashboard clients."""
    await broadcast({"type": MSG_PROFILE, "profile": dict(profile_state)})


def _set_profile(**kwargs):
    """Update profile_state in place — shared with /profile/status."""
    profile_state.update(kwargs)


async def _ramp_to(target_us: int, ramp_rate: float, from_us: int) -> int:
    """Linearly ramp the throttle from from_us to target_us at ramp_rate µs/s.

    Returns the throttle value actually reached (== target_us on success).
    Raises RuntimeError if the serial link drops mid-ramp.
    """
    delta = target_us - from_us
    if delta == 0:
        return from_us

    duration = abs(delta) / ramp_rate
    start = time.monotonic()

    while True:
        elapsed = time.monotonic() - start
        frac = 1.0 if elapsed >= duration else (elapsed / duration)
        us = int(round(from_us + delta * frac))

        if not write_throttle_us(us):
            raise RuntimeError("serial link lost during ramp")
        _set_profile(commanded_us=us)

        if frac >= 1.0:
            return target_us
        await asyncio.sleep(PROFILE_TICK_S)


async def _hold(seconds: float, at_us: int):
    """Dwell at a fixed throttle, refreshing the countdown for the UI."""
    start = time.monotonic()
    while True:
        remaining = seconds - (time.monotonic() - start)
        if remaining <= 0:
            _set_profile(hold_remaining_s=0.0)
            return
        _set_profile(hold_remaining_s=round(remaining, 1))

        # The ESC holds its last commanded pulse width on its own, so
        # re-asserting is belt-and-braces — but it means a serial drop is
        # detected DURING a long dwell, not only at the next ramp.
        if not write_throttle_us(at_us):
            raise RuntimeError("serial link lost during hold")

        await asyncio.sleep(min(1.0, remaining))


async def _run_profile(steps: list[dict], ramp_rate: float,
                       operator: str = UNKNOWN_OPERATOR):
    """Execute a full throttle profile. Cancelled by abort_profile()."""
    # From where the ESC actually is, not an assumed idle — see the
    # last_throttle_us note above.
    current_us = max(THR_MIN, min(THR_MAX, last_throttle_us))
    manual_takeover = False
    try:
        _set_profile(running=True, phase=PHASE_RAMPING, step_index=-1,
                     step_count=len(steps), ramp_rate_us_per_s=ramp_rate,
                     steps=steps, message="")
        audit.log_profile(operator, "start", steps=steps, ramp_rate_us_per_s=ramp_rate)
        await _broadcast_profile_state()

        for i, step in enumerate(steps):
            target = int(step["us"])
            hold_s = float(step["hold_s"])

            _set_profile(phase=PHASE_RAMPING, step_index=i, target_us=target,
                         hold_remaining_s=0.0)
            await _broadcast_profile_state()
            current_us = await _ramp_to(target, ramp_rate, current_us)

            _set_profile(phase=PHASE_HOLDING, commanded_us=current_us)
            await _broadcast_profile_state()
            await _hold(hold_s, current_us)

        _set_profile(phase=PHASE_RAMPING_DOWN, step_index=len(steps),
                     target_us=THR_MIN)
        await _broadcast_profile_state()
        current_us = await _ramp_to(THR_MIN, ramp_rate, current_us)

        _set_profile(phase=PHASE_DONE, message="Profile completed")
        audit.log_profile(operator, "done", steps=len(steps))

    except asyncio.CancelledError:
        # Operator pressed STOP, or a manual throttle command took over.
        manual_takeover = profile_state.get("_takeover", False)
        _set_profile(phase=PHASE_ABORTED,
                     message="Manual throttle took over" if manual_takeover else "Stopped by operator")
        audit.log_profile(operator, "aborted", takeover=manual_takeover,
                          at_step=profile_state.get("step_index"))
        raise
    except Exception as e:
        _set_profile(phase=PHASE_ERROR, message=str(e))
        audit.log_profile(operator, "error", error=str(e),
                          at_step=profile_state.get("step_index"))
        print(f"{LOG_PROFILE} Aborted: {e}")
    finally:
        profile_state.pop("_takeover", None)
        # Unconditional safety cut, except when the operator is taking
        # manual control right now. A plain synchronous write on
        # purpose: it must complete even when we got here via
        # cancellation, and awaiting anything here could re-raise
        # CancelledError before the throttle actually comes down.
        if not manual_takeover:
            write_throttle_us(THR_MIN)
            _set_profile(commanded_us=THR_MIN)
        _set_profile(running=False, hold_remaining_s=0.0)
        # Fire-and-forget: awaiting here would be unreliable during
        # cancellation (CancelledError skips past an `except Exception`
        # guard), and clients re-sync from /profile/status anyway.
        #
        # Guarded because during interpreter shutdown the loop can
        # already be closed and create_task() then raises RuntimeError,
        # which would propagate out of this finally and replace whatever
        # exception was actually being handled.
        if main_loop is not None and not main_loop.is_closed():
            try:
                main_loop.create_task(_broadcast_profile_state())
            except RuntimeError:
                pass


async def abort_profile(cut: bool = True, takeover: bool = False):
    """Cancel a running profile and wait for its cleanup to finish.

    cut=True      → the sequencer's finally block drops throttle to THR_MIN.
    takeover=True → suppress that cut because the caller is about to
                    apply its own throttle value (manual override).
    """
    global profile_task
    t = profile_task
    profile_task = None
    if t is None or t.done():
        return
    if takeover:
        profile_state["_takeover"] = True
    t.cancel()
    try:
        await t                                                 # Wait for the finally block to actually run
    except asyncio.CancelledError:
        pass
    if cut and not takeover:
        write_throttle_us(THR_MIN)


# ================================================================
# AUTO-RECONNECT
#
#   Without this, an ESP32 that dropped off (unplug, USB hiccup, reset,
#   Windows re-enumerating the port) left the backend sitting with
#   serial_conn = None until someone restarted the process or reopened
#   the port by hand. Re-scanning fresh each time rather than
#   remembering the old port name also handles the device coming back
#   under a different COM number after a replug.
# ================================================================
async def auto_reconnect_loop():
    while True:
        await asyncio.sleep(AUTO_RECONNECT_INTERVAL_S)
        if serial_conn is None and not user_requested_disconnect:
            port = find_esp32_port()
            if port:
                print(f"{LOG_SERIAL} Auto-reconnect: found {port}, connecting...")
                start_serial(port)

# ================================================================
# STARTUP / SHUTDOWN
# ================================================================
async def audit_flush_loop():
    """Write out manual throttle values once the operator stops moving them.

    Dragging the slider produces ~12 commands a second, and only the
    value it comes to rest on is worth a log line — see audit.py.
    """
    while True:
        await asyncio.sleep(AUDIT_FLUSH_INTERVAL_S)
        audit.flush_pending_throttle()


@app.on_event("startup")
async def startup():
    """Auto-connect to an ESP32 if one is present; the dashboard can also connect by hand."""
    global main_loop
    main_loop = asyncio.get_running_loop()                    # The real loop, captured once
    port = find_esp32_port()
    if port:
        start_serial(port)
    asyncio.create_task(auto_reconnect_loop())
    asyncio.create_task(audit_flush_loop())
    print(f"{LOG_SERVER} NWA Bench Console backend started")
    print(f"{LOG_SERVER} Operator data folder: {app_paths.app_dir()}")


@app.on_event("shutdown")
async def shutdown():
    """A running profile must never outlive the server with the motor spinning."""
    await abort_profile(cut=True)
    # Flush the last buffered rows — an interrupted run should still
    # open in Excel.
    if recording:
        session_recorder.stop()
    audit.flush_pending_throttle(force=True)
    stop_serial()

# ================================================================
# WEBSOCKET ENDPOINT — /ws
# ================================================================
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Handle a new dashboard WebSocket connection."""
    # A browser can't set an Authorization header on a WebSocket, so the
    # token arrives as ?token=... instead. Rejected before accept: an
    # unauthenticated socket must never receive a telemetry frame.
    operator = websocket_user(ws)
    if operator is None:
        await ws.close(code=WS_UNAUTHORIZED)
        return

    await ws.accept()
    connected_clients.append(ws)
    print(f"{LOG_WS} Client connected ({operator}). Total: {len(connected_clients)}")

    if history:                                                # So the chart populates immediately
        await ws.send_text(json.dumps({
            "type": MSG_HISTORY,
            "data": list(history)
        }))

    try:
        while True:
            msg = await ws.receive_text()
            try:
                cmd = json.loads(msg)
            except json.JSONDecodeError:
                continue

            # "sent" means the transport write succeeded. The firmware
            # confirms receipt separately with its own TARE_DONE line,
            # which is what the frontend shows as "done".
            if cmd.get("action") == ACTION_TARE:
                if serial_conn and serial_conn.is_open:
                    serial_conn.write(f"{CMD_TARE}\n".encode())
                    audit.log_profile(operator, ACTION_TARE)   # Re-zeroes every cell — worth attributing
                    print(f"{LOG_SERIAL} Tare command sent by {operator}")
                    await ws.send_text(json.dumps({
                        "type": MSG_ACK, "action": ACTION_TARE, "status": ACK_SENT
                    }))
                else:
                    await ws.send_text(json.dumps({
                        "type": MSG_ACK, "action": ACTION_TARE, "status": ACK_ERROR,
                        "message": ERR_SERIAL_DOWN
                    }))

            elif cmd.get("action") == ACTION_THROTTLE:
                us = cmd.get("us")                             # Microsecond pulse width
                if not isinstance(us, int):
                    await ws.send_text(json.dumps({
                        "type": MSG_ACK, "action": ACTION_THROTTLE, "status": ACK_ERROR,
                        "message": "Invalid throttle value"
                    }))
                    continue

                # A manual throttle always wins over a running profile,
                # CUT THROTTLE included — reaching for the slider
                # mid-profile means the operator wants control back now,
                # not after the current step. takeover=True stops the
                # sequencer's safety cut from immediately overwriting
                # the value they just asked for.
                if profile_state["running"]:
                    await abort_profile(takeover=True)

                if write_throttle_us(us):
                    # A cut is logged the instant it happens; ordinary
                    # slider movement is debounced, so a drag leaves one
                    # line, not fifty.
                    audit.note_throttle(
                        operator, max(THR_MIN, min(THR_MAX, us)),
                        source=THROTTLE_SOURCE_CUT if us <= THR_MIN
                        else THROTTLE_SOURCE_MANUAL)
                    await ws.send_text(json.dumps({
                        "type": MSG_ACK, "action": ACTION_THROTTLE,
                        "status": ACK_SENT, "us": us
                    }))
                else:
                    await ws.send_text(json.dumps({
                        "type": MSG_ACK, "action": ACTION_THROTTLE, "status": ACK_ERROR,
                        "message": ERR_SERIAL_DOWN
                    }))

    except WebSocketDisconnect:
        pass
    finally:
        # broadcast() also prunes dead sockets, so this client may
        # already be gone — a bare .remove() would raise ValueError and
        # turn a routine disconnect into a logged exception.
        if ws in connected_clients:
            connected_clients.remove(ws)
        print(f"{LOG_WS} Client disconnected. Total: {len(connected_clients)}")

# ================================================================
# SERIAL CONNECTION MANAGEMENT ENDPOINTS
# ================================================================
@app.get("/ports")
def get_ports(operator: str = Depends(require_auth)):
    """Serial ports visible to the OS, for the dashboard's port picker."""
    return {"ports": list_available_ports()}

@app.post("/connect")
def connect_port(payload: dict, operator: str = Depends(require_auth)):
    """Open a serial connection. Body: {"port": "COM6"}."""
    global user_requested_disconnect
    port = payload.get("port")
    if not port:
        return {"ok": False, "error": "No port specified"}
    if serial_conn is not None:
        return {"ok": False, "error": ERR_ALREADY_CONNECTED}
    user_requested_disconnect = False                          # Re-arms auto-reconnect for later drops
    started = start_serial(port)
    return {"ok": started, "port": port}

@app.post("/disconnect")
async def disconnect_port(operator: str = Depends(require_auth)):
    """Close the current serial connection, if any."""
    global user_requested_disconnect
    if serial_conn is None:
        return {"ok": False, "error": "Not connected"}
    # Stop any running profile FIRST, while the link is still open, so
    # the sequencer's safety cut actually reaches the ESC — tearing the
    # port down underneath a live profile would leave the motor at
    # whatever throttle it had reached with no way to command it down.
    await abort_profile(cut=True)
    user_requested_disconnect = True                           # Auto-reconnect stands down
    stop_serial()
    return {"ok": True}


# ================================================================
# REPLAY / SIMULATED SOURCE  —  off unless NWA_BENCH_ALLOW_REPLAY=1
#
#   Exercises the whole stack without an ESP32 — UI work, recording,
#   profile sequencing, export — with frames fed through
#   serial_reader_thread exactly as real ones are.
#
#   Off by default, deliberately: synthetic telemetry is
#   indistinguishable from real telemetry once it's in a CSV, and this
#   machine produces test reports.
# ================================================================
@app.get("/replay/status")
def replay_status(operator: str = Depends(require_auth)):
    return {
        "enabled": replay.replay_enabled(),
        "active": isinstance(serial_conn, replay.ReplaySerial),
        "env_var": replay.ENABLE_ENV,
    }


@app.post("/replay/start")
def replay_start(payload: dict | None = None, operator: str = Depends(require_auth)):
    """Attach a simulated or replayed source in place of the ESP32.

    Body: {"mode": "simulate"|"file", "file": "<path to .jsonl/.csv>",
           "rate_hz": 20, "loop": true, "direction": "CW"|"CCW"}
    """
    global user_requested_disconnect

    if not replay.replay_enabled():
        return {"ok": False,
                "error": f"Replay is disabled. Set {replay.ENABLE_ENV}=1 and restart the backend."}
    if serial_conn is not None:
        return {"ok": False, "error": ERR_ALREADY_CONNECTED}

    body = payload or {}
    mode = str(body.get("mode", "simulate"))
    try:
        rate_hz = float(body.get("rate_hz", 20.0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "rate_hz must be a number"}

    frames = None
    if mode == "file":
        path = body.get("file")
        if not path:
            return {"ok": False, "error": "mode 'file' needs a 'file' path"}
        try:
            frames = replay.load_frames(str(path))
        except (FileNotFoundError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        if not frames:
            return {"ok": False, "error": "That file contained no usable frames"}

    # Plausible mid-range values so the settings panel has something to
    # render in replay mode. Derived from CAL_FIELD_SPEC rather than
    # written out again — the firmware and CAL_FIELD_SPEC are already
    # two hand-synced copies of this table.
    sim_calibration = {
        f["key"]: (7700.0 if f["group"] == "Load cells" else
                   1.0 if f["group"].startswith("Torque") else 10.0)
        for f in CAL_FIELD_SPEC
    }
    conn = replay.ReplaySerial(
        frames=frames,
        rate_hz=rate_hz,
        loop=bool(body.get("loop", True)),
        direction=str(body.get("direction", DIR_CW)),
        calibration=sim_calibration,
    )
    # Stops the auto-reconnect loop racing a real board in behind the
    # replay when it ends.
    user_requested_disconnect = True
    started = start_serial(f"REPLAY:{mode}", DEFAULT_BAUD, conn=conn)
    audit.log_serial("replay_started", mode=mode, rate_hz=rate_hz, user=operator)
    return {"ok": started, "mode": mode,
            "frames": len(frames) if frames is not None else None}


# ================================================================
# AUTOMATED THROTTLE PROFILE ENDPOINTS
# ================================================================
def _validate_profile(payload: dict) -> tuple[list[dict], float, str | None]:
    """Validate a profile request body. Returns (steps, ramp_rate, error)."""
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return [], 0.0, "At least one step is required"
    if len(raw_steps) > PROFILE_MAX_STEPS:
        return [], 0.0, f"Too many steps (max {PROFILE_MAX_STEPS})"

    steps: list[dict] = []
    for i, s in enumerate(raw_steps, start=1):
        if not isinstance(s, dict):
            return [], 0.0, f"Step {i} is malformed"
        try:
            us = int(s["us"])
            hold_s = float(s["hold_s"])
        except (KeyError, TypeError, ValueError):
            return [], 0.0, f"Step {i} needs a numeric 'us' and 'hold_s'"
        # Reject out-of-range targets outright rather than silently
        # clamping them — an operator who typed 2500 meant something,
        # and quietly running 2000 instead would corrupt their test.
        if not (THR_MIN <= us <= THR_MAX):
            return [], 0.0, f"Step {i}: throttle {us}µs is outside {THR_MIN}-{THR_MAX}µs"
        if not (0 < hold_s <= PROFILE_MAX_HOLD_S):
            return [], 0.0, f"Step {i}: hold time must be between 0 and {PROFILE_MAX_HOLD_S:.0f}s"
        steps.append({"us": us, "hold_s": hold_s})

    try:
        ramp_rate = float(payload.get("ramp_rate_us_per_s", DEFAULT_RAMP_RATE))
    except (TypeError, ValueError):
        return [], 0.0, "Invalid ramp rate"
    if not (PROFILE_MIN_RAMP_RATE <= ramp_rate <= PROFILE_MAX_RAMP_RATE):
        return [], 0.0, (f"Ramp rate must be between {PROFILE_MIN_RAMP_RATE:.0f} "
                         f"and {PROFILE_MAX_RAMP_RATE:.0f} µs/s")

    return steps, ramp_rate, None


@app.post("/profile/start")
async def profile_start(payload: dict, operator: str = Depends(require_auth)):
    """Start an automated throttle profile.

    Body: {"steps": [{"us": 1200, "hold_s": 5}, ...],
           "ramp_rate_us_per_s": 100}
    """
    global profile_task

    if profile_state["running"]:
        return {"ok": False, "error": "A profile is already running"}
    if not (serial_conn and serial_conn.is_open):
        return {"ok": False, "error": ERR_SERIAL_DOWN}

    steps, ramp_rate, error = _validate_profile(payload)
    if error:
        return {"ok": False, "error": error}

    profile_task = asyncio.create_task(_run_profile(steps, ramp_rate, operator))
    return {"ok": True, "steps": steps, "ramp_rate_us_per_s": ramp_rate}


@app.post("/profile/stop")
async def profile_stop(operator: str = Depends(require_auth)):
    """Abort a running profile and cut the throttle to idle immediately."""
    if not profile_state["running"]:
        # Still force a cut — if the UI thinks a profile is running and
        # the backend disagrees, the safe resolution is throttle at idle.
        write_throttle_us(THR_MIN)
        audit.note_throttle(operator, THR_MIN, source=THROTTLE_SOURCE_CUT)
        return {"ok": False, "error": "No profile running"}
    await abort_profile(cut=True)
    return {"ok": True}


@app.get("/profile/status")
def profile_status(operator: str = Depends(require_auth)):
    """Current profile phase/progress, for polling clients."""
    return {"profile": dict(profile_state)}

# ================================================================
# DATA LOGGING / RECORDING ENDPOINTS
# ================================================================
@app.post("/recording/start")
def start_recording(payload: dict | None = None, operator: str = Depends(require_auth)):
    """Begin capturing frames to disk as they arrive.

    Body (optional): {"test_name": "Prop 18x6 CW"} — names the files, so
    a folder of runs is identifiable without opening each one.
    """
    global recording
    test_name = str((payload or {}).get("test_name", "")).strip()

    # Snapshot the calibration in force NOW, not when the run ends: the
    # operator can legitimately change a scale factor afterwards, and
    # the run must stay attached to the numbers that produced it. The
    # recorder writes this into the run's .meta.json sidecar.
    status = session_recorder.start(test_name, operator, dict(calibration["values"]))
    recording = True                                           # The reader thread now writes to disk

    print(f"{LOG_RECORDING} Started by {operator}" + (f" — '{test_name}'" if test_name else ""))
    return {"ok": True, **status}

@app.post("/recording/stop")
def stop_recording(operator: str = Depends(require_auth)):
    """Close the run's files and report where they were written.

    JSON, not a download: the run is already on disk in the operator's
    Documents folder, and handing back a second copy through the browser
    only made the Downloads folder a competing home for test data.
    """
    global recording
    recording = False
    disk = session_recorder.stop()                             # Flush, close, finalise metadata
    print(f"{LOG_RECORDING} Stopped — {disk['frame_count']} frames captured")
    return {"ok": True, **disk}

@app.get("/recording/status")
def recording_status(operator: str = Depends(require_auth)):
    """Whether a recording is active, and how many frames it has captured."""
    return {**session_recorder.status(), "recording": recording}

@app.get("/recordings")
def list_recordings(operator: str = Depends(require_auth)):
    """Recent runs already written to <Documents>/NWA Bench Console/CSV."""
    return {"ok": True, "folder": str(app_paths.csv_dir()), "recordings": recorder.list_recordings()}

# ================================================================
# CALIBRATION ENDPOINTS
#   Thin pass-through to the firmware's runtime table; the dashboard's
#   settings panel is the only consumer.
# ================================================================
def _write_cal_line(text: str) -> bool:
    """Write one raw calibration command line to the ESP32."""
    if not (serial_conn and serial_conn.is_open):
        return False
    try:
        serial_conn.write(f"{text}\n".encode())
        return True
    except (serial.SerialException, OSError) as e:
        print(f"{LOG_SERIAL} Calibration write failed: {e}")
        return False

async def _request_cal_dump(timeout_s: float = CAL_DUMP_TIMEOUT_S) -> bool:
    """Ask the device for its calibration table and wait for the reply.

    Waiting on calibration_seq rather than sleeping a fixed time means
    the common case returns in a few tens of milliseconds.
    """
    before = calibration_seq
    if not _write_cal_line(CMD_CAL_DUMP):
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        await asyncio.sleep(CAL_DUMP_POLL_S)
        if calibration_seq != before:
            # The values line lands first; give the defaults line that
            # follows it a moment, so a first open of the panel already
            # has something in the "firmware default" column.
            await asyncio.sleep(CAL_DEFAULTS_GRACE_S)
            return True
    return False

def _cal_payload(extra: dict | None = None) -> dict:
    payload = {
        "ok": True,
        "fields": CAL_FIELD_SPEC,                             # So the UI needs no duplicated table of its own
        # Copied, not referenced: the serial reader thread can write into
        # these dicts at any moment, and mutating one mid-serialisation
        # raises "dictionary changed size during iteration" — rare, but
        # it would 500 the settings panel.
        "values": dict(calibration["values"]),
        "defaults": dict(calibration["defaults"]),
        "updated": calibration["updated"],
        "serial_connected": bool(serial_conn and serial_conn.is_open),
    }
    if extra:
        payload.update(extra)
    return payload

@app.get("/calibration")
async def get_calibration(refresh: bool = True, operator: str = Depends(require_auth)):
    """The ESP32's runtime calibration table.

    Re-read from the device by default, so the panel always opens on
    what the hardware is really using. ?refresh=false serves the cache.
    """
    fresh = False
    if refresh:
        fresh = await _request_cal_dump()
    return _cal_payload({
        "fresh": fresh,
        "error": "" if fresh or not refresh else
                 ("Serial not connected — showing last known values"
                  if not (serial_conn and serial_conn.is_open)
                  else "Device did not answer — showing last known values"),
    })

def _validate_cal_values(payload: dict) -> tuple[dict, str | None]:
    """Pull the {key: value} map out of a save request and range-check it."""
    values = payload.get("values")
    if not isinstance(values, dict) or not values:
        return {}, "No values supplied"

    cleaned: dict[str, float] = {}
    for key, raw in values.items():
        spec = CAL_KEYS.get(key)
        if spec is None:
            return {}, f"Unknown calibration key '{key}'"
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return {}, f"{spec['label']} ({key}): '{raw}' is not a number"
        # nan/inf parse fine and would poison every subsequent telemetry
        # frame. Rejected explicitly rather than via the range check,
        # where NaN compares False either way and would report itself as
        # "out of range", hiding the real cause.
        if v != v or v in (float("inf"), float("-inf")):
            return {}, f"{spec['label']} ({key}): must be a finite number"
        if not (spec["min"] <= v <= spec["max"]):
            return {}, (f"{spec['label']} ({key}): {v:g} is outside the accepted "
                        f"range {spec['min']:g} to {spec['max']:g}")
        cleaned[key] = v
    return cleaned, None

@app.post("/calibration")
async def set_calibration(payload: dict, operator: str = Depends(require_auth)):
    """Write one or more calibration values to the ESP32 (persisted in its NVS)."""
    # Changing a scale factor mid-sweep would splice two calibrations
    # into one run's data with nothing in the log to say where the join
    # is. A profile is the one case where that can happen without the
    # operator being at the bench, so it's refused rather than warned.
    if profile_state["running"]:
        return {"ok": False, "error": ERR_PROFILE_RUNNING_CAL}

    values, error = _validate_cal_values(payload)
    if error:
        return {"ok": False, "error": error}

    if not (serial_conn and serial_conn.is_open):
        return {"ok": False, "error": ERR_SERIAL_CONNECT_FIRST}

    written: list[str] = []
    for key, v in values.items():
        # Captured before the write, so the log records what this value
        # replaced — "18178.5 → 18200" beats "changed to 18200".
        previous = calibration["values"].get(key)
        if not _write_cal_line(CMD_CAL_SET.format(key=key, value=CAL_VALUE_FORMAT.format(v))):
            return {"ok": False, "error": ERR_SERIAL_WRITE, "written": written}
        audit.log_calibration(operator, key, previous, v)
        written.append(key)
        await asyncio.sleep(CAL_WRITE_GAP_S)                  # Keeps the firmware's RX buffer from overrunning

    fresh = await _request_cal_dump()
    return _cal_payload({
        "written": written,
        "fresh": fresh,
        "error": "" if fresh else "Saved, but the device didn't confirm — reopen settings to verify",
    })

@app.post("/calibration/reset")
async def reset_calibration(operator: str = Depends(require_auth)):
    """Restore every calibration value to the firmware's compiled-in default."""
    if profile_state["running"]:
        return {"ok": False, "error": ERR_PROFILE_RUNNING_CAL}
    if not (serial_conn and serial_conn.is_open):
        return {"ok": False, "error": ERR_SERIAL_CONNECT_FIRST}
    previous = dict(calibration["values"])                    # Keep what's about to be wiped
    if not _write_cal_line(CMD_CAL_RESET):
        return {"ok": False, "error": ERR_SERIAL_WRITE}
    audit.log_calibration_reset(operator, previous)
    # The firmware erases NVS and re-dumps on its own, but ask anyway:
    # if the erase failed it still reverted the live values, and this
    # makes sure the panel shows whatever actually took effect.
    fresh = await _request_cal_dump()
    return _cal_payload({"reset": True, "fresh": fresh})

# ================================================================
# GENERAL HTTP ENDPOINTS
# ================================================================
@app.get("/status")
def get_status(operator: str = Depends(require_auth)):
    """Serial connection status, link health, and the latest snapshot."""
    return {
        "serial": serial_status,
        "health": health_snapshot(),                          # Frame rate / frame age / stall verdict
        "clients": len(connected_clients),
        "latest": latest_data,
        "recording": recording,
        "recorder": session_recorder.status(),                 # Where this run is being written
        "profile": dict(profile_state),
        "operator": operator,
    }

@app.get("/history")
def get_history(operator: str = Depends(require_auth)):
    """The full rolling 30-second history buffer."""
    return {"data": list(history)}


@app.get("/audit/{category}")
def get_audit(category: str, limit: int = 200, operator: str = Depends(require_auth)):
    """Read back an audit trail — category is "throttle" or "calibration"."""
    if category not in (AUDIT_THROTTLE, AUDIT_CALIBRATION):
        return {"ok": False, "error": "Unknown audit category"}
    # Flushed first, so the newest entry the operator just caused is
    # actually in the list they're reading.
    audit.flush_pending_throttle(force=True)
    return {"ok": True, "category": category,
            "entries": audit.read_recent(category, max(1, min(AUDIT_READ_MAX, limit)))}


@app.get("/paths")
def get_paths(operator: str = Depends(require_auth)):
    """Where the console writes the operator's files, for display in the UI."""
    return {
        "app":      str(app_paths.app_dir()),
        "csv":      str(app_paths.csv_dir()),
        "logs":     str(app_paths.logs_dir()),
        "login":    str(app_paths.login_file()),
        "recovery": str(app_paths.recovery_dir()),
    }


# ================================================================
# DESKTOP SHELL (TAURI SIDECAR) — /health, /shutdown
# ================================================================
# Both are deliberately outside the operator-session gate that covers
# every other route, because the caller is the Rust shell, not a signed
# in human — the shell has to poll readiness and ask for a clean stop
# before any account exists or anyone has logged in. /health leaks
# nothing an attacker on loopback couldn't get by connecting to the
# port anyway, and /shutdown carries its own token instead.
# tests/test_deployment.py pins that token behaviour, and the coverage
# guard in tests/test_auth.py names these two as the only exemptions.

# Set by run_backend.py to uvicorn's "stop serving" switch. Left None
# under `uvicorn main:app` (dev) and in tests, where Ctrl+C is the way
# out and nothing should be able to stop the server over HTTP.
request_shutdown = None


@app.get("/health")
def health():
    """Readiness probe the shell polls instead of sleeping a fixed time."""
    return {
        "ok": True,
        "version": APP_VERSION,
        "serial_connected": serial_status["connected"],
        # The shell refuses to install an update while either is true —
        # an update kills the process that owns the safety cut.
        "profile_running": profile_state["phase"] not in (
            PHASE_IDLE, PHASE_DONE, PHASE_ABORTED, PHASE_ERROR),
        "recording": recording,
    }


@app.post("/shutdown")
async def shutdown_endpoint(request: Request):
    """Stop gracefully so the shutdown handler can cut the throttle.

    The handler this eventually reaches aborts a running profile *before*
    tearing down the serial port, which is what lets _run_profile()'s
    finally block write THR_MIN and flush it. A force-kill from the shell
    would skip all of that and leave the ESC driving its last commanded
    value, so the shell asks here first and only kills if this never
    lands (DEPLOYMENT.md §9).
    """
    expected = os.environ.get(SHUTDOWN_TOKEN_ENV)
    if not expected or request_shutdown is None:
        return JSONResponse({"ok": False, "error": ERR_SHUTDOWN_DISABLED}, status_code=404)

    supplied = request.headers.get(SHUTDOWN_TOKEN_HEADER, "")
    # Constant-time: a token that stops a 100 kgf test stand should not
    # be guessable a character at a time off response timing.
    if not hmac.compare_digest(supplied, expected):
        print(f"{LOG_SERVER} Rejected shutdown request with a bad token")
        return JSONResponse({"ok": False, "error": ERR_SHUTDOWN_BAD_TOKEN}, status_code=403)

    print(f"{LOG_SERVER} Shutdown requested by the desktop shell")
    request_shutdown()          # Flips uvicorn's should_exit; the shutdown handler then runs
    return {"ok": True, "stopping": True}
