# Operator accounts & sessions. The purpose is accountability, not
# secrecy: every throttle command and calibration write is attributed to
# a named operator in the audit log. It is not a defence against someone
# who already has the bench PC — it's a defence against "nobody knows
# who ran that sweep" and a passer-by nudging the slider.
#
# Accounts live in Login/login.json, outside the repo, so a fresh
# checkout never ships a password. Passwords are PBKDF2-HMAC-SHA256
# with a per-user random salt; no plaintext is ever written.
#
# Sessions are in-memory only — restarting the backend logs everyone
# out, the safe default for a shared bench machine.

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, WebSocket

from app_paths import login_file
from constants import (
    ERR_BAD_CREDENTIALS, ERR_NOT_SIGNED_IN, FILE_ENCODING, HTTP_UNAUTHORIZED,
    LOG_AUTH, MAX_USERNAME_LEN, MIN_PASSWORD_LEN, PBKDF2_HASH_NAME,
    ROLE_ADMIN, ROLE_OPERATOR, SETUP_USER,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# 200k iterations is roughly 100 ms on the kind of mini-PC these benches
# run — a sensible ceiling for a login a human waits on, while still
# being expensive to grind offline.
PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16
SESSION_TOKEN_BYTES = 32

# Generous on purpose: a test session can legitimately sit idle while
# the operator changes a prop. It exists so an unattended console
# doesn't stay signed in forever, not to force re-auth mid-run.
SESSION_IDLE_TIMEOUT_S = 12 * 3600

STORE_VERSION = 1
BEARER_SCHEME = "bearer"
WS_TOKEN_PARAM = "token"

# token -> {"username": str, "created": float, "last_seen": float}
_sessions: dict[str, dict] = {}


def _empty_store() -> dict:
    return {"version": STORE_VERSION, "users": []}


def load_store() -> dict:
    """Read login.json, tolerating a missing or corrupt file."""
    path = login_file()
    if not path.exists():
        return _empty_store()
    try:
        with path.open("r", encoding=FILE_ENCODING) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        # A corrupt file must not lock the operator out permanently, but
        # silently replacing it would delete their accounts. Move it
        # aside so it can be inspected, and start clean.
        print(f"{LOG_AUTH} login.json unreadable ({e}) — starting a fresh store")
        try:
            path.rename(path.with_suffix(f".corrupt-{int(time.time())}.json"))
        except OSError:
            pass
        return _empty_store()

    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        return _empty_store()
    return data


def save_store(store: dict) -> None:
    """Write login.json atomically so a crash mid-write can't truncate it."""
    path = login_file()
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding=FILE_ENCODING) as fh:
        json.dump(store, fh, indent=2)
    tmp.replace(path)


def has_any_user() -> bool:
    return bool(load_store()["users"])


def _hash_password(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac(
        PBKDF2_HASH_NAME, password.encode(FILE_ENCODING), salt, iterations
    ).hex()


def _make_user(username: str, password: str, role: str = ROLE_OPERATOR) -> dict:
    salt = secrets.token_bytes(SALT_BYTES)
    return {
        "username": username,
        "role": role,
        "salt": salt.hex(),
        "hash": _hash_password(password, salt),
        "iterations": PBKDF2_ITERATIONS,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def _verify(user: dict, password: str) -> bool:
    try:
        salt = bytes.fromhex(user["salt"])
        expected = user["hash"]
        iterations = int(user.get("iterations", PBKDF2_ITERATIONS))
    except (KeyError, ValueError):
        return False
    # compare_digest, not ==, so a wrong password can't be narrowed down
    # by timing how far the comparison got.
    return secrets.compare_digest(_hash_password(password, salt, iterations), expected)


def _find_user(store: dict, username: str) -> dict | None:
    target = username.strip().lower()
    for u in store["users"]:
        if u.get("username", "").lower() == target:
            return u
    return None


def _validate_credentials(username: str, password: str) -> str | None:
    """Return an error message, or None if the pair is acceptable."""
    if not username or not username.strip():
        return "Username is required"
    if len(username.strip()) > MAX_USERNAME_LEN:
        return f"Username must be {MAX_USERNAME_LEN} characters or fewer"
    if len(password or "") < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters"
    return None


def _prune_sessions() -> None:
    now = time.time()
    for token, s in list(_sessions.items()):
        if now - s["last_seen"] > SESSION_IDLE_TIMEOUT_S:
            del _sessions[token]


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    now = time.time()
    _sessions[token] = {"username": username, "created": now, "last_seen": now}
    return token


def session_user(token: str | None) -> str | None:
    """Resolve a token to a username, refreshing its idle timer."""
    if not token:
        return None
    _prune_sessions()
    s = _sessions.get(token)
    if s is None:
        return None
    s["last_seen"] = time.time()
    return s["username"]


def _token_from_header(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == BEARER_SCHEME:
        return parts[1].strip()
    return None


def require_auth(authorization: str | None = Header(default=None)) -> str:
    """Reject the request unless it carries a live session token.

    Returns the operator's username so a handler can attribute what it
    does to a person (the audit log relies on this).
    """
    # An empty store means the console has never been set up; locking
    # every endpoint then would leave no way to create the first account.
    if not has_any_user():
        return SETUP_USER

    user = session_user(_token_from_header(authorization))
    if user is None:
        raise HTTPException(status_code=HTTP_UNAUTHORIZED, detail=ERR_NOT_SIGNED_IN)
    return user


def websocket_user(ws: WebSocket) -> str | None:
    """Resolve the operator for a WebSocket handshake.

    Browsers can't set headers on a WebSocket, so the token rides in the
    query string instead: ws://host/ws?token=...
    """
    if not has_any_user():
        return SETUP_USER
    return session_user(ws.query_params.get(WS_TOKEN_PARAM))


@router.get("/status")
def auth_status(authorization: str | None = Header(default=None)):
    """Whether the console needs first-run setup, and who is signed in.

    Deliberately open: the login screen calls this before it has a
    token, and it reveals nothing beyond whether an account exists.
    """
    user = session_user(_token_from_header(authorization))
    return {
        "setup_required": not has_any_user(),
        "authenticated": user is not None,
        "username": user,
        "login_file": str(login_file()),
    }


@router.post("/setup")
def auth_setup(payload: dict):
    """Create the very first account. Only works while none exists."""
    if has_any_user():
        return {"ok": False, "error": "An account already exists — sign in instead"}

    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    error = _validate_credentials(username, password)
    if error:
        return {"ok": False, "error": error}

    store = _empty_store()
    store["users"].append(_make_user(username, password, role=ROLE_ADMIN))
    try:
        save_store(store)
    except OSError as e:
        return {"ok": False, "error": f"Could not write login.json: {e}"}

    token = create_session(username)
    print(f"{LOG_AUTH} First account created: {username}")
    return {"ok": True, "token": token, "username": username, "role": ROLE_ADMIN}


@router.post("/login")
def auth_login(payload: dict):
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))

    store = load_store()
    user = _find_user(store, username)
    if user is None or not _verify(user, password):
        return {"ok": False, "error": ERR_BAD_CREDENTIALS}

    token = create_session(user["username"])
    print(f"{LOG_AUTH} Signed in: {user['username']}")
    return {
        "ok": True,
        "token": token,
        "username": user["username"],
        "role": user.get("role", ROLE_OPERATOR),
    }


@router.post("/logout")
def auth_logout(authorization: str | None = Header(default=None)):
    token = _token_from_header(authorization)
    if token:
        _sessions.pop(token, None)
    return {"ok": True}


@router.get("/me")
def auth_me(authorization: str | None = Header(default=None)):
    user = session_user(_token_from_header(authorization))
    if user is None:
        raise HTTPException(status_code=HTTP_UNAUTHORIZED, detail=ERR_NOT_SIGNED_IN)
    store = load_store()
    record = _find_user(store, user)
    return {
        "ok": True,
        "username": user,
        "role": (record or {}).get("role", ROLE_OPERATOR),
    }


@router.post("/users")
def auth_add_user(payload: dict, actor: str = Header(default=None, alias="Authorization")):
    """Add another operator account. Requires an existing signed-in user."""
    current = session_user(_token_from_header(actor))
    if current is None:
        raise HTTPException(status_code=HTTP_UNAUTHORIZED, detail=ERR_NOT_SIGNED_IN)

    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    error = _validate_credentials(username, password)
    if error:
        return {"ok": False, "error": error}

    store = load_store()
    if _find_user(store, username) is not None:
        return {"ok": False, "error": f"'{username}' already exists"}

    store["users"].append(_make_user(username, password))
    try:
        save_store(store)
    except OSError as e:
        return {"ok": False, "error": f"Could not write login.json: {e}"}
    print(f"{LOG_AUTH} Account created by {current}: {username}")
    return {"ok": True, "username": username}


@router.post("/password")
def auth_change_password(payload: dict, authorization: str | None = Header(default=None)):
    """Change the signed-in operator's own password."""
    current = session_user(_token_from_header(authorization))
    if current is None:
        raise HTTPException(status_code=HTTP_UNAUTHORIZED, detail=ERR_NOT_SIGNED_IN)

    old = str(payload.get("current_password", ""))
    new = str(payload.get("new_password", ""))
    if len(new) < MIN_PASSWORD_LEN:
        return {"ok": False,
                "error": f"New password must be at least {MIN_PASSWORD_LEN} characters"}

    store = load_store()
    user = _find_user(store, current)
    if user is None or not _verify(user, old):
        return {"ok": False, "error": "Current password is incorrect"}

    updated = _make_user(user["username"], new, role=user.get("role", ROLE_OPERATOR))
    updated["created_at"] = user.get("created_at", updated["created_at"])
    store["users"] = [updated if u is user else u for u in store["users"]]
    try:
        save_store(store)
    except OSError as e:
        return {"ok": False, "error": f"Could not write login.json: {e}"}
    return {"ok": True}


@router.get("/users")
def auth_list_users(authorization: str | None = Header(default=None)):
    """List account names (never hashes) so the UI can show who exists."""
    if session_user(_token_from_header(authorization)) is None:
        raise HTTPException(status_code=HTTP_UNAUTHORIZED, detail=ERR_NOT_SIGNED_IN)
    store = load_store()
    return {
        "ok": True,
        "users": [
            {"username": u.get("username"), "role": u.get("role", ROLE_OPERATOR),
             "created_at": u.get("created_at")}
            for u in store["users"]
        ],
    }
