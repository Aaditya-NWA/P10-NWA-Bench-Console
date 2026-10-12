# Firmware source download — Settings → Firmware.
#
# Hands the operator the ESP32 source exactly as it stands on the
# repository's main branch, as esp32.zip. Nothing is flashed and nothing
# is built: this is a copy of the files, for whoever then builds them
# with ESP-IDF.
#
# The source is GitHub, not this install's own files, on purpose — a
# packaged desktop app doesn't ship esp32/ at all, and "the current
# firmware" should mean what the team has pushed, not whatever copy
# happens to be on one bench PC.
#
# The backend does the fetch rather than the browser: it keeps the
# repository address in one place and spares the webview a cross-origin
# download that GitHub's redirects make awkward.

from __future__ import annotations

import io
import urllib.error
import urllib.request
import zipfile

from constants import APP_VERSION

FIRMWARE_REPO   = "Aaditya-NWA/P10-NWA-Bench-Console"
FIRMWARE_BRANCH = "main"
FIRMWARE_DIR    = "esp32"
FIRMWARE_ZIP_NAME = "esp32.zip"

# github.com/<repo>/archive/... redirects here; asking codeload directly
# saves a round trip. The repository is public, so no token is needed.
ARCHIVE_URL = (
    f"https://codeload.github.com/{FIRMWARE_REPO}/zip/refs/heads/{FIRMWARE_BRANCH}"
)

FETCH_TIMEOUT_S = 30
# The whole repository archive is a few MB. The cap only stops a
# misbehaving server from filling memory; it is not a tuning knob.
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024


class FirmwareSourceError(Exception):
    """Raised with a message meant to be shown to the operator as-is."""


def fetch_repo_archive() -> bytes:
    """Download the main-branch archive. Separate so tests can replace it."""
    req = urllib.request.Request(
        ARCHIVE_URL,
        headers={"User-Agent": f"NWA-Bench-Console/{APP_VERSION}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
            data = resp.read(MAX_ARCHIVE_BYTES + 1)
    except urllib.error.HTTPError as e:
        # 404 is also what GitHub answers for a repository that has gone
        # private — say so, rather than implying the branch vanished.
        raise FirmwareSourceError(
            f"GitHub answered {e.code} for {FIRMWARE_REPO} ({FIRMWARE_BRANCH}). "
            "The repository may be private or renamed."
        ) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FirmwareSourceError(
            "Could not reach GitHub — check this PC's internet connection."
        ) from e

    if len(data) > MAX_ARCHIVE_BYTES:
        raise FirmwareSourceError("The repository archive is unexpectedly large.")
    return data


def extract_firmware_zip(archive: bytes) -> bytes:
    """Repack just the esp32/ folder of a GitHub archive as esp32.zip.

    GitHub wraps everything in a single "<repo>-<branch>/" folder; that
    wrapper is dropped so the zip opens to esp32/ directly, matching the
    repository's own layout.
    """
    try:
        src = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as e:
        raise FirmwareSourceError("GitHub returned something that isn't a zip archive.") from e

    out = io.BytesIO()
    count = 0
    with src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            # "<wrapper>/esp32/main.c" → "esp32/main.c"
            parts = info.filename.split("/", 1)
            if len(parts) != 2:
                continue
            rel = parts[1]
            if not rel.startswith(f"{FIRMWARE_DIR}/") or info.is_dir():
                continue
            # Names come from the network; never let one climb out of
            # the folder it will be extracted into.
            if ".." in rel.split("/") or rel.startswith("/"):
                continue
            dst.writestr(zipfile.ZipInfo(rel, date_time=info.date_time),
                         src.read(info), zipfile.ZIP_DEFLATED)
            count += 1

    if count == 0:
        raise FirmwareSourceError(
            f"No {FIRMWARE_DIR}/ folder on the {FIRMWARE_BRANCH} branch of {FIRMWARE_REPO}."
        )
    return out.getvalue()


def build_firmware_zip() -> bytes:
    return extract_firmware_zip(fetch_repo_archive())
