#  Recording pipeline — disk streaming, crash recovery, and the
#  calibration snapshot kept alongside each run.
#  The property that matters most here: a recording is on disk WHILE
#  it happens, not only when the operator presses Stop. Before this,
#  a backend crash meant the entire run was gone.

import csv
import io
import json
import re
import time

import pytest

import main
import recorder


def make_frame(i: int = 0, **overrides) -> dict:
    frame = {
        "ts": time.time() + i * 0.05,
        "torque": 1.2345 + i * 0.01, "thrust": 45.6 + i,
        "s1": 1.0, "s2": -0.4, "s3": 0.9, "s4": 0.03,
        "stable": True, "dir": "CW",
        "rpm": 12450.0 + i, "rpm_raw": 12470.0 + i, "throttle_us": 1300,
        "current_a": 1.85, "voltage_v": 22.1,
        "current_a2": 0.0, "voltage_v2": 0.0,
    }
    frame.update(overrides)
    return frame


def record_frames(client, n: int = 5, test_name: str = "Prop 18x6 CW"):
    started = client.post("/recording/start", json={"test_name": test_name}).json()
    for i in range(n):
        main.session_recorder.write(make_frame(i))
    return started


def test_recording_writes_a_csv_under_the_documents_folder(auth_client, data_dir):
    started = record_frames(auth_client)
    auth_client.post("/recording/stop")

    csv_files = list((data_dir / "CSV").glob("*.csv"))
    assert len(csv_files) == 1
    assert csv_files[0].name.startswith("Prop-18x6-CW_")
    assert started["csv_path"].endswith(csv_files[0].name)


def test_csv_contains_a_row_per_frame_with_the_documented_columns(auth_client, data_dir):
    record_frames(auth_client, n=5)
    auth_client.post("/recording/stop")

    path = next((data_dir / "CSV").glob("*.csv"))
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))

    assert len(rows) == 5
    assert list(rows[0].keys()) == [label for label, _ in recorder.COLUMNS]
    assert rows[0]["Thrust (N)"] == "45.6"
    assert rows[0]["Stable"] == "Yes"
    assert rows[0]["Direction"] == "CW"


def test_csv_columns_are_the_ones_the_analysis_tab_can_auto_detect(auth_client, data_dir):
    """A recorded run must be loadable straight back into Tab 2.

    analysis.py finds its columns by substring, so the headers have to
    contain 'rpm', 'torque' and 'thrust'.
    """
    record_frames(auth_client)
    auth_client.post("/recording/stop")

    path = next((data_dir / "CSV").glob("*.csv"))
    header = path.read_text(encoding="utf-8-sig").splitlines()[0].lower()
    for keyword in ("rpm", "torque", "thrust"):
        assert keyword in header


def test_a_test_name_with_path_separators_cannot_escape_the_folder(auth_client, data_dir):
    record_frames(auth_client, test_name="../../evil / run #2")
    auth_client.post("/recording/stop")

    csv_files = list((data_dir / "CSV").glob("*.csv"))
    assert len(csv_files) == 1
    assert ".." not in csv_files[0].name
    assert csv_files[0].parent == data_dir / "CSV"


def test_an_unnamed_run_still_gets_a_file(auth_client, data_dir):
    record_frames(auth_client, test_name="")
    auth_client.post("/recording/stop")
    assert len(list((data_dir / "CSV").glob("*.csv"))) == 1


def test_frames_reach_the_recovery_log_before_the_recording_is_stopped(auth_client, data_dir):
    """The whole point: data survives a process that never reaches Stop."""
    record_frames(auth_client, n=40)
    main.session_recorder._flush()          # What the 1 s flush budget does on its own

    recovery = next((data_dir / "Recovery").glob("*.jsonl"))
    lines = [l for l in recovery.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) >= 30, "frames should already be on disk mid-run"
    assert json.loads(lines[0])["thrust"] == 45.6


def test_recovery_log_is_replayable_json_lines(auth_client, data_dir):
    record_frames(auth_client, n=6)
    auth_client.post("/recording/stop")

    recovery = next((data_dir / "Recovery").glob("*.jsonl"))
    frames = recorder.__dict__ and [json.loads(l) for l in
                                    recovery.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(frames) == 6
    assert all("torque" in f and "ts" in f for f in frames)


def test_run_records_the_calibration_that_was_in_force(auth_client, data_dir):
    main.calibration["values"] = {"thrust_scale": 18178.5, "cal_kg_s1": 7700.0}
    main.calibration["defaults"] = {"thrust_scale": 18000.0, "cal_kg_s1": 7650.0}

    record_frames(auth_client, n=3)
    # Changing the calibration AFTER the run must not rewrite history.
    main.calibration["values"]["thrust_scale"] = 99999.0
    auth_client.post("/recording/stop")

    meta = json.loads(next((data_dir / "CSV").glob("*.meta.json")).read_text())
    assert meta["calibration"]["thrust_scale"] == 18178.5, \
        "run must record the calibration it actually used"
    assert 99999.0 not in meta["calibration"].values(), \
        "a later edit must not be attributed to this run"


def test_meta_sidecar_records_operator_and_calibration(auth_client, data_dir):
    main.calibration["values"] = {"thrust_scale": 18178.5}
    record_frames(auth_client, n=2)
    auth_client.post("/recording/stop")

    meta = json.loads(next((data_dir / "CSV").glob("*.meta.json")).read_text())
    assert meta["operator"] == "testop"
    assert meta["test_name"] == "Prop 18x6 CW"
    assert meta["frame_count"] == 2
    assert meta["calibration"]["thrust_scale"] == 18178.5
    assert meta["finished_at"] is not None


def test_run_filename_carries_the_test_name_and_a_timestamp(auth_client, data_dir):
    record_frames(auth_client, test_name="Prop 20x8 CCW")
    auth_client.post("/recording/stop")

    name = next((data_dir / "CSV").glob("*.csv")).name
    assert name.startswith("Prop-20x8-CCW_")
    assert re.fullmatch(r"Prop-20x8-CCW_\d{8}_\d{6}\.csv", name), name


def test_stop_returns_the_file_locations_not_a_download(auth_client):
    """Stop hands back JSON, not an .xlsx.

    The run is already on disk in the operator's Documents folder, so a
    second copy through the browser only created a file to be filed by
    hand and made Downloads a competing home for test data. The paths
    are the confirmation instead.
    """
    record_frames(auth_client, n=4)
    res = auth_client.post("/recording/stop")

    assert res.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in res.headers

    body = res.json()
    assert body["ok"] is True
    assert body["recording"] is False
    assert body["frame_count"] == 4
    assert body["csv_path"].endswith(".csv")
    assert body["recovery_path"].endswith(".jsonl")


def test_recording_status_reports_where_the_run_is_being_written(auth_client):
    record_frames(auth_client, n=3)
    status = auth_client.get("/recording/status").json()

    assert status["recording"] is True
    assert status["frame_count"] == 3
    assert status["test_name"] == "Prop 18x6 CW"
    assert status["csv_path"].endswith(".csv")


def test_finished_runs_are_listed(auth_client):
    record_frames(auth_client, n=2, test_name="Run A")
    auth_client.post("/recording/stop")

    listed = auth_client.get("/recordings").json()
    assert listed["ok"] is True
    assert any(r["test_name"] == "Run A" and r["operator"] == "testop"
               for r in listed["recordings"])


def test_starting_a_new_recording_opens_a_fresh_file(auth_client, data_dir):
    record_frames(auth_client, n=3, test_name="First")
    auth_client.post("/recording/stop")
    started = auth_client.post("/recording/start", json={"test_name": "Second"}).json()

    assert main.session_recorder.frame_count == 0
    assert "Second_" in started["csv_path"]
    assert len(list((data_dir / "CSV").glob("*.csv"))) == 2, "the first run's file must survive"
