# Where the console writes the operator's data:
#
#     <Documents>/NWA Bench Console/
#         CSV/      one .csv + .meta.json per recording
#         Login/    login.json — operator accounts
#         Logs/     append-only audit trails
#         Recovery/ per-session JSONL, for replaying a crashed run
#
# Outside the repo deliberately: an operator should find their data
# without knowing where the code is checked out, and a `git clean` must
# never be able to delete a test log.

from __future__ import annotations

import os
import sys
from pathlib import Path

from constants import (
    APP_FOLDER_NAME, DATA_DIR_ENV,
    DIR_CSV, DIR_LOGIN, DIR_LOGS, DIR_RECOVERY, LOGIN_FILENAME,
)

MAX_NAME_LEN = 60                     # Well inside every filesystem's limit


def _windows_documents_dir() -> Path | None:
    """Ask Windows itself where Documents is.

    Path.home()/"Documents" is wrong on a large share of real machines:
    with OneDrive Backup on, Documents is redirected to
    %USERPROFILE%/OneDrive/Documents and the literal ~/Documents is
    either missing or a stale empty folder — recordings written there
    would silently not be where the operator looks for them.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        FOLDERID_DOCUMENTS = ctypes.create_unicode_buffer(
            "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"
        )

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_byte * 8),
            ]

        guid = GUID()
        if ctypes.windll.ole32.CLSIDFromString(FOLDERID_DOCUMENTS, ctypes.byref(guid)) != 0:
            return None

        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(out)
        ) != 0:
            return None
        try:
            return Path(out.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:
        return None


def documents_dir() -> Path:
    """The user's Documents folder, however this OS names it."""
    win = _windows_documents_dir()
    if win is not None:
        return win
    return Path.home() / "Documents"


def app_dir() -> Path:
    """<Documents>/NWA Bench Console — created on first use.

    Read fresh on every call rather than cached at import: the tests set
    the override after main.py has already been imported.
    """
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override)
    return documents_dir() / APP_FOLDER_NAME


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def csv_dir() -> Path:
    return _ensure(app_dir() / DIR_CSV)


def login_dir() -> Path:
    return _ensure(app_dir() / DIR_LOGIN)


def logs_dir() -> Path:
    return _ensure(app_dir() / DIR_LOGS)


def recovery_dir() -> Path:
    return _ensure(app_dir() / DIR_RECOVERY)


def login_file() -> Path:
    return login_dir() / LOGIN_FILENAME


def safe_name(text: str, fallback: str = "session") -> str:
    """Reduce operator-supplied text to something safe as a filename.

    Test names come from a free-text box, so this has to survive
    "Prop 18x6 CW / run #2" — path separators included — without
    escaping the folder it's meant to write into.
    """
    cleaned = "".join(c if (c.isalnum() or c in " -_.") else "-" for c in (text or ""))
    cleaned = "-".join(cleaned.split())                 # Collapse whitespace runs into dashes
    cleaned = cleaned.strip(" .-")[:MAX_NAME_LEN]       # Trailing dots/spaces are invalid on Windows
    return cleaned or fallback
