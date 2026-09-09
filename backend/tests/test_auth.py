#  Operator accounts, sessions, and endpoint protection.
#  The account system exists for attribution, not secrecy — but an
#  endpoint that forgets its Depends(require_auth) would silently let
#  an unattributed throttle command through, which defeats the point
#  of the audit log entirely. The coverage test at the bottom is the
#  guard against that.

import json

import pytest

import auth
import main


def test_console_reports_setup_required_when_no_account_exists(client):
    body = client.get("/auth/status").json()
    assert body["setup_required"] is True
    assert body["authenticated"] is False


def test_setup_creates_the_first_account_and_signs_in(client, data_dir):
    body = client.post("/auth/setup",
                       json={"username": "aditya", "password": "benchpass"}).json()
    assert body["ok"] is True
    assert body["username"] == "aditya"
    assert body["role"] == "admin"
    assert len(body["token"]) > 20

    stored = json.loads((data_dir / "Login" / "login.json").read_text())
    assert stored["users"][0]["username"] == "aditya"


def test_password_is_never_stored_in_plaintext(client, data_dir):
    client.post("/auth/setup", json={"username": "aditya", "password": "hunter2secret"})
    raw = (data_dir / "Login" / "login.json").read_text()

    assert "hunter2secret" not in raw
    record = json.loads(raw)["users"][0]
    assert record["hash"] != "hunter2secret"
    assert len(record["salt"]) == auth.SALT_BYTES * 2      # hex-encoded
    assert record["iterations"] >= 100_000


def test_setup_refuses_once_an_account_exists(auth_client):
    body = auth_client.post("/auth/setup",
                            json={"username": "someone", "password": "another"}).json()
    assert body["ok"] is False
    assert "already exists" in body["error"]


@pytest.mark.parametrize("username, password, expected", [
    ("",      "longenough", "Username is required"),
    ("  ",    "longenough", "Username is required"),
    ("valid", "short",      "at least 6"),
])
def test_setup_rejects_bad_credentials(client, username, password, expected):
    body = client.post("/auth/setup",
                       json={"username": username, "password": password}).json()
    assert body["ok"] is False
    assert expected in body["error"]


def test_correct_password_signs_in(auth_client):
    body = auth_client.post("/auth/login",
                            json={"username": "testop", "password": "testpass123"}).json()
    assert body["ok"] is True
    assert body["username"] == "testop"


def test_wrong_password_is_refused(auth_client):
    body = auth_client.post("/auth/login",
                            json={"username": "testop", "password": "wrong"}).json()
    assert body["ok"] is False


def test_unknown_and_wrong_password_give_the_same_message(auth_client):
    """Not leaking which usernames exist to an unauthenticated caller."""
    wrong_pw = auth_client.post("/auth/login",
                                json={"username": "testop", "password": "wrong"}).json()
    no_user = auth_client.post("/auth/login",
                               json={"username": "ghost", "password": "wrong"}).json()
    assert wrong_pw["error"] == no_user["error"]


def test_username_match_is_case_insensitive(auth_client):
    body = auth_client.post("/auth/login",
                            json={"username": "TESTOP", "password": "testpass123"}).json()
    assert body["ok"] is True


def test_signing_out_invalidates_the_token(auth_client):
    assert auth_client.get("/status").status_code == 200
    auth_client.post("/auth/logout")
    assert auth_client.get("/status").status_code == 401


def test_protected_endpoints_reject_a_missing_token(auth_client):
    no_token = {"Authorization": ""}
    for path in ("/status", "/history", "/ports", "/calibration", "/profile/status",
                 "/recording/status", "/recordings", "/paths"):
        res = auth_client.get(path, headers=no_token)
        assert res.status_code == 401, f"{path} answered {res.status_code} without a token"


def test_protected_endpoints_reject_a_bogus_token(auth_client):
    bad = {"Authorization": "Bearer not-a-real-token"}
    assert auth_client.get("/status", headers=bad).status_code == 401


def test_analysis_endpoints_are_protected_too(auth_client):
    """Uploaded rig logs are the operator's test data, not public."""
    res = auth_client.post("/analysis/run", headers={"Authorization": ""})
    assert res.status_code == 401


def test_every_mutating_route_declares_an_auth_dependency():
    """Coverage guard: a new endpoint must not be added unprotected.

    Checks the wiring rather than each behaviour, so an endpoint added
    next year is caught without anyone remembering to add a test.
    """
    exempt = {"/auth/status", "/auth/setup", "/auth/login", "/auth/logout",
              "/auth/me", "/auth/users", "/auth/password",
              "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
              # Called by the Tauri shell, which is not a signed-in
              # operator and has no way to become one — it polls /health
              # before any account may even exist, and asks /shutdown to
              # stop gracefully so the throttle cut still runs. /shutdown
              # is guarded by its own per-launch token instead; that
              # guard is pinned in test_deployment.py, so this exemption
              # is a different check, not an absent one.
              "/health", "/shutdown"}

    unprotected = []
    for route in main.app.routes:
        path = getattr(route, "path", None)
        if path is None or path in exempt:
            continue
        methods = getattr(route, "methods", set())
        if not methods or methods <= {"HEAD", "OPTIONS"}:
            continue
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        names = {d.call.__name__ for d in dependant.dependencies if d.call}
        if "require_auth" not in names:
            unprotected.append(f"{sorted(methods)} {path}")

    assert not unprotected, f"routes missing require_auth: {unprotected}"


def test_a_corrupt_login_file_does_not_lock_the_operator_out(client, data_dir):
    login_dir = data_dir / "Login"
    login_dir.mkdir(parents=True, exist_ok=True)
    (login_dir / "login.json").write_text("{ this is not valid json")

    # Falls back to setup mode with the bad file moved aside, rather
    # than raising and making the console unusable.
    body = client.get("/auth/status").json()
    assert body["setup_required"] is True
    assert list(login_dir.glob("*.corrupt-*.json")), "bad file should be preserved for inspection"


def test_additional_operators_can_be_added(auth_client):
    body = auth_client.post("/auth/users",
                            json={"username": "second", "password": "secondpass"}).json()
    assert body["ok"] is True

    listed = auth_client.get("/auth/users").json()
    assert {u["username"] for u in listed["users"]} == {"testop", "second"}

    signed_in = auth_client.post("/auth/login",
                                 json={"username": "second", "password": "secondpass"}).json()
    assert signed_in["ok"] is True


def test_duplicate_usernames_are_refused(auth_client):
    body = auth_client.post("/auth/users",
                            json={"username": "TestOp", "password": "whatever"}).json()
    assert body["ok"] is False
    assert "already exists" in body["error"]
