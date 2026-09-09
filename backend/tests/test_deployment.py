#  Guards for the desktop build (DEPLOYMENT.md).
#
#  Two things are pinned here, both of them duplication that already
#  exists elsewhere in this project and has already caused bugs once:
#
#  1. The version string, declared in five files. The updater compares
#     versions, so a drifted one means installed copies either never see
#     a release or reinstall one they already have — and neither is
#     visible from the dev machine. This is O5 in DEPLOYMENT.md §12.
#
#  2. The shell/backend contract: the port, the shutdown token's env var
#     and header. Same class of problem as THR_MIN/THR_MAX being spelled
#     out in three places; here a mismatch means the window closes
#     without the throttle ever being cut.

import json
import os
import re
from pathlib import Path

import pytest

import constants
import main

REPO = Path(__file__).resolve().parent.parent.parent
TAURI_CONF = REPO / "src-tauri" / "tauri.conf.json"
CARGO_TOML = REPO / "src-tauri" / "Cargo.toml"
MAIN_RS = REPO / "src-tauri" / "src" / "main.rs"

# The Rust shell is not built in CI for the Python suite, so these tests
# skip rather than fail when someone is working on the backend alone in a
# checkout that has no src-tauri yet.
needs_shell = pytest.mark.skipif(
    not TAURI_CONF.exists(), reason="src-tauri/ is not present in this checkout"
)


def _rust_const(name: str) -> str:
    """Pull a `const NAME: type = "value";` out of the shell's source."""
    text = MAIN_RS.read_text(encoding="utf-8")
    match = re.search(rf'const {name}:[^=]+=\s*"?([^";]+)"?;', text)
    assert match, f"{name} not found in src-tauri/src/main.rs"
    return match.group(1).strip()


# ── Version sync (O5) ─────────────────────────────────────────────

@needs_shell
def test_every_declared_version_matches():
    """All five version declarations agree.

    Listed explicitly rather than derived, because the point is to catch
    the one that was forgotten — a helper that reads them all from a
    single place would have nothing to disagree with.
    """
    versions = {
        "backend/constants.py": constants.APP_VERSION,
        "package.json": json.loads((REPO / "package.json").read_text())["version"],
        "frontend/package.json": json.loads(
            (REPO / "frontend" / "package.json").read_text())["version"],
        "src-tauri/tauri.conf.json": json.loads(TAURI_CONF.read_text())["version"],
    }

    cargo = re.search(r'^version\s*=\s*"([^"]+)"',
                      CARGO_TOML.read_text(encoding="utf-8"), re.M)
    assert cargo, "no version in src-tauri/Cargo.toml"
    versions["src-tauri/Cargo.toml"] = cargo.group(1)

    distinct = set(versions.values())
    assert len(distinct) == 1, (
        "version strings have drifted apart:\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(versions.items()))
    )


def test_version_is_semver():
    """The updater orders releases by semver; anything else won't compare."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", constants.APP_VERSION), constants.APP_VERSION


# ── The shell/backend contract ────────────────────────────────────

@needs_shell
def test_shell_and_backend_agree_on_the_port():
    assert _rust_const("BACKEND_PORT") == str(constants.DEFAULT_PORT)


@needs_shell
def test_shell_and_backend_agree_on_the_host():
    assert _rust_const("BACKEND_HOST") == constants.DEFAULT_HOST


@needs_shell
def test_shell_and_backend_agree_on_the_shutdown_token_names():
    """A mismatch here means the graceful stop silently 403s.

    The shell falls back to killing the process, which is precisely the
    path that skips the throttle cut — so this is a safety guard, not
    tidiness.
    """
    assert _rust_const("SHUTDOWN_TOKEN_ENV") == constants.SHUTDOWN_TOKEN_ENV
    assert _rust_const("SHUTDOWN_TOKEN_HEADER") == constants.SHUTDOWN_TOKEN_HEADER


@needs_shell
def test_updater_endpoint_and_pubkey_are_configured():
    """An unsigned or unpointed updater fails silently, by design (§6)."""
    conf = json.loads(TAURI_CONF.read_text())
    updater = conf["plugins"]["updater"]
    assert updater["pubkey"], "no updater public key — updates cannot be verified"
    assert updater["endpoints"], "no updater endpoint — nothing to check"
    assert all(e.startswith("https://") for e in updater["endpoints"]), \
        "update manifests must be fetched over HTTPS"


# ── Updates are on-demand only ────────────────────────────────────

@needs_shell
def test_the_shell_exposes_manual_update_commands():
    """The Updates panel drives these two; without them the tab is inert."""
    src = MAIN_RS.read_text(encoding="utf-8")
    for command in ("async fn updates_check", "async fn updates_install"):
        assert command in src, f"{command} missing from the shell"
    assert "generate_handler![updates_check, updates_install]" in src,         "the update commands are not registered with the invoke handler"


@needs_shell
def test_the_shell_never_checks_for_updates_on_its_own():
    """No timer, no launch check — the operator's click is the only trigger.

    A bench machine should make no unprompted network requests, and no
    update should be able to interrupt a run. A reintroduced background
    poll would undo both, and nothing else in the app would look
    different — the failure is entirely invisible from the UI.
    """
    src = MAIN_RS.read_text(encoding="utf-8")
    for banned in ("UPDATE_CHECK_INTERVAL", "FIRST_UPDATE_CHECK_DELAY",
                   "spawn_update_loop"):
        assert banned not in src, (
            f"{banned} is back in the shell — updates must only be checked "
            "when the operator asks (DEPLOYMENT.md §6)"
        )


@needs_shell
def test_the_updater_plugin_does_not_own_the_dialog():
    """The panel renders the prompt, so the plugin's own dialog stays off.

    With both enabled the operator gets a native popup *and* the in-app
    panel for the same update.
    """
    conf = json.loads(TAURI_CONF.read_text())
    assert conf["plugins"]["updater"].get("dialog") is False


@needs_shell
def test_install_is_still_gated_on_a_live_bench():
    """Manual triggering changed who starts an update, not when it is safe.

    An update replaces the process that owns the serial port and the
    throttle cut, so the pre-download and post-download checks both have
    to be there (§6).
    """
    src = MAIN_RS.read_text(encoding="utf-8")
    install = src.split("async fn updates_install")[1].split("\nfn ")[0]
    assert install.count("busy_reason()") >= 2, (
        "updates_install must check the bench state before downloading AND "
        "again before installing — state can change while 40 MB downloads"
    )
    assert "stop_sidecar_gracefully" in install,         "the sidecar must stop through the handshake before the installer runs"


@needs_shell
def test_frozen_backend_is_not_committed():
    """The staged sidecar is ~120 MB of build output, regenerated on every build."""
    staged = REPO / "src-tauri" / "resources" / "backend"
    if not staged.exists():
        return
    import subprocess
    tracked = subprocess.run(
        ["git", "ls-files", "src-tauri/resources/backend"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert not tracked.stdout.strip(), \
        "the frozen backend is being tracked by git — check .gitignore"


# ── /health ───────────────────────────────────────────────────────

def test_health_needs_no_session(client):
    """The shell polls this before any account may even exist."""
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["version"] == constants.APP_VERSION


def test_health_reports_the_state_the_updater_gates_on(client):
    """serial/profile/recording drive the "never mid-test" rule (§6)."""
    body = client.get("/health").json()
    for key in ("serial_connected", "profile_running", "recording"):
        assert key in body, f"/health must report {key}"
        assert isinstance(body[key], bool)


def test_health_reports_a_running_profile(auth_client, fake_serial):
    """A profile in flight must be visible, or an update could land mid-run."""
    res = auth_client.post("/profile/start", json={
        "steps": [{"us": 1200, "hold_s": 30}],
        "ramp_rate_us_per_s": 10,
    })
    assert res.json()["ok"], res.json()
    try:
        assert auth_client.get("/health").json()["profile_running"] is True
    finally:
        auth_client.post("/profile/stop")


# ── /shutdown ─────────────────────────────────────────────────────

def test_shutdown_is_inert_without_a_token(client, monkeypatch):
    """Under `uvicorn main:app` nothing should be able to stop the server.

    run_backend.py is what sets request_shutdown, so a dev server leaves
    it None and the route reports 404 — no endpoint, rather than a
    refused one.
    """
    monkeypatch.delenv(constants.SHUTDOWN_TOKEN_ENV, raising=False)
    monkeypatch.setattr(main, "request_shutdown", None)
    assert client.post("/shutdown").status_code == 404


def test_shutdown_refuses_a_wrong_token(client, monkeypatch):
    called = []
    monkeypatch.setenv(constants.SHUTDOWN_TOKEN_ENV, "the-real-token")
    monkeypatch.setattr(main, "request_shutdown", lambda: called.append(True))

    res = client.post("/shutdown",
                      headers={constants.SHUTDOWN_TOKEN_HEADER: "not-the-token"})
    assert res.status_code == 403
    assert not called, "a bad token must not stop the server"


def test_shutdown_refuses_a_missing_token(client, monkeypatch):
    called = []
    monkeypatch.setenv(constants.SHUTDOWN_TOKEN_ENV, "the-real-token")
    monkeypatch.setattr(main, "request_shutdown", lambda: called.append(True))

    assert client.post("/shutdown").status_code == 403
    assert not called


def test_shutdown_accepts_the_right_token(client, monkeypatch):
    called = []
    monkeypatch.setenv(constants.SHUTDOWN_TOKEN_ENV, "the-real-token")
    monkeypatch.setattr(main, "request_shutdown", lambda: called.append(True))

    res = client.post("/shutdown",
                      headers={constants.SHUTDOWN_TOKEN_HEADER: "the-real-token"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert called == [True], "the correct token must trigger the graceful stop"


def test_shutdown_needs_no_operator_session(client, monkeypatch):
    """The shell has no account and cannot get one — the token is the gate."""
    monkeypatch.setenv(constants.SHUTDOWN_TOKEN_ENV, "tok")
    monkeypatch.setattr(main, "request_shutdown", lambda: None)
    res = client.post("/shutdown", headers={constants.SHUTDOWN_TOKEN_HEADER: "tok"})
    assert res.status_code == 200


# ── The sidecar entry point ───────────────────────────────────────

def test_run_backend_defaults_to_loopback():
    """Binding 0.0.0.0 would expose the throttle API to the local network."""
    import run_backend
    args = run_backend.parse_args([])
    assert args.host == constants.DEFAULT_HOST == "127.0.0.1"
    assert args.port == constants.DEFAULT_PORT


def test_run_backend_wires_the_shutdown_hook():
    """build_server is what makes /shutdown live; without it the route 404s."""
    import run_backend
    main.request_shutdown = None
    server = run_backend.build_server("127.0.0.1", 8999)
    assert main.request_shutdown is not None
    assert server.config.host == "127.0.0.1"

    main.request_shutdown()
    assert server.should_exit is True, "the hook must ask uvicorn to stop"
    main.request_shutdown = None


def test_port_in_use_is_detected(monkeypatch):
    """Reported as a sentence, not a stack trace nobody can see (§8)."""
    import socket
    import run_backend

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        assert run_backend._port_is_free("127.0.0.1", port) is False
        assert run_backend.run(["--port", str(port)]) == run_backend.EXIT_PORT_IN_USE
