"""Synthetic ESP32-S3 *app image* builder for OTA engine tests.

The bytes follow the real esp_image_header_t (24 bytes) + first segment
header (8 bytes) + esp_app_desc_t layout so the Agent's validator is
exercised against the same offsets it reads on real builds. These are NOT
bootable firmware and are never flashed; they only feed host tests.
"""
from __future__ import annotations

import hashlib
import io
import json
import struct
import zipfile

ESP32S3_CHIP_ID = 9
PARTITION_SHA = "1d9cca96de0fe07ad7fc0648b9878ddecd9ce565e38b589ad20fea698ed4c80c"
SLOT = 0x340000


def profile(pid, hardware_model="ES3C28P", partition_sha=PARTITION_SHA, slot=SLOT, chip="esp32s3", board=None):
    return {
        "profile_id": pid,
        "hardware_model": hardware_model,
        "chip": chip,
        "min_chip_revision": 0,
        "flash_size": "16MB",
        "psram": "8MB-OPI",
        "partition_table_sha256": partition_sha,
        "ota_slot_size": slot,
        "board": board or pid,
        "scanner_interface": "GM65-UART1-RX44",
        "fqbn": "esp32:esp32:esp32s3:FlashSize=16M,PartitionScheme=default_8MB,PSRAM=opi,CDCOnBoot=cdc",
    }


def app_image(version, build, profile_id, hardware_model="ES3C28P", chip_id=ESP32S3_CHIP_ID,
              app_magic=0xABCD5432, filler=4096, elf_seed=b"elf"):
    header = bytes([0xE9, 4, 2, 0x4F]) + struct.pack("<I", 0x40378000)
    ext = bytes([0xEE, 0, 0, 0]) + struct.pack("<H", chip_id) + bytes([0]) + struct.pack("<HH", 0, 99) + bytes(4) + bytes([1])
    assert len(header + ext) == 24
    seg = struct.pack("<II", 0x3C000020, 256)
    elf_sha = hashlib.sha256(elf_seed + version.encode() + profile_id.encode()).digest()
    desc = struct.pack("<II8x", app_magic, 0) + version.encode().ljust(32, b"\0") + b"esp".ljust(32, b"\0") \
        + b"00:00:00".ljust(16, b"\0") + b"Nov 11 2026".ljust(16, b"\0") + b"v5.5".ljust(32, b"\0") + elf_sha
    marker = f"MESFLOW_FW_IDENTITY:version={version};build={build};profile={profile_id};hw={hardware_model};end".encode()
    body = header + ext + seg + desc
    return body + bytes(64) + marker + bytes(filler), elf_sha.hex()


def release_zip(version, build, profiles, *, commit="c0ffee", tamper=None, extra_files=None, bins=None):
    """Build a schema-2 release package with one app.bin per profile."""
    out = io.BytesIO()
    artifacts = []
    with zipfile.ZipFile(out, "w") as z:
        for p in profiles:
            pid = p["profile_id"]
            data, elf = (bins or {}).get(pid) or app_image(version, build, pid, p["hardware_model"])
            manifest = {
                "schema": "mesflow-esp-ota-artifact/2",
                "artifact_type": "app",
                "version": version,
                "build_id": build,
                "commit": commit,
                "profile": p,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "app_elf_sha256": elf,
                "config_schema": 2,
                "bootloader_requirement": "esp32s3-rollback-enabled",
            }
            if tamper: tamper(pid, manifest)
            z.writestr(f"{pid}/app.bin", data)
            z.writestr(f"{pid}/manifest.json", json.dumps(manifest))
            artifacts.append({"profile_id": pid, "path": f"{pid}/app.bin", "manifest": f"{pid}/manifest.json"})
        z.writestr("release.json", json.dumps({
            "schema": "mesflow-esp-ota-release/2", "version": version, "build_id": build,
            "commit": commit, "artifacts": artifacts}))
        for name, content in (extra_files or {}).items():
            z.writestr(name, content)
    return io.BytesIO(out.getvalue())


def device(kid, *, online=True, profile_id="", version="5.6.2", model="ES3C28P", name=None, station="LASER-01"):
    return {"kiosk_id": kid, "kiosk_name": name or kid, "station_code": station, "hardware_model": model,
            "hardware_profile": profile_id, "firmware_version": version, "firmware_build": "",
            "online": online}
