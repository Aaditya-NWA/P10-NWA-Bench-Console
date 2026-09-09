#  Shared test fixtures for the NWA Bench Console backend.
#  Two things every test here depends on:
#  1. NOTHING TOUCHES THE OPERATOR'S REAL DATA. app_paths reads
#     NWA_BENCH_DATA_DIR on every call, and the env var is set below
#     before main is imported, so accounts, CSVs and audit logs all
#     land in a tmp directory. Without this the suite would create
#     accounts in the real login.json and litter Documents with
#     fixture runs.
#  2. NO ESP32 IS REQUIRED. replay.ReplaySerial (and FakeSerial here)
#     stand in for the port, so the real serial_reader_thread, health
#     counters, recording pipeline and command writers are all
#     exercised — the same code path production uses, not a parallel
#     one that could drift.

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Must be set before `import main` — the backend resolves paths lazily,
# but auth/recorder call into app_paths during the first request and a
# real Documents write would already have happened by then.
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="nwa-bench-tests-"))
os.environ["NWA_BENCH_DATA_DIR"] = str(_TMP_ROOT / "data")
os.environ["NWA_BENCH_ALLOW_REPLAY"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient    # noqa: E402

import app_paths                                # noqa: E402
import audit                                    # noqa: E402
import main                                     # noqa: E402


class FakeSerial:
    """Minimal stand-in for serial.Serial that records what was written.

    Used where a test needs to assert on the exact bytes sent to the
    ESC — the throttle safety cut, calibration lines — rather than on
    telemetry coming back.
    """

    def __init__(self, fail_writes: bool = False):
        self.is_open = True
        self.writes: list[bytes] = []
        self.fail_writes = fail_writes

    def write(self, data: bytes) -> int:
        if self.fail_writes:
            raise OSError("simulated cable yank")
        self.writes.append(data)
        return len(data)

    def readline(self) -> bytes:
        return b""

    def close(self) -> None:
        self.is_open = False

    # Convenience for assertions
    @property
    def throttle_commands(self) -> list[int]:
        out = []
        for w in self.writes:
            text = w.decode().strip()
            if text.startswith("S"):
                try:
                    out.append(int(text[1:]))
                except ValueError:
                    pass
        return out


@pytest.fixture(autouse=True)
def reset_backend_state():
    """Return main's module-level globals to a known state per test.

    The backend keeps deliberate module-level state (one bench, one
    serial link), so tests would otherwise leak into each other.
    """
    yield
    main.serial_conn = None
    main.recording = False
    main.last_throttle_us = main.THR_MIN
    main.history.clear()
    main.latest_data = {}
    main.profile_task = None
    main.profile_state.update(running=False, phase="idle", step_index=-1,
                              step_count=0, target_us=main.THR_MIN,
                              commanded_us=main.THR_MIN, hold_remaining_s=0.0,
                              steps=[], message="")
    main.profile_state.pop("_takeover", None)
    main.calibration["values"] = {}
    main.calibration["defaults"] = {}
    main.serial_status.update(connected=False, port="", error="")
    # Each TestClient's lifespan captures a loop that is closed when the
    # client exits. Leaving the stale reference behind makes a later
    # test's broadcast target a dead loop.
    main.main_loop = None
    main._reset_serial_health()
    audit._pending_throttle = None
    audit._last_logged_us = None
    if main.session_recorder.active:
        main.session_recorder.stop()


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    """Point the console's data folder at this test's own tmp_path."""
    target = tmp_path / "bench-data"
    monkeypatch.setenv("NWA_BENCH_DATA_DIR", str(target))
    return target


@pytest.fixture
def client(data_dir) -> TestClient:
    """A TestClient with no accounts yet — the console in setup mode."""
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def auth_client(client) -> TestClient:
    """A TestClient already signed in as 'testop'.

    The token is attached to the client's default headers so individual
    tests don't repeat it on every call.
    """
    res = client.post("/auth/setup",
                      json={"username": "testop", "password": "testpass123"})
    body = res.json()
    assert body["ok"], body
    client.headers.update({"Authorization": f"Bearer {body['token']}"})
    return client


@pytest.fixture
def fake_serial() -> FakeSerial:
    """A fake port already attached to the backend as the live link."""
    conn = FakeSerial()
    main.serial_conn = conn
    main.serial_status.update(connected=True, port="TEST", error="")
    return conn
