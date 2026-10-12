"""Settings → Firmware: esp32.zip built from the repository archive.

The GitHub fetch is replaced in every test — the suite must never touch
the network.
"""

import io
import zipfile

import pytest

import firmware_source


def _github_archive(files: dict[str, str], wrapper="P10-NWA-Bench-Console-main") -> bytes:
    """A zip shaped like GitHub's: everything under one wrapper folder."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{wrapper}/", "")
        for name, body in files.items():
            z.writestr(f"{wrapper}/{name}", body)
    return buf.getvalue()


REPO_FILES = {
    "esp32/main.c": "int main;",
    "esp32/hx711.c": "// hx711",
    "esp32/hx711.h": "#pragma once",
    "esp32/CMakeLists.txt": "idf_component_register()",
    "backend/main.py": "print('not firmware')",
    "frontend/src/esp32.js": "// a name containing esp32, not the folder",
    "README.md": "# repo",
}


def _names(zip_bytes: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        return set(z.namelist())


def test_only_the_esp32_folder_is_kept_without_githubs_wrapper():
    out = firmware_source.extract_firmware_zip(_github_archive(REPO_FILES))
    assert _names(out) == {
        "esp32/main.c", "esp32/hx711.c", "esp32/hx711.h", "esp32/CMakeLists.txt",
    }
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        assert z.read("esp32/main.c") == b"int main;"


def test_a_branch_without_esp32_is_an_error_not_an_empty_zip():
    with pytest.raises(firmware_source.FirmwareSourceError, match="No esp32/"):
        firmware_source.extract_firmware_zip(_github_archive({"README.md": "x"}))


def test_a_non_zip_response_is_reported():
    with pytest.raises(firmware_source.FirmwareSourceError, match="isn't a zip"):
        firmware_source.extract_firmware_zip(b"<html>rate limited</html>")


def test_endpoint_returns_esp32_zip_as_an_attachment(auth_client, monkeypatch):
    monkeypatch.setattr(firmware_source, "fetch_repo_archive",
                        lambda: _github_archive(REPO_FILES))
    res = auth_client.get("/firmware/source")

    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    assert 'filename="esp32.zip"' in res.headers["content-disposition"]
    assert "esp32/main.c" in _names(res.content)


def test_endpoint_turns_an_unreachable_github_into_a_readable_502(auth_client, monkeypatch):
    def offline():
        raise firmware_source.FirmwareSourceError("Could not reach GitHub — check this PC's internet connection.")
    monkeypatch.setattr(firmware_source, "fetch_repo_archive", offline)

    res = auth_client.get("/firmware/source")
    assert res.status_code == 502
    assert "Could not reach GitHub" in res.json()["detail"]


def test_endpoint_requires_a_signed_in_operator(auth_client, monkeypatch):
    called = []
    monkeypatch.setattr(firmware_source, "fetch_repo_archive", lambda: called.append(1))
    res = auth_client.get("/firmware/source", headers={"Authorization": ""})
    assert res.status_code == 401
    assert not called
