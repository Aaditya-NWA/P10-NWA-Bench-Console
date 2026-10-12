#  Serial link health — both directions, and the board itself.
#  The failure each test pins down is one that "connected: true" hides:
#  frames silently lost on the way in, a command wire that has died
#  while telemetry still looks perfect, and a board that rebooted
#  mid-run and lost its tare and throttle state.

import json
import time

import pytest

import link_health
import main
import replay
from link_health import LinkMonitor


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def feed_frames(mon: LinkMonitor, start: float, seconds: float, hz: float = 20.0, seq0: int = 0) -> float:
    """Frames at a steady rate from `start`; returns the time of the last one."""
    n = int(seconds * hz)
    t = start
    for i in range(n):
        t = start + i / hz
        mon.note_frame({"seq": seq0 + i}, now=t)
    return t


@pytest.fixture
def mon() -> LinkMonitor:
    m = LinkMonitor()
    m.reset("COM_TEST", 115200, now=time.monotonic() - 60)   # Connected a minute ago — past the boot grace
    return m


# ── ESP32 → PC: lost frames ───────────────────────────────────────
def test_gaps_in_the_frame_counter_are_counted_as_dropped_frames(mon):
    t = time.monotonic()
    for seq in (0, 1, 2, 5, 6):
        mon.note_frame({"seq": seq}, now=t)

    assert mon.dropped_total == 2
    assert mon.snapshot(connected=True, now=t)["seq_supported"] is True


def test_a_repeated_frame_is_not_a_drop(mon):
    t = time.monotonic()
    for seq in (7, 7, 8):
        mon.note_frame({"seq": seq}, now=t)
    assert mon.dropped_total == 0


def test_the_counter_wrapping_at_2_to_the_32_is_not_a_reboot(mon):
    t = time.monotonic()
    mon.note_frame({"seq": 2 ** 32 - 1}, now=t)
    mon.note_frame({"seq": 0}, now=t)

    assert mon.dropped_total == 0
    assert mon.device_resets == 0


def test_heavy_recent_loss_is_raised_as_an_issue(mon):
    t = time.monotonic()
    seq = 0
    for i in range(100):
        mon.note_frame({"seq": seq}, now=t)
        seq += 2 if i % 10 == 0 else 1                       # ~10% of frames lost
    snap = mon.snapshot(connected=True, now=t)

    assert snap["verdict"] == "warn"
    assert any("lost" in i["text"] for i in snap["issues"])


def test_firmware_without_seq_is_reported_as_unknown_not_faulty():
    t = time.monotonic()
    old = LinkMonitor()
    old.reset("COM_OLD", 115200, now=t - 60)
    for i in range(40):
        old.note_frame({}, now=t - 2 + i * 0.05)             # No seq: an older build
    snap = old.snapshot(connected=True, now=t)

    assert snap["seq_supported"] is False
    assert snap["dropped_total"] == 0
    assert snap["verdict"] == "ok"
    assert any("Older firmware" in i["text"] for i in snap["issues"])


# ── PC → ESP32: is anything we send arriving? ─────────────────────
def test_a_ping_answered_by_a_pong_confirms_the_command_path(mon):
    t = time.monotonic()
    ping = mon.next_ping(now=t)
    assert ping == "P1"

    broadcast = mon.note_event({"event": "PONG", "n": 1, "up_ms": 12000,
                                "rx_ovf": 0, "rx_err": 0, "bad_cmd": 0, "late": 0}, now=t + 0.012)
    snap = mon.snapshot(connected=True, now=t + 0.02)

    assert broadcast is False, "PONGs are bookkeeping — not for every browser"
    assert snap["command_link"]["state"] == "ok"
    assert snap["command_link"]["rtt_ms"] == pytest.approx(12.0, abs=0.5)
    assert snap["device"]["up_ms"] == 12000


def test_pings_are_spaced_not_sent_on_every_loop(mon):
    t = time.monotonic()
    assert mon.next_ping(now=t) is not None
    assert mon.next_ping(now=t + 0.1) is None
    assert mon.next_ping(now=t + 1.01) is not None


def test_telemetry_flowing_but_pongs_stopped_is_a_fault(mon):
    """The case this whole feature exists for: data looks perfect, but
    the wire carrying tare/throttle/CUT to the board is dead."""
    t0 = time.monotonic()
    mon.next_ping(now=t0)
    mon.note_event({"event": "PONG", "n": 1}, now=t0 + 0.01)
    last = feed_frames(mon, t0, 10)                          # Frames keep coming for 10 s…
    for k in range(1, 10):
        mon.next_ping(now=t0 + k)                            # …pings keep going out, unanswered

    snap = mon.snapshot(connected=True, now=last + 0.01)

    assert snap["stalled"] is False
    assert snap["command_link"]["state"] == "lost"
    assert snap["verdict"] == "error"
    assert any("CUT" in i["text"] for i in snap["issues"])


def test_firmware_that_never_answers_pings_is_unverified_not_failed(mon):
    t0 = time.monotonic()
    for k in range(8):
        mon.next_ping(now=t0 + k)
    last = feed_frames(mon, t0, 8)

    snap = mon.snapshot(connected=True, now=last + 0.01)

    assert snap["command_link"]["state"] == "unverified"
    assert snap["verdict"] == "ok", "an un-reflashed board must not show a permanent red light"


def test_increasing_device_side_errors_are_raised(mon):
    t = time.monotonic()
    mon.note_event({"event": "PONG", "n": 0, "rx_err": 0, "rx_ovf": 0, "bad_cmd": 0, "late": 0}, now=t)
    mon.note_event({"event": "PONG", "n": 0, "rx_err": 4, "rx_ovf": 0, "bad_cmd": 1, "late": 0}, now=t + 1)
    feed_frames(mon, t, 2)

    snap = mon.snapshot(connected=True, now=t + 2)

    texts = " ".join(i["text"] for i in snap["issues"])
    assert "corrupted or lost incoming" in texts
    assert "unrecognised" in texts
    assert snap["device"]["rx_err"] == 4


# ── The board itself ──────────────────────────────────────────────
def test_a_boot_right_after_opening_the_port_is_expected():
    """Opening the port toggles DTR/RTS and reboots the chip — normal."""
    m = LinkMonitor()
    t = time.monotonic()
    m.reset("COM_TEST", 115200, now=t)
    m.note_event({"event": "BOOT", "reason": "POWERON"}, now=t + 0.3)
    snap = m.snapshot(connected=True, now=t + 1)

    assert snap["device_resets"] == 0
    assert snap["device"]["boot_reason"] == "POWERON"
    assert all(i["level"] != "error" for i in snap["issues"])


def test_a_brownout_mid_run_is_counted_and_explained(mon):
    t = time.monotonic()
    feed_frames(mon, t - 10, 10, seq0=500)
    mon.note_event({"event": "BOOT", "reason": "BROWNOUT"}, now=t)
    feed_frames(mon, t + 16, 3, seq0=0)                      # Back after the ~15 s boot

    snap = mon.snapshot(connected=True, now=t + 19)

    assert snap["device_resets"] == 1, "BOOT and the seq restart are ONE reset"
    reset_issue = next(i for i in snap["issues"] if "restarted" in i["text"])
    assert "BROWNOUT" in reset_issue["text"]
    assert "supply" in reset_issue["hint"]


def test_a_seq_restart_without_a_boot_line_still_counts_as_a_reboot(mon):
    t = time.monotonic()
    feed_frames(mon, t - 5, 5, seq0=9000)
    mon.note_frame({"seq": 0}, now=t)

    assert mon.device_resets == 1


# ── Stall and waveform ────────────────────────────────────────────
def test_a_stall_is_logged_once_when_it_starts_and_once_when_it_clears(mon):
    t = time.monotonic()
    last = feed_frames(mon, t, 2)
    mon.tick(connected=True, now=last + 3)
    mon.tick(connected=True, now=last + 4)                   # Still stalled — no second line
    mon.note_frame({"seq": 40}, now=last + 5)
    mon.tick(connected=True, now=last + 5.01)

    texts = [e["text"] for e in mon.events]
    assert texts.count("Telemetry stopped arriving") == 1
    assert texts.count("Telemetry resumed") == 1


def test_waiting_for_the_first_frame_after_connect_is_not_an_error():
    m = LinkMonitor()
    t = time.monotonic()
    m.reset("COM_TEST", 115200, now=t)
    snap = m.snapshot(connected=True, now=t + 5)

    assert snap["stalled"] is True                           # The raw flag keeps its old meaning
    assert snap["verdict"] != "error"
    assert any("Waiting for the first frame" in i["text"] for i in snap["issues"])


def test_the_timeline_has_one_point_per_second_including_silent_ones(mon):
    t = time.monotonic()
    feed_frames(mon, t, 3)
    snap = mon.snapshot(connected=True, now=t + 8)           # 3 s of data, then 5 s of nothing

    timeline = snap["timeline"]
    seconds = [b["t"] for b in timeline]
    assert seconds == sorted(seconds)
    assert all(b - a == 1 for a, b in zip(seconds, seconds[1:])), "no holes in the waveform"
    assert sum(b["frames"] for b in timeline) == 60
    assert timeline[-1]["frames"] == 0


def test_disconnected_reports_offline_with_no_issues(mon):
    snap = mon.snapshot(connected=False)
    assert snap["verdict"] == "offline"
    assert snap["issues"] == []
    assert snap["command_link"]["state"] == "offline"


# ── End to end, through the real reader thread ────────────────────
@pytest.fixture
def stop_reader():
    yield
    main.stop_serial()
    wait_for(lambda: main.serial_conn is None, timeout=3)


def test_the_reader_pings_and_the_simulator_answers(auth_client, stop_reader):
    auth_client.post("/replay/start", json={"mode": "simulate", "rate_hz": 50})

    assert wait_for(lambda: main.link.pongs_received >= 2, timeout=6), "no PONGs came back"
    health = auth_client.get("/status").json()["health"]

    assert health["command_link"]["state"] == "ok"
    assert health["command_link"]["rtt_ms"] is not None
    assert health["seq_supported"] is True
    assert health["dropped_total"] == 0
    assert health["port"]["name"].startswith("REPLAY")
    assert all("event" not in f for f in main.history), "events never enter history"


def test_simulated_tare_reports_started_then_done_with_per_cell_noise(stop_reader):
    conn = replay.ReplaySerial(rate_hz=50)
    conn.write(b"T\n")

    assert json.loads(conn.readline())["event"] == "TARE_STARTED"
    deadline = time.monotonic() + 4
    done = None
    while time.monotonic() < deadline and done is None:
        line = conn.readline()
        if line and b'"TARE_DONE"' in line:
            done = json.loads(line)

    assert done is not None, "TARE_DONE never arrived"
    assert done["ok"] is True
    assert set(done["cells"]) == {"s1", "s2", "s3", "s4", "thrust"}
    assert done["failed"] == []
