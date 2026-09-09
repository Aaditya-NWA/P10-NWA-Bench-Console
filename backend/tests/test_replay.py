#  Replay / simulated serial source  (improvements.txt #26, #35)
#  Two things being pinned here:
#    * the whole stack runs with no ESP32 attached — frames travel the
#      REAL serial_reader_thread, so history, health counters and the
#      recording pipeline are all genuinely exercised
#    * it stays OFF unless explicitly enabled. Synthetic telemetry is
#      indistinguishable from real telemetry once it is in a CSV, and
#      this machine produces test reports.

import json
import time

import pytest

import main
import replay


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll until predicate holds. The reader runs on its own thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def stop_reader():
    yield
    main.stop_serial()
    wait_for(lambda: main.serial_conn is None, timeout=3)


def test_replay_is_refused_when_not_enabled(auth_client, monkeypatch):
    monkeypatch.delenv(replay.ENABLE_ENV, raising=False)

    body = auth_client.post("/replay/start", json={"mode": "simulate"}).json()

    assert body["ok"] is False
    assert replay.ENABLE_ENV in body["error"]
    assert main.serial_conn is None


@pytest.mark.parametrize("value, enabled", [
    ("1", True), ("true", True), ("", False), ("0", False), ("false", False),
])
def test_enable_flag_parsing(monkeypatch, value, enabled):
    monkeypatch.setenv(replay.ENABLE_ENV, value)
    assert replay.replay_enabled() is enabled


def test_simulated_source_feeds_frames_through_the_real_reader(auth_client, stop_reader):
    body = auth_client.post("/replay/start",
                            json={"mode": "simulate", "rate_hz": 50}).json()
    assert body["ok"] is True

    assert wait_for(lambda: len(main.history) >= 10), "no frames arrived"

    frame = main.history[-1]
    for key in ("torque", "thrust", "s1", "s2", "s3", "s4", "stable", "dir",
                "rpm", "rpm_raw", "current_a", "voltage_v",
                "current_a2", "voltage_v2", "throttle_us", "ts"):
        assert key in frame, f"replayed frame is missing {key}"


def test_replayed_frames_update_the_health_counters(auth_client, stop_reader):
    auth_client.post("/replay/start", json={"mode": "simulate", "rate_hz": 50})
    assert wait_for(lambda: main.serial_health["frames_total"] >= 10)

    health = auth_client.get("/status").json()["health"]
    assert health["frames_total"] >= 10
    assert health["stalled"] is False
    assert health["frame_rate_hz"] > 0


def test_the_simulated_bench_responds_to_throttle(auth_client, stop_reader):
    """Not a noise generator: rpm and thrust follow the commanded throttle."""
    auth_client.post("/replay/start", json={"mode": "simulate", "rate_hz": 50})
    assert wait_for(lambda: len(main.history) >= 5)

    idle_rpm = main.history[-1]["rpm"]
    main.write_throttle_us(1800)

    assert wait_for(lambda: main.history[-1]["rpm"] > idle_rpm + 1000, timeout=6), \
        "rpm did not follow the throttle command"
    assert main.history[-1]["thrust"] > 0
    assert main.history[-1]["throttle_us"] == 1800


def test_simulated_source_answers_commands_with_firmware_event_lines(stop_reader):
    conn = replay.ReplaySerial(rate_hz=50)

    conn.write(b"T\n")
    assert json.loads(conn.readline())["event"] == "TARE_DONE"

    conn.write(b"S1450\n")
    ack = json.loads(conn.readline())
    assert ack == {"event": "THROTTLE_SET", "us": 1450}


def test_simulated_source_clamps_out_of_range_throttle(stop_reader):
    conn = replay.ReplaySerial(rate_hz=50)
    conn.write(b"S9999\n")
    assert json.loads(conn.readline())["us"] == replay.THR_MAX


def test_a_recovery_jsonl_can_be_replayed(auth_client, data_dir, tmp_path, stop_reader):
    source = tmp_path / "captured.jsonl"
    with source.open("w", encoding="utf-8") as fh:
        for i in range(5):
            fh.write(json.dumps({
                "torque": 1.0 + i, "thrust": 40.0 + i, "rpm": 10000 + i * 100,
                "rpm_raw": 10010 + i * 100, "s1": 0.1, "s2": 0.2, "s3": 0.3, "s4": 0.4,
                "stable": True, "dir": "CW", "throttle_us": 1300,
                "current_a": 1.0, "voltage_v": 22.0,
                "current_a2": 0.0, "voltage_v2": 0.0,
            }) + "\n")

    body = auth_client.post("/replay/start", json={
        "mode": "file", "file": str(source), "rate_hz": 50, "loop": False}).json()
    assert body["ok"] is True
    assert body["frames"] == 5

    assert wait_for(lambda: len(main.history) >= 5)
    assert [round(f["torque"], 1) for f in list(main.history)[:5]] == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_a_recorded_csv_round_trips_back_through_replay(auth_client, data_dir, stop_reader):
    """Record a run, then feed that same CSV back in — end to end."""
    auth_client.post("/recording/start", json={"test_name": "RoundTrip"})
    for i in range(4):
        frame = {"ts": time.time(), "torque": 2.5 + i, "thrust": 50.0 + i,
                 "s1": 0.1, "s2": 0.2, "s3": 0.3, "s4": 0.4, "stable": True, "dir": "CW",
                 "rpm": 9000.0 + i, "rpm_raw": 9010.0 + i, "throttle_us": 1350,
                 "current_a": 2.0, "voltage_v": 23.0, "current_a2": 0.0, "voltage_v2": 0.0}
        main.session_recorder.write(frame)
    auth_client.post("/recording/stop")

    recorded_csv = next((data_dir / "CSV").glob("*.csv"))
    frames = replay.load_frames(str(recorded_csv))

    assert len(frames) == 4
    assert frames[0]["torque"] == 2.5
    assert frames[0]["dir"] == "CW"
    assert frames[0]["stable"] is True
    assert frames[0]["throttle_us"] == 1350.0


def test_replaying_a_missing_file_is_reported_not_raised(auth_client):
    body = auth_client.post("/replay/start",
                            json={"mode": "file", "file": "no/such/file.jsonl"}).json()
    assert body["ok"] is False
    assert "No such file" in body["error"]


def test_replaying_an_unsupported_format_is_refused(auth_client, tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("not telemetry")

    body = auth_client.post("/replay/start",
                            json={"mode": "file", "file": str(bad)}).json()
    assert body["ok"] is False
    assert ".jsonl or .csv" in body["error"]


def test_replay_will_not_hijack_an_existing_connection(auth_client, fake_serial):
    body = auth_client.post("/replay/start", json={"mode": "simulate"}).json()
    assert body["ok"] is False
    assert "disconnect first" in body["error"]


def test_a_replayed_run_can_be_recorded_to_disk(auth_client, data_dir, stop_reader):
    auth_client.post("/replay/start", json={"mode": "simulate", "rate_hz": 50})
    assert wait_for(lambda: len(main.history) >= 3)

    auth_client.post("/recording/start", json={"test_name": "Simulated sweep"})
    assert wait_for(lambda: main.session_recorder.frame_count >= 10, timeout=6)
    res = auth_client.post("/recording/stop")

    assert res.status_code == 200
    csv_file = next((data_dir / "CSV").glob("Simulated-sweep_*.csv"))
    assert len(csv_file.read_text(encoding="utf-8-sig").splitlines()) >= 10
