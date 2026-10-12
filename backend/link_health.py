# Serial link health — everything behind the dashboard's link panel.
#
# "Connected" only means the OS handle is open. A board that has hung,
# browned out, or is wired to a dead TX line looks exactly as connected
# as a healthy one — readline() just keeps timing out. This module
# answers the questions "connected" can't, in both directions:
#
#   ESP32 → PC   is data arriving, at the promised pace, intact, and
#                complete? (frame age, rate, malformed lines, and gaps
#                in the firmware's `seq` counter = frames actually lost)
#   PC → ESP32   are commands arriving at all? Telemetry can be perfect
#                while the adapter-TX → GPIO44 wire is broken, and then
#                tare, throttle and CUT are all silently ignored. A
#                once-a-second "P<n>" ping answered by a PONG is the
#                only way to see that from the PC side.
#   the board    did it reboot (BOOT event, or `seq` restarting), and
#                what does IT see — UART overflows and framing errors on
#                the bytes coming in, unknown commands, loop overruns
#                (counters carried in every PONG)
#
# One instance lives in main.py. The serial reader thread feeds it;
# /status reads a snapshot. A lock covers both, since they run on
# different threads.
#
# Older firmware sends no `seq`, no PONG and no BOOT. Each of those is
# reported as "can't tell" rather than as a fault, so an un-reflashed
# board degrades to the previous level of health reporting instead of
# a permanent red light.

from __future__ import annotations

import threading
import time
from collections import deque

import audit
from constants import CMD_PING, EV_BOOT, EV_PONG

# ── Tunables ──────────────────────────────────────────────────────
EXPECTED_RATE_HZ   = 20.0          # Firmware TELEMETRY_PERIOD_MS = 50
LOW_RATE_HZ        = 15.0          # Below this the panel calls the rate degraded
RATE_WINDOW_S      = 5.0           # Rolling window behind frame_rate_hz
STALL_THRESHOLD_S  = 2.0           # ~40 missed frames at 20 Hz — past a hiccup
BOOT_GRACE_S       = 25.0          # ESC arming (8 s) + "remove loads" wait (5 s) + tare, with margin

PING_INTERVAL_S    = 1.0
PONG_TIMEOUT_S     = 3.5           # ~3 consecutive unanswered pings
PING_UNSUPPORTED_AFTER_S = 6.0     # Frames flowing this long with no PONG → firmware predates pings
PENDING_PING_TTL_S = 10.0
RTT_WARN_MS        = 250.0
RTT_SAMPLES        = 30

SEQ_MODULUS        = 2 ** 32       # Firmware's uint32 counter
SEQ_MAX_GAP        = 1000          # A bigger forward jump is a resync, not 1000 lost frames

TIMELINE_S         = 120           # Seconds of per-second history behind the waveform
RECENT_S           = 30.0          # Window for "in the last N s" issue counts
DROP_WARN_FRACTION = 0.01
RESET_RECENT_S     = 60.0
PORT_OPEN_RESET_S  = 5.0           # A BOOT this soon after opening is the adapter's DTR/RTS auto-reset
EVENT_LOG_MAX      = 40

LEVEL_OK    = "ok"
LEVEL_INFO  = "info"
LEVEL_WARN  = "warn"
LEVEL_ERROR = "error"
VERDICT_OFFLINE = "offline"
_LEVEL_RANK = {LEVEL_OK: 0, LEVEL_INFO: 0, LEVEL_WARN: 1, LEVEL_ERROR: 2}

CMD_LINK_OK         = "ok"
CMD_LINK_LOST       = "lost"
CMD_LINK_WAITING    = "waiting"      # Pings sent, nothing to judge by yet
CMD_LINK_UNVERIFIED = "unverified"   # Firmware doesn't answer pings
CMD_LINK_OFFLINE    = "offline"

# Counters the firmware carries in every PONG. Only increases matter —
# each is cumulative since the board booted.
DEVICE_COUNTERS = ("rx_ovf", "rx_err", "bad_cmd", "late")


def _bucket(second: int) -> dict:
    return {"t": second, "frames": 0, "dropped": 0, "malformed": 0, "rtt_ms": None}


class LinkMonitor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    # ── Lifecycle ─────────────────────────────────────────────────
    def reset(self, port: str = "", baud: int = 0, port_info: dict | None = None,
              now: float | None = None) -> None:
        """Start a fresh connection's worth of counters."""
        now = time.monotonic() if now is None else now
        with getattr(self, "_lock", threading.Lock()):
            self.frames_total = 0
            self.events_total = 0
            self.malformed_total = 0
            self.dropped_total = 0
            self.device_resets = 0
            self.connected_at = time.time() if port else None
            self._connected_mono = now
            self._first_frame_mono: float | None = None
            self._last_frame_mono: float | None = None
            self._last_frame_wall: float | None = None
            self._frame_times: deque = deque(maxlen=400)

            self.port = {"name": port, "baud": baud, **(port_info or {})}

            self._seq_seen = False
            self._last_seq: int | None = None

            self._ping_n = 0
            self._next_ping_at = now
            self._pending_pings: dict[int, float] = {}
            self.pings_sent = 0
            self.pongs_received = 0
            self._last_pong_mono: float | None = None
            self._rtts: deque = deque(maxlen=RTT_SAMPLES)

            self.device: dict = {"up_ms": None, "boot_reason": None}
            self._device_prev: dict = {}
            self._device_increases: deque = deque(maxlen=200)   # (mono, key, delta)
            self._last_reset_mono: float | None = None
            self._last_reset_reason: str | None = None
            self._last_reset_expected = False

            self._timeline: deque = deque(maxlen=TIMELINE_S)
            self._cur = _bucket(int(time.time()))

            self._was_stalled = False
            self._cmd_state = CMD_LINK_WAITING
            self.events: deque = deque(maxlen=EVENT_LOG_MAX)

    # ── Inputs (serial reader thread) ─────────────────────────────
    def note_frame(self, frame: dict, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._roll(now)
            self.frames_total += 1
            self._cur["frames"] += 1
            if self._first_frame_mono is None:
                self._first_frame_mono = now
            self._last_frame_mono = now
            # The frame's own `ts` where it has one, so last_frame_at is the
            # exact stamp of the newest frame counted in frames_total — the
            # dashboard counts frames newer than it to keep the packet
            # total live between /status polls.
            ts = frame.get("ts")
            self._last_frame_wall = (ts if isinstance(ts, (int, float))
                                     else time.time() - (time.monotonic() - now))
            self._frame_times.append(now)
            while self._frame_times and now - self._frame_times[0] > RATE_WINDOW_S:
                self._frame_times.popleft()
            self._note_seq(frame.get("seq"), now)

    def note_malformed(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._roll(now)
            self.malformed_total += 1
            self._cur["malformed"] += 1

    def note_event(self, frame: dict, now: float | None = None) -> bool:
        """Account for a firmware event line. False means don't broadcast it.

        PONGs are link bookkeeping, once a second, and every browser
        would otherwise receive them as noise.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            self._roll(now)
            self.events_total += 1
            event = frame.get("event")
            if event == EV_PONG:
                self._note_pong(frame, now)
                return False
            if event == EV_BOOT:
                self._note_boot(str(frame.get("reason") or "UNKNOWN"), now)
        return True

    def next_ping(self, now: float | None = None) -> str | None:
        """The ping command to write now, or None if one isn't due."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if now < self._next_ping_at:
                return None
            self._next_ping_at = now + PING_INTERVAL_S
            self._ping_n += 1
            self._pending_pings[self._ping_n] = now
            for n in [n for n, t in self._pending_pings.items() if now - t > PENDING_PING_TTL_S]:
                del self._pending_pings[n]
            self.pings_sent += 1
            return f"{CMD_PING}{self._ping_n}"

    def tick(self, connected: bool, now: float | None = None) -> None:
        """Roll the timeline and log state transitions. Called every reader loop."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._roll(now)
            self._transitions(connected, now)

    # ── Output (/status) ──────────────────────────────────────────
    def snapshot(self, connected: bool, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._roll(now)
            self._transitions(connected, now)

            age = (now - self._last_frame_mono) if self._last_frame_mono is not None else None
            stalled = bool(connected and (age is None or age > STALL_THRESHOLD_S))
            gaps = self._gap_stats()
            rtts = list(self._rtts)
            cmd_state = self._command_state(connected, now)
            issues = self._issues(connected, now, age, stalled, cmd_state, rtts)
            worst = max((_LEVEL_RANK[i["level"]] for i in issues), default=0)
            verdict = (VERDICT_OFFLINE if not connected
                       else LEVEL_ERROR if worst == 2 else LEVEL_WARN if worst == 1 else LEVEL_OK)

            return {
                # The original health block — the shape older UI code reads
                "frames_total":     self.frames_total,
                "events_total":     self.events_total,
                "malformed_total":  self.malformed_total,
                "last_frame_at":    self._last_frame_wall,
                "connected_at":     self.connected_at,
                "frame_rate_hz":    self._rate(),
                "last_frame_age_s": round(age, 2) if age is not None else None,
                "stalled":          stalled,
                # Link panel
                "verdict":          verdict,
                "expected_rate_hz": EXPECTED_RATE_HZ,
                "dropped_total":    self.dropped_total,
                "seq_supported":    self._seq_seen,
                "gap_p95_ms":       gaps[0],
                "gap_max_ms":       gaps[1],
                "device_resets":    self.device_resets,
                "command_link": {
                    "state":           cmd_state,
                    "rtt_ms":          round(rtts[-1], 1) if rtts else None,
                    "rtt_avg_ms":      round(sum(rtts) / len(rtts), 1) if rtts else None,
                    "rtt_max_ms":      round(max(rtts), 1) if rtts else None,
                    "pings_sent":      self.pings_sent,
                    "pongs_received":  self.pongs_received,
                    "last_pong_age_s": (round(now - self._last_pong_mono, 2)
                                        if self._last_pong_mono is not None else None),
                },
                "device":   dict(self.device),
                "port":     dict(self.port),
                "timeline": list(self._timeline),
                "issues":   issues,
                "events":   list(self.events),
            }

    # ── Internals (caller holds the lock) ─────────────────────────
    def _log(self, level: str, text: str, audit_event: str | None = None, **fields) -> None:
        self.events.append({"ts": time.time(), "level": level, "text": text})
        if audit_event:
            # Persisted alongside the existing serial_connected/closed
            # lines, so a stall or reboot during a run is on record next
            # to the throttle commands of the same moment.
            audit.log_serial(audit_event, port=self.port.get("name", ""), **fields)

    def _roll(self, now: float) -> None:
        second = int(time.time() - (time.monotonic() - now))
        if second <= self._cur["t"]:
            return
        self._timeline.append(self._cur)
        # Seconds with nothing at all still need a (zero) point, or a
        # dead link would read as a gap in the waveform rather than a drop.
        for s in range(max(self._cur["t"] + 1, second - TIMELINE_S), second):
            self._timeline.append(_bucket(s))
        self._cur = _bucket(second)

    def _note_seq(self, seq, now: float) -> None:
        if not isinstance(seq, int) or isinstance(seq, bool):
            return
        self._seq_seen = True
        last = self._last_seq
        self._last_seq = seq
        if last is None:
            return
        gap = (seq - last) % SEQ_MODULUS
        if gap == 0:
            return                                         # A repeated line, not a loss
        if gap <= SEQ_MAX_GAP:
            lost = gap - 1
            if lost:
                self.dropped_total += lost
                self._cur["dropped"] += lost
            return
        if seq < last and seq <= SEQ_MAX_GAP:
            # The counter started again from zero: the board rebooted
            # without its BOOT line reaching us (a BOOT clears _last_seq,
            # so a reboot that WAS announced never gets here).
            self._record_reset(None, now, expected=False,
                               text="ESP32 restarted (its frame counter started again from zero)")
            return
        self._log(LEVEL_INFO, f"Frame counter jumped from {last} to {seq} — resynchronised")

    def _note_boot(self, reason: str, now: float) -> None:
        self._last_seq = None                              # The next seq is a fresh start, not a reboot to report twice
        self.device["boot_reason"] = reason
        self._device_prev = {}                             # Its counters restart from zero too
        expected = (now - self._connected_mono) < PORT_OPEN_RESET_S and reason != "BROWNOUT"
        if expected:
            text = "ESP32 restarted as the port opened (normal for an auto-reset adapter) — booting, telemetry in ~15 s"
        else:
            text = f"ESP32 restarted — reset reason {reason}"
        self._record_reset(reason, now, expected, text)

    def _record_reset(self, reason: str | None, now: float, expected: bool, text: str) -> None:
        self._last_reset_mono = now
        self._last_reset_reason = reason
        self._last_reset_expected = expected
        if not expected:
            self.device_resets += 1
        self._log(LEVEL_INFO if expected else LEVEL_WARN, text,
                  audit_event=None if expected else "device_reset", reason=reason)

    def _note_pong(self, frame: dict, now: float) -> None:
        self.pongs_received += 1
        self._last_pong_mono = now
        n = frame.get("n")
        sent = self._pending_pings.pop(n, None) if isinstance(n, int) else None
        if sent is not None:
            rtt = (now - sent) * 1000.0
            self._rtts.append(rtt)
            cur = self._cur["rtt_ms"]
            self._cur["rtt_ms"] = round(rtt if cur is None else max(cur, rtt), 1)

        up = frame.get("up_ms")
        if isinstance(up, (int, float)):
            self.device["up_ms"] = int(up)
        for key in DEVICE_COUNTERS:
            val = frame.get(key)
            if not isinstance(val, int) or isinstance(val, bool):
                continue
            self.device[key] = val
            prev = self._device_prev.get(key)
            self._device_prev[key] = val
            if prev is None or val <= prev:
                continue                                   # First sight, or the board restarted
            delta = val - prev
            self._device_increases.append((now, key, delta))
            if key == "rx_err":
                self._log(LEVEL_WARN, f"ESP32 received {delta} corrupted byte(s) on its command input")
            elif key == "rx_ovf":
                self._log(LEVEL_WARN, f"ESP32 command input overflowed {delta} time(s) — incoming bytes lost")
            elif key == "bad_cmd":
                self._log(LEVEL_WARN, f"ESP32 ignored {delta} unrecognised command line(s)")

    def _command_state(self, connected: bool, now: float) -> str:
        if not connected:
            return CMD_LINK_OFFLINE
        if self.pongs_received == 0:
            if (self._first_frame_mono is not None
                    and now - self._first_frame_mono > PING_UNSUPPORTED_AFTER_S
                    and self.pings_sent >= 3):
                return CMD_LINK_UNVERIFIED
            return CMD_LINK_WAITING
        if self._last_pong_mono is not None and now - self._last_pong_mono > PONG_TIMEOUT_S:
            return CMD_LINK_LOST
        return CMD_LINK_OK

    def _transitions(self, connected: bool, now: float) -> None:
        """Log the moments things change, not every second they stay bad."""
        if not connected:
            self._was_stalled = False
            return
        flowing_before = self._first_frame_mono is not None
        age = (now - self._last_frame_mono) if self._last_frame_mono is not None else None
        stalled = flowing_before and age is not None and age > STALL_THRESHOLD_S
        if stalled and not self._was_stalled:
            self._log(LEVEL_ERROR, "Telemetry stopped arriving", audit_event="stall")
        elif not stalled and self._was_stalled:
            self._log(LEVEL_OK, "Telemetry resumed", audit_event="stall_cleared")
        self._was_stalled = stalled

        state = self._command_state(connected, now)
        if state != self._cmd_state:
            if state == CMD_LINK_LOST:
                self._log(LEVEL_ERROR, "ESP32 stopped answering link checks — commands may not be reaching it",
                          audit_event="command_link_lost")
            elif state == CMD_LINK_OK and self._cmd_state == CMD_LINK_LOST:
                self._log(LEVEL_OK, "ESP32 is answering link checks again",
                          audit_event="command_link_restored")
            elif state == CMD_LINK_OK:
                self._log(LEVEL_OK, "Command path confirmed — the ESP32 is answering link checks")
            elif state == CMD_LINK_UNVERIFIED:
                self._log(LEVEL_INFO, "Firmware doesn't answer link checks — command delivery can't be confirmed")
            self._cmd_state = state

    def _rate(self) -> float:
        times = self._frame_times
        if len(times) < 2:
            return 0.0
        span = times[-1] - times[0]
        return round((len(times) - 1) / span, 1) if span > 0 else 0.0

    def _gap_stats(self) -> tuple[float | None, float | None]:
        times = list(self._frame_times)
        if len(times) < 3:
            return None, None
        gaps = sorted((b - a) * 1000.0 for a, b in zip(times, times[1:]))
        p95 = gaps[min(len(gaps) - 1, int(round(0.95 * (len(gaps) - 1))))]
        return round(p95, 1), round(gaps[-1], 1)

    def _recent_counts(self, now: float) -> dict:
        cutoff = int(time.time() - (time.monotonic() - now) - RECENT_S)
        frames = dropped = malformed = 0
        for b in list(self._timeline) + [self._cur]:
            if b["t"] >= cutoff:
                frames += b["frames"]
                dropped += b["dropped"]
                malformed += b["malformed"]
        device = {k: 0 for k in DEVICE_COUNTERS}
        for t, key, delta in self._device_increases:
            if now - t <= RECENT_S:
                device[key] += delta
        return {"frames": frames, "dropped": dropped, "malformed": malformed, **device}

    def _issues(self, connected, now, age, stalled, cmd_state, rtts) -> list[dict]:
        """What is wrong right now, in the operator's terms, with what to check."""
        if not connected:
            return []
        out: list[dict] = []

        def add(level, text, hint=""):
            out.append({"level": level, "text": text, "hint": hint})

        since_connect = now - self._connected_mono
        booting = (self._last_reset_mono is not None
                   and now - self._last_reset_mono < BOOT_GRACE_S
                   and (self._last_frame_mono is None or self._last_frame_mono < self._last_reset_mono))

        if stalled:
            if self._first_frame_mono is None and (since_connect < BOOT_GRACE_S or booting):
                add(LEVEL_INFO, "Waiting for the first frame — the ESP32 needs ~15 s to boot "
                                "(ESC arming, then the tare)")
            elif booting:
                add(LEVEL_WARN, "ESP32 is rebooting — telemetry will resume in ~15 s",
                    "Throttle and tare were reset by the reboot. Re-check them before continuing.")
            else:
                add(LEVEL_ERROR,
                    f"No telemetry for {age:.0f} s" if age is not None else "No telemetry has arrived",
                    "Check the board's power and the ESP32 TX (GPIO43) → adapter RX wire. "
                    "A board that has hung needs a reset.")
        elif cmd_state == CMD_LINK_LOST:
            add(LEVEL_ERROR,
                "Telemetry is arriving, but the ESP32 isn't answering — tare, throttle and CUT may not reach it",
                "Check the adapter TX → ESP32 RX (GPIO44) wire.")

        recent = self._recent_counts(now)
        if recent["dropped"]:
            total = recent["frames"] + recent["dropped"]
            frac = recent["dropped"] / total if total else 0.0
            if frac >= DROP_WARN_FRACTION:
                add(LEVEL_WARN, f"{recent['dropped']} frame(s) lost in the last {RECENT_S:.0f} s ({frac:.1%})",
                    "Lines lost between the ESP32 and the PC: a loose or long USB cable, an unpowered hub, "
                    "or interference from the ESC/motor wiring.")
        if recent["malformed"]:
            add(LEVEL_WARN, f"{recent['malformed']} corrupted line(s) in the last {RECENT_S:.0f} s",
                "Usually a noisy cable, or a baud mismatch (the firmware runs at 115200).")

        rate = self._rate()
        if not stalled and self._first_frame_mono is not None and 0 < rate < LOW_RATE_HZ:
            hint = ("The ESP32's measurement loop is overrunning its 50 ms budget."
                    if recent["late"] else
                    "Frames are leaving the ESP32 slower than expected, or being lost on the way.")
            add(LEVEL_WARN, f"Telemetry at {rate:.1f} Hz (expected {EXPECTED_RATE_HZ:.0f})", hint)

        if len(rtts) >= 3:
            recent_rtt = rtts[-5:]
            avg = sum(recent_rtt) / len(recent_rtt)
            if avg > RTT_WARN_MS:
                add(LEVEL_WARN, f"Commands take {avg:.0f} ms to reach the ESP32 and back",
                    "A busy USB hub, or the board spending long stretches printing logs.")

        if recent["rx_err"] or recent["rx_ovf"]:
            add(LEVEL_WARN,
                f"ESP32 saw {recent['rx_err'] + recent['rx_ovf']} corrupted or lost incoming byte(s) "
                f"in the last {RECENT_S:.0f} s",
                "Noise on the adapter TX → GPIO44 wire — route it away from motor and ESC wiring.")
        if recent["bad_cmd"]:
            add(LEVEL_WARN, f"ESP32 ignored {recent['bad_cmd']} unrecognised command(s)",
                "Usually bytes corrupted on the way in.")

        if (self._last_reset_mono is not None and not self._last_reset_expected
                and now - self._last_reset_mono < RESET_RECENT_S and not booting):
            ago = now - self._last_reset_mono
            if self._last_reset_reason == "BROWNOUT":
                hint = ("Its supply sagged. Power the board from a source that can take the ESC arming "
                        "surge rather than the adapter's 3.3 V pin.")
            else:
                hint = "Throttle and tare were reset by the reboot. Re-check them before continuing."
            reason = f" ({self._last_reset_reason})" if self._last_reset_reason else ""
            add(LEVEL_WARN, f"ESP32 restarted{reason} {ago:.0f} s ago", hint)

        if self._first_frame_mono is not None:
            old = []
            if not self._seq_seen:
                old.append("dropped frames can't be counted")
            if cmd_state == CMD_LINK_UNVERIFIED:
                old.append("command delivery can't be confirmed")
            if old:
                add(LEVEL_INFO, "Older firmware: " + " and ".join(old),
                    "Flash the current esp32/main.c to enable full link checks.")
        return out
