#  Calibration guard rails + audit trail + serial link health.
#  The guard under test at the top is the one case where a scale
#  factor could change mid-sweep with nobody at the bench, splicing
#  two calibrations into one run's data with nothing in the log to
#  say where the join is.

import json
import time

import pytest

import audit
import main
from conftest import FakeSerial


def test_calibration_write_is_refused_while_a_profile_is_running(auth_client, fake_serial):
    main.profile_state["running"] = True

    body = auth_client.post("/calibration",
                            json={"values": {"thrust_scale": 18000.0}}).json()

    assert body["ok"] is False
    assert "profile is running" in body["error"]
    assert fake_serial.writes == [], "nothing should reach the device"


def test_calibration_reset_is_refused_while_a_profile_is_running(auth_client, fake_serial):
    main.profile_state["running"] = True

    body = auth_client.post("/calibration/reset").json()

    assert body["ok"] is False
    assert "profile is running" in body["error"]
    assert fake_serial.writes == []


def test_calibration_write_is_allowed_once_the_profile_stops(auth_client, fake_serial):
    main.profile_state["running"] = False
    auth_client.post("/calibration", json={"values": {"thrust_scale": 18000.0}})

    assert any(w.startswith(b"Cthrust_scale=") for w in fake_serial.writes)


@pytest.mark.parametrize("value, expected", [
    (0.0,          "outside the accepted range"),
    (1.0e9,        "outside the accepted range"),
    ("not-a-num",  "is not a number"),
    (None,         "is not a number"),
])
def test_out_of_range_values_never_reach_the_device(auth_client, fake_serial, value, expected):
    body = auth_client.post("/calibration",
                            json={"values": {"thrust_scale": value}}).json()
    assert body["ok"] is False
    assert expected in body["error"]
    assert fake_serial.writes == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_are_rejected(value):
    """Checked at the validator, not over HTTP.

    NaN and Infinity aren't valid JSON, so neither a browser nor the
    test client can even encode them into a request body — but they
    parse fine from a raw float and would poison every subsequent
    telemetry frame, so the guard still has to exist and be pinned.
    """
    cleaned, error = main._validate_cal_values({"values": {"thrust_scale": value}})

    assert cleaned == {}
    assert "finite" in error, f"expected a finite-number error, got {error!r}"


def test_unknown_keys_are_refused(auth_client, fake_serial):
    body = auth_client.post("/calibration",
                            json={"values": {"made_up_key": 1.0}}).json()
    assert body["ok"] is False
    assert "Unknown calibration key" in body["error"]


def test_values_are_written_at_nine_significant_digits(auth_client, fake_serial):
    """9 sig-figs is the documented round-trip width for a 32-bit float."""
    auth_client.post("/calibration", json={"values": {"thrust_scale": 18178.5}})

    line = next(w.decode().strip() for w in fake_serial.writes
                if w.startswith(b"Cthrust_scale="))
    assert line == "Cthrust_scale=18178.5"


def test_every_calibration_write_is_logged_with_the_value_it_replaced(auth_client, fake_serial):
    main.calibration["values"] = {"thrust_scale": 18178.5}
    auth_client.post("/calibration", json={"values": {"thrust_scale": 18200.0}})

    entries = audit.read_recent("calibration")
    assert entries, "the write should have produced a log entry"
    latest = entries[0]
    assert latest["event"] == "set"
    assert latest["user"] == "testop"
    assert latest["key"] == "thrust_scale"
    assert latest["old"] == 18178.5
    assert latest["new"] == 18200.0
    assert "ts" in latest


def test_a_reset_records_what_was_wiped(auth_client, fake_serial):
    main.calibration["values"] = {"thrust_scale": 18178.5, "cal_kg_s1": 7700.0}
    auth_client.post("/calibration/reset")

    reset_entry = next(e for e in audit.read_recent("calibration") if e["event"] == "reset")
    assert reset_entry["user"] == "testop"
    assert reset_entry["previous"]["thrust_scale"] == 18178.5


def test_audit_endpoint_rejects_unknown_categories(auth_client):
    assert auth_client.get("/audit/nonsense").json()["ok"] is False


def test_a_cut_is_logged_immediately_not_debounced(auth_client, fake_serial):
    """A safety action must be on disk before the process can die."""
    audit.note_throttle("testop", main.THR_MIN, source="cut")

    entries = audit.read_recent("throttle")
    assert entries[0]["event"] == "cut"
    assert entries[0]["us"] == main.THR_MIN


def test_slider_drags_collapse_to_a_single_settled_entry(auth_client):
    """A drag emits a command every ~80 ms; only where it lands matters."""
    for us in range(1000, 1500, 20):                # A drag, 25 commands
        audit.note_throttle("testop", us)
    audit.flush_pending_throttle(force=True)

    sets = [e for e in audit.read_recent("throttle") if e["event"] == "set"]
    assert len(sets) == 1, f"expected one settled entry, got {len(sets)}"
    assert sets[0]["us"] == 1480
    assert sets[0]["user"] == "testop"


def test_an_unsettled_throttle_is_not_written_yet(auth_client):
    audit.note_throttle("testop", 1400)
    audit.flush_pending_throttle()                  # Not forced, and not yet settled

    assert [e for e in audit.read_recent("throttle") if e["event"] == "set"] == []


def test_profile_lifecycle_is_attributed_to_the_operator(auth_client):
    audit.log_profile("testop", "start", steps=[{"us": 1400, "hold_s": 2}])

    entry = audit.read_recent("throttle")[0]
    assert entry["event"] == "profile_start"
    assert entry["user"] == "testop"


def test_audit_log_survives_a_torn_final_line(auth_client, data_dir):
    """A hard kill can leave half a line; reading must not blow up."""
    audit.log_profile("testop", "start")
    path = next((data_dir / "Logs").glob("throttle-*.jsonl"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"event": "truncated"')          # No closing brace or newline

    entries = audit.read_recent("throttle")         # Must not raise
    assert any(e["event"] == "profile_start" for e in entries)


def test_health_starts_empty_and_reports_no_stall_when_disconnected(auth_client):
    health = auth_client.get("/status").json()["health"]
    assert health["frames_total"] == 0
    assert health["last_frame_age_s"] is None
    assert health["stalled"] is False, "not connected is not the same as stalled"


def test_frames_are_counted_and_a_rate_is_derived(auth_client, fake_serial):
    for _ in range(10):
        main._note_frame()
        time.sleep(0.01)

    health = auth_client.get("/status").json()["health"]
    assert health["frames_total"] == 10
    assert health["frame_rate_hz"] > 0
    assert health["last_frame_age_s"] < 1.0


def test_an_open_port_with_no_frames_is_reported_as_stalled(auth_client, fake_serial):
    """The failure this exists for: connected, but the board is dead."""
    main._note_frame()
    main.serial_health["last_frame_at"] = time.time() - 5.0    # No data for 5 s

    health = auth_client.get("/status").json()["health"]
    assert health["stalled"] is True
    assert health["last_frame_age_s"] >= 5.0


def test_malformed_lines_are_counted_separately_from_frames(auth_client, fake_serial):
    main.serial_health["malformed_total"] = 3
    health = auth_client.get("/status").json()["health"]
    assert health["malformed_total"] == 3
    assert health["frames_total"] == 0
