import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("WORKSHOP_AGENT_HOME", "/tmp/mesflow-ota-package-test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ota_control as ota


def _package(binary_name="esp-kiosk-5.5.7.bin", merged=False):
    manifest_name = "esp-kiosk-5.5.7.manifest.json"
    manifest = {
        "version": "5.5.7",
        "build": "20260813.0015",
        "hardware_model": "ES3C28P",
        "filename": binary_name,
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(binary_name, b"firmware-bytes")
        z.writestr(manifest_name, json.dumps(manifest))
    return io.BytesIO(out.getvalue())


def test_ota_package_extracts_manifest_and_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(ota, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(ota, "MAX_APP_SIZE", 1024)
    item = ota._save_firmware_package(_package(), "release.ota.zip")
    assert item["version"] == "5.5.7"
    assert item["hardware_model"] == "ES3C28P"
    assert item["size"] == len(b"firmware-bytes")
    assert item["sha256"]


def test_ota_package_rejects_merged_image(tmp_path, monkeypatch):
    monkeypatch.setattr(ota, "ARTIFACT_ROOT", tmp_path)
    with pytest.raises(ValueError, match="OTA_PACKAGE_CONTENT_INVALID"):
        ota._save_firmware_package(_package("esp-kiosk-5.5.7.merged.bin"), "release.ota.zip")
