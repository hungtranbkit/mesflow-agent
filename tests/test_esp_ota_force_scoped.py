"""Contract tests for the scoped, profile-aware, force-capable ESP OTA engine.

Written before the engine change (spec section I). Every test drives the real
ota_control module against an isolated SQLite file; nothing here talks to a
device, and nothing here is a hardware test.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("WORKSHOP_AGENT_HOME", tempfile.mkdtemp(prefix="mesflow-ota-scoped-"))
from esp_ota_fixtures import app_image, device, profile, release_zip  # noqa: E402

A = "inst-aaaaaaaa-0000-4000-8000-000000000001"
B = "inst-bbbbbbbb-0000-4000-8000-000000000002"
M910 = profile("es3c28p-n16r8-m910-gm65uart")
KL = profile("es3c28p-n16r8-kiosklinux-gm65uart")
OTHER_PARTS = profile("es3c28p-n16r8-m910-bigapp", partition_sha="f" * 64, slot=0x600000)


@pytest.fixture()
def ota(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSHOP_AGENT_HOME", str(tmp_path))
    import ota_control
    mod = importlib.reload(ota_control)
    mod.init_ota_db()
    return mod


def _release(ota, version="5.6.3", build="b1", profiles=(M910, KL), **kw):
    return ota.save_release_package(release_zip(version, build, list(profiles), **kw), f"esp-kiosk-{version}.ota.zip")


def _bound(ota, instance=A, items=()):
    assert ota.ensure_instance(instance)["ok"]
    ota.sync_fleet(instance, list(items))


def _report(version="5.6.2", profile_id="", artifact="", model="ES3C28P", api=2):
    return {"current_version": version, "hardware_model": model, "hardware_profile": profile_id,
            "running_artifact_id": artifact, "api": api}


def _check(ota, kid, instance=A, **kw):
    ready = kw.pop("ready", True)
    return ota.device_check(instance, kid, _report(**kw), readiness={"ota_ready": ready, "reason": "READY" if ready else "DEVICE_BUSY"})


def _ev(ota, kid, event, offer=None, instance=A, **extra):
    body = {"status": event, **extra}
    if offer:
        body.setdefault("firmware_id", offer["artifact_id"])
        body.setdefault("to_version", offer["version"])
        body.setdefault("target_id", offer["target_id"])
        body.setdefault("claim_id", offer["claim_id"])
    return ota.record_device_event(instance, kid, body)


def _install(ota, kid, offer, instance=A):
    assert ota.target_for_download(instance, kid, offer["artifact_id"], offer["claim_id"])
    for e in ("OTA_DOWNLOAD_START", "OTA_DOWNLOAD_COMPLETE", "OTA_VERIFY_OK", "OTA_REBOOTING", "OTA_BOOT_NEW_VERSION", "OTA_HEALTHCHECK_OK"):
        _ev(ota, kid, e, offer, instance=instance, build_id=offer["build"], hardware_profile=offer["hardware_profile"])


def _target(ota, kid, instance=A):
    rows = ota.list_targets(instance, kiosk_id=kid)["items"]
    return rows[0] if rows else None


# ---------------------------------------------------------------- 1) scope
def test_instance_binding_fails_closed_on_mismatch(ota):
    assert ota.ensure_instance(A)["ok"]
    res = ota.ensure_instance(B)
    assert not res["ok"] and res["reason"] == "INSTANCE_MISMATCH"
    assert ota.ensure_instance(A)["ok"]


def test_instance_a_cannot_see_or_drive_instance_b_targets(ota):
    rel = _release(ota)
    # Same device id exists in two instances (DB copied / id collision).
    _bound(ota, A, [device("K1", profile_id=M910["profile_id"])])
    batch_a = ota.create_batch(A, rel["release_id"], force=True)
    assert batch_a["total"] == 1
    # Owner moves the Agent to another MESFlow (explicit rebind).
    ota.rebind_instance(B, actor="test")
    ota.sync_fleet(B, [device("K1", profile_id=M910["profile_id"])])
    # A's open target was cancelled by the rebind and is invisible from B.
    assert ota.list_targets(B)["total"] == 0
    assert ota.get_batch(B, batch_a["batch_id"]) is None
    assert ota.retry_targets(B, batch_id=batch_a["batch_id"]) == {"retried": 0}
    assert ota.cancel_batch(B, batch_a["batch_id"]) is None
    # B polling K1 gets nothing from A's batch.
    assert _check(ota, "K1", instance=B, profile_id=M910["profile_id"])["update_available"] is False
    # Guessing A's target id / artifact from B does not authorize a download.
    a_target = ota.list_targets(A)["items"][0]
    assert a_target["status"] == "CANCELLED" and a_target["error"] == "INSTANCE_REBOUND"
    assert ota.target_for_download(B, "K1", a_target["artifact_id"], a_target.get("claim_id") or "x") is None
    # Events with A's ids are ignored for B.
    assert _ev(ota, "K1", "OTA_HEALTHCHECK_OK", instance=B, target_id=a_target["id"], firmware_id=a_target["artifact_id"])["applied"] is False


def test_device_lists_are_instance_partitioned_even_with_duplicate_ids(ota):
    _bound(ota, A, [device("K1"), device("K2")])
    ota.sync_fleet(B, [device("K1", name="other-customer")])
    a = {d["kiosk_id"]: d for d in ota.list_fleet(A)["items"]}
    b = {d["kiosk_id"]: d for d in ota.list_fleet(B)["items"]}
    assert set(a) == {"K1", "K2"} and set(b) == {"K1"}
    assert b["K1"]["kiosk_name"] == "other-customer" and a["K1"]["kiosk_name"] == "K1"


def test_revoked_device_job_is_cancelled_and_not_claimable(ota):
    rel = _release(ota)
    _bound(ota, A, [device("K1", profile_id=M910["profile_id"]), device("K2", profile_id=M910["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    ota.sync_fleet(A, [device("K2", profile_id=M910["profile_id"])])  # K1 revoked/transferred in MESFlow
    assert _target(ota, "K1")["status"] == "CANCELLED"
    assert _check(ota, "K1", profile_id=M910["profile_id"])["update_available"] is False


# ---------------------------------------------------------------- 2) profile
def test_each_profile_gets_its_own_artifact(ota):
    rel = _release(ota)
    _bound(ota, A, [device("M", profile_id=M910["profile_id"]), device("L", profile_id=KL["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    om = _check(ota, "M", profile_id=M910["profile_id"])
    ol = _check(ota, "L", profile_id=KL["profile_id"])
    assert om["hardware_profile"] == M910["profile_id"] and ol["hardware_profile"] == KL["profile_id"]
    assert om["artifact_id"] != ol["artifact_id"] and om["sha256"] != ol["sha256"]


def test_unknown_and_conflicting_profile_are_blocked_never_latest(ota):
    _release(ota, version="5.6.1", build="b0", profiles=(KL,))  # KL is a known catalog profile
    rel = _release(ota, profiles=(M910,))
    _bound(ota, A, [device("U"), device("X", profile_id="es3c28p-unknown-board"), device("C", profile_id=M910["profile_id"])])
    with pytest.raises(ValueError, match="PROFILE_UNKNOWN"):
        ota.assign_profile(A, ["U"], "not-in-catalog")
    ota.assign_profile(A, ["C"], KL["profile_id"])  # admin assignment conflicts with device-reported profile
    prev = ota.preview_batch(A, rel["release_id"])
    reasons = {i["kiosk_id"]: i["blocked_reason"] for i in prev["items"]}
    assert reasons["U"] == "PROFILE_REQUIRED"
    assert reasons["X"] == "INCOMPATIBLE_FIRMWARE"
    assert reasons["C"] == "PROFILE_CONFLICT"
    batch = ota.create_batch(A, rel["release_id"], force=True)
    assert batch["counts"]["BLOCKED"] == 3
    for kid in ("U", "X", "C"):
        assert _check(ota, kid)["update_available"] is False


def test_same_chip_different_partition_is_a_different_profile(ota):
    rel = _release(ota, profiles=(M910, OTHER_PARTS))
    _bound(ota, A, [device("P1", profile_id=M910["profile_id"]), device("P2", profile_id=OTHER_PARTS["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    o1, o2 = _check(ota, "P1", profile_id=M910["profile_id"]), _check(ota, "P2", profile_id=OTHER_PARTS["profile_id"])
    assert o1["artifact_id"] != o2["artifact_id"]


def test_claim_rejects_device_reporting_a_different_profile_than_pinned(ota):
    rel = _release(ota)
    _bound(ota, A, [device("K", profile_id=M910["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    res = _check(ota, "K", profile_id=KL["profile_id"])
    assert res["update_available"] is False and res["reason"] == "PROFILE_MISMATCH"
    assert _target(ota, "K")["status"] == "BLOCKED"


@pytest.mark.parametrize("case,error", [
    ("merged", "OTA_PACKAGE_CONTENT_INVALID"),
    ("bootloader_magic", "OTA_IMAGE_NOT_APP"),
    ("wrong_chip", "OTA_IMAGE_CHIP_MISMATCH"),
    ("sha", "OTA_MANIFEST_SHA_MISMATCH"),
    ("identity", "OTA_IDENTITY_MISMATCH"),
    ("too_big", "OTA_IMAGE_TOO_LARGE"),
    ("not_app", "OTA_ARTIFACT_TYPE_INVALID"),
])
def test_upload_rejects_invalid_or_full_flash_artifacts(ota, case, error):
    kw = {}
    if case == "merged":
        kw["extra_files"] = {"es3c28p-n16r8-m910-gm65uart/esp.ino.merged.bin": b"x"}
    elif case == "bootloader_magic":
        kw["bins"] = {M910["profile_id"]: app_image("5.6.3", "b1", M910["profile_id"], app_magic=0x50)}
    elif case == "wrong_chip":
        kw["bins"] = {M910["profile_id"]: app_image("5.6.3", "b1", M910["profile_id"], chip_id=0)}
    elif case == "sha":
        kw["tamper"] = lambda pid, m: m.update(sha256="0" * 64)
    elif case == "identity":
        kw["bins"] = {M910["profile_id"]: app_image("5.6.3", "b1", KL["profile_id"])}
    elif case == "too_big":
        kw["bins"] = {M910["profile_id"]: app_image("5.6.3", "b1", M910["profile_id"], filler=M910["ota_slot_size"])}
    elif case == "not_app":
        kw["tamper"] = lambda pid, m: m.update(artifact_type="merged")
    with pytest.raises(ValueError, match=error):
        _release(ota, profiles=(M910,), **kw)


def test_profile_definition_cannot_be_silently_redefined(ota):
    _release(ota, profiles=(M910,))
    changed = dict(M910, partition_table_sha256="e" * 64)
    with pytest.raises(ValueError, match="PROFILE_DEFINITION_CONFLICT"):
        _release(ota, version="5.6.4", build="b2", profiles=(changed,))


def test_later_upload_does_not_change_pinned_targets(ota):
    rel1 = _release(ota, profiles=(M910,))
    _bound(ota, A, [device("K", profile_id=M910["profile_id"])])
    ota.create_batch(A, rel1["release_id"], force=True)
    pinned = _target(ota, "K")["artifact_sha256"]
    _release(ota, version="5.6.4", build="b2", profiles=(M910,))
    offer = _check(ota, "K", profile_id=M910["profile_id"])
    assert offer["sha256"] == pinned and offer["version"] == "5.6.3"


def test_new_device_is_not_added_to_existing_snapshot(ota):
    rel = _release(ota)
    _bound(ota, A, [device("K1", profile_id=M910["profile_id"])])
    batch = ota.create_batch(A, rel["release_id"], force=True)
    ota.sync_fleet(A, [device("K1", profile_id=M910["profile_id"]), device("K2", profile_id=M910["profile_id"])])
    assert ota.get_batch(A, batch["batch_id"])["total"] == 1
    assert _check(ota, "K2", profile_id=M910["profile_id"])["update_available"] is False


# ---------------------------------------------------------------- 3) force
def test_force_same_version_installs_exactly_once(ota):
    rel = _release(ota, version="5.6.2", build="b-same")
    _bound(ota, A, [device("K", profile_id=M910["profile_id"], version="5.6.2")])
    ota.create_batch(A, rel["release_id"], force=True)
    offer = _check(ota, "K", version="5.6.2", profile_id=M910["profile_id"])
    assert offer["update_available"] is True and offer["force"] is True
    _install(ota, "K", offer)
    assert _target(ota, "K")["status"] == "SUCCESS"
    # Duplicate polls / duplicate ACK / another reboot never re-flash the same job.
    for _ in range(3):
        assert _check(ota, "K", version="5.6.2", profile_id=M910["profile_id"], artifact=offer["artifact_id"])["update_available"] is False
    assert _ev(ota, "K", "OTA_HEALTHCHECK_OK", offer)["applied"] is False
    # A NEW force batch reinstalls the same artifact once more.
    ota.create_batch(A, rel["release_id"], force=True)
    again = _check(ota, "K", version="5.6.2", profile_id=M910["profile_id"], artifact=offer["artifact_id"])
    assert again["update_available"] is True and again["target_id"] != offer["target_id"]


def test_non_force_skips_device_already_running_artifact(ota):
    rel = _release(ota, version="5.6.2", build="b-same")
    _bound(ota, A, [device("K", profile_id=M910["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    offer = _check(ota, "K", profile_id=M910["profile_id"])
    _install(ota, "K", offer)
    ota.sync_fleet(A, [dict(device("K", profile_id=M910["profile_id"]), firmware_artifact_id=offer["artifact_id"])])
    batch = ota.create_batch(A, rel["release_id"], force=False)
    assert batch["counts"].get("SKIPPED") == 1


def test_downgrade_is_blocked_even_when_forced(ota):
    rel = _release(ota, version="5.6.0", build="old")
    _bound(ota, A, [device("K", profile_id=M910["profile_id"], version="5.6.2")])
    batch = ota.create_batch(A, rel["release_id"], force=True)
    assert batch["counts"]["BLOCKED"] == 1
    assert _target(ota, "K")["error"] == "DOWNGRADE_BLOCKED"


def test_offline_device_waits_and_claims_on_reconnect(ota):
    rel = _release(ota)
    _bound(ota, A, [device("OFF", online=False, profile_id=M910["profile_id"]), device("ON", profile_id=M910["profile_id"])])
    batch = ota.create_batch(A, rel["release_id"], force=True)
    assert batch["total"] == 2
    assert _target(ota, "OFF")["status"] == "WAITING_OFFLINE"
    ota.sync_fleet(A, [device("OFF", online=True, profile_id=M910["profile_id"]), device("ON", profile_id=M910["profile_id"])])
    assert _check(ota, "OFF", profile_id=M910["profile_id"])["update_available"] is True


def test_waiting_safe_point_is_not_success_and_resumes(ota):
    rel = _release(ota)
    _bound(ota, A, [device("K", profile_id=M910["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    res = _check(ota, "K", profile_id=M910["profile_id"], ready=False)
    assert res["update_available"] is False
    t = _target(ota, "K")
    assert t["status"] == "WAITING_SAFE_POINT" and t["wait_reason"] == "DEVICE_BUSY"
    offer = _check(ota, "K", profile_id=M910["profile_id"])
    _ev(ota, "K", "OTA_WAITING_IDLE", offer, error_code="BLOCKED_TRANSACTION")
    t = _target(ota, "K")
    assert t["status"] == "WAITING_SAFE_POINT" and t["wait_reason"] == "BLOCKED_TRANSACTION"
    assert _check(ota, "K", profile_id=M910["profile_id"])["update_available"] is True


def test_one_failure_does_not_stop_others_and_retries_with_backoff(ota):
    rel = _release(ota)
    _bound(ota, A, [device("BAD", profile_id=M910["profile_id"]), device("GOOD", profile_id=M910["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    bad = _check(ota, "BAD", profile_id=M910["profile_id"])
    assert ota.target_for_download(A, "BAD", bad["artifact_id"], bad["claim_id"])
    _ev(ota, "BAD", "OTA_VERIFY_FAILED", bad, error_code="OTA_HASH_MISMATCH")
    t = _target(ota, "BAD")
    assert t["status"] == "QUEUED" and t["attempt"] == 1 and t["next_retry_at"] > ota._now()
    assert _check(ota, "BAD", profile_id=M910["profile_id"])["reason"] == "RETRY_BACKOFF"
    good = _check(ota, "GOOD", profile_id=M910["profile_id"])
    _install(ota, "GOOD", good)
    assert _target(ota, "GOOD")["status"] == "SUCCESS"


def test_attempts_are_bounded(ota, monkeypatch):
    rel = _release(ota)
    _bound(ota, A, [device("K", profile_id=M910["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    clock = [ota._now()]
    monkeypatch.setattr(ota, "_now", lambda: clock[0])
    for i in range(ota.MAX_ATTEMPTS):
        offer = _check(ota, "K", profile_id=M910["profile_id"])
        assert offer["update_available"], (i, offer)
        ota.target_for_download(A, "K", offer["artifact_id"], offer["claim_id"])
        _ev(ota, "K", "OTA_FAILED", offer, error_code="OTA_NETWORK_ERROR")
        clock[0] += 3600
    assert _target(ota, "K")["status"] == "FAILED"
    assert _check(ota, "K", profile_id=M910["profile_id"])["update_available"] is False


def test_rollback_after_reboot_is_failure_not_success(ota, monkeypatch):
    rel = _release(ota)
    _bound(ota, A, [device("K", profile_id=M910["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    offer = _check(ota, "K", profile_id=M910["profile_id"])
    ota.target_for_download(A, "K", offer["artifact_id"], offer["claim_id"])
    for e in ("OTA_DOWNLOAD_START", "OTA_DOWNLOAD_COMPLETE", "OTA_REBOOTING"):
        _ev(ota, "K", e, offer)
    # Synthetic heartbeat claiming the new version must NOT mark success.
    ota.sync_fleet(A, [device("K", profile_id=M910["profile_id"], version="5.6.3")])
    assert _target(ota, "K")["status"] == "REBOOTING"
    _ev(ota, "K", "OTA_ROLLBACK", offer, error_code="OTA_BOOT_FAILED")
    assert _target(ota, "K")["status"] in {"QUEUED", "FAILED"}
    assert _target(ota, "K")["status"] != "SUCCESS"


def test_healthcheck_for_wrong_artifact_is_ignored(ota):
    rel = _release(ota)
    _bound(ota, A, [device("K", profile_id=M910["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    offer = _check(ota, "K", profile_id=M910["profile_id"])
    ota.target_for_download(A, "K", offer["artifact_id"], offer["claim_id"])
    _ev(ota, "K", "OTA_REBOOTING", offer)
    res = _ev(ota, "K", "OTA_HEALTHCHECK_OK", offer, firmware_id="some-other-artifact")
    assert res["applied"] is False and _target(ota, "K")["status"] == "REBOOTING"


def test_reboot_lease_expiry_without_boot_proof_retries(ota, monkeypatch):
    rel = _release(ota)
    _bound(ota, A, [device("K", profile_id=M910["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    clock = [ota._now()]
    monkeypatch.setattr(ota, "_now", lambda: clock[0])
    offer = _check(ota, "K", profile_id=M910["profile_id"])
    ota.target_for_download(A, "K", offer["artifact_id"], offer["claim_id"])
    _ev(ota, "K", "OTA_REBOOTING", offer)
    # Device polls during the reboot window still running the old image -> wait, not re-offer.
    assert _check(ota, "K", profile_id=M910["profile_id"])["update_available"] is False
    clock[0] += ota.REBOOT_LEASE_S + 1
    _check(ota, "K", profile_id=M910["profile_id"])
    t = _target(ota, "K")
    assert t["status"] == "QUEUED" and t["error"] == "BOOT_NOT_CONFIRMED"


def test_legacy_firmware_flow_without_profile_reporting(ota):
    """5.6.2 devices report only hardware_model; admin assigns the profile once."""
    rel = _release(ota, profiles=(M910,))
    _bound(ota, A, [device("LEG")])
    ota.assign_profile(A, ["LEG"], M910["profile_id"])
    ota.create_batch(A, rel["release_id"], force=True)
    offer = ota.device_check(A, "LEG", {"current_version": "5.6.2", "hardware_model": "ES3C28P"}, readiness={"ota_ready": True})
    assert offer["update_available"] and offer["hardware_model"] == "ES3C28P" and offer["firmware_id"] == offer["artifact_id"]
    assert ota.target_for_download(A, "LEG", offer["firmware_id"], None)
    for e in ("OTA_DOWNLOAD_START", "OTA_DOWNLOAD_COMPLETE", "OTA_VERIFY_OK", "OTA_REBOOTING"):
        ota.record_device_event(A, "LEG", {"status": e, "firmware_id": offer["firmware_id"], "to_version": "5.6.3"})
    ota.record_device_event(A, "LEG", {"status": "OTA_BOOT_NEW_VERSION", "firmware_id": offer["firmware_id"], "to_version": "5.6.3"})
    ota.record_device_event(A, "LEG", {"status": "OTA_HEALTHCHECK_OK", "firmware_id": offer["firmware_id"], "to_version": "5.6.3"})
    assert _target(ota, "LEG")["status"] == "SUCCESS"


def test_legacy_model_mismatch_blocks(ota):
    rel = _release(ota, profiles=(M910,))
    _bound(ota, A, [device("LEG", model="OTHER")])
    ota.assign_profile(A, ["LEG"], M910["profile_id"])
    batch = ota.create_batch(A, rel["release_id"], force=True)
    assert batch["counts"]["BLOCKED"] == 1 and _target(ota, "LEG")["error"] == "HARDWARE_MODEL_MISMATCH"


def test_concurrent_claims_never_double_offer_or_double_open(ota):
    rel = _release(ota)
    _bound(ota, A, [device("K", profile_id=M910["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    results = []
    def worker():
        results.append(_check(ota, "K", profile_id=M910["profile_id"]))
    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]; [t.join() for t in threads]
    claims = {r["claim_id"] for r in results if r.get("update_available")}
    assert len(claims) == 1
    # A second batch while one is open does not create a second open target.
    ota.create_batch(A, rel["release_id"], force=True)
    with sqlite3.connect(ota.OTA_DB) as c:
        n = c.execute("SELECT COUNT(*) FROM ota2_targets WHERE instance_id=? AND kiosk_id='K' AND status NOT IN ('SUCCESS','FAILED','BLOCKED','CANCELLED','SKIPPED')", (A,)).fetchone()[0]
    assert n == 1


def test_in_flight_install_is_not_superseded_by_new_batch(ota):
    rel = _release(ota)
    _bound(ota, A, [device("K", profile_id=M910["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    offer = _check(ota, "K", profile_id=M910["profile_id"])
    ota.target_for_download(A, "K", offer["artifact_id"], offer["claim_id"])
    second = ota.create_batch(A, rel["release_id"], force=True)
    assert second["counts"]["BLOCKED"] == 1
    assert ota.list_targets(A, batch_id=second["batch_id"])["items"][0]["error"] == "OTA_IN_PROGRESS"


def test_cancel_does_not_interrupt_flashing_device(ota):
    rel = _release(ota)
    _bound(ota, A, [device("F", profile_id=M910["profile_id"]), device("Q", profile_id=M910["profile_id"])])
    batch = ota.create_batch(A, rel["release_id"], force=True)
    offer = _check(ota, "F", profile_id=M910["profile_id"])
    ota.target_for_download(A, "F", offer["artifact_id"], offer["claim_id"])
    _ev(ota, "F", "OTA_DOWNLOAD_COMPLETE", offer)
    ota.cancel_batch(A, batch["batch_id"])
    assert _target(ota, "F")["status"] == "FLASHING"
    assert _target(ota, "Q")["status"] == "CANCELLED"


def test_jobs_survive_engine_restart(ota, tmp_path, monkeypatch):
    rel = _release(ota)
    _bound(ota, A, [device("K", profile_id=M910["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    offer = _check(ota, "K", profile_id=M910["profile_id"])
    import ota_control
    again = importlib.reload(ota_control)
    again.init_ota_db()
    same = again.device_check(A, "K", _report(profile_id=M910["profile_id"]), readiness={"ota_ready": True})
    assert same["claim_id"] == offer["claim_id"]
    assert again.list_targets(A)["total"] == 1


def test_download_requires_live_claim_and_pinned_artifact(ota):
    rel = _release(ota)
    _bound(ota, A, [device("K", profile_id=M910["profile_id"]), device("Z", profile_id=KL["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    offer = _check(ota, "K", profile_id=M910["profile_id"])
    other = _check(ota, "Z", profile_id=KL["profile_id"])
    assert ota.target_for_download(A, "K", other["artifact_id"], offer["claim_id"]) is None
    assert ota.target_for_download(A, "K", offer["artifact_id"], "forged") is None
    row = ota.target_for_download(A, "K", offer["artifact_id"], offer["claim_id"])
    assert row and Path(row["storage_path"]).stat().st_size == offer["size"]


def test_list_targets_pagination_and_totals_include_blocked_offline(ota):
    rel = _release(ota, profiles=(M910,))
    items = [device(f"K{i:02d}", profile_id=M910["profile_id"], online=i % 3 != 0) for i in range(25)] + [device("NOPROFILE")]
    _bound(ota, A, items)
    batch = ota.create_batch(A, rel["release_id"], force=True)
    assert batch["total"] == 26
    assert batch["counts"]["BLOCKED"] == 1 and batch["counts"]["WAITING_OFFLINE"] == 9
    page1 = ota.list_targets(A, batch_id=batch["batch_id"], page=1, per_page=10)
    page3 = ota.list_targets(A, batch_id=batch["batch_id"], page=3, per_page=10)
    assert page1["total"] == 26 and len(page1["items"]) == 10 and len(page3["items"]) == 6
    assert ota.list_targets(A, status="BLOCKED")["total"] == 1


# ---------------------------------------------------------------- resources
def _open_fds():
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc FD table")
def test_store_calls_do_not_leak_file_descriptors(ota):
    rel = _release(ota)
    _bound(ota, A, [device("K", profile_id=M910["profile_id"])])
    ota.create_batch(A, rel["release_id"], force=True)
    for _ in range(3):  # warm-up
        _check(ota, "K", profile_id=M910["profile_id"]); ota.list_releases(); ota.list_fleet(A)
    before = _open_fds()
    for _ in range(200):
        _check(ota, "K", profile_id=M910["profile_id"])
        ota.list_releases(); ota.list_fleet(A); ota.list_targets(A); ota.bound_instance(); ota.list_jobs()
    assert _open_fds() - before <= 2


def test_connect_rolls_back_on_error_and_commits_on_success(ota):
    with pytest.raises(RuntimeError):
        with ota._connect() as c:
            c.execute("INSERT INTO ota2_meta(key,value,updated_at) VALUES('probe','x',0)")
            raise RuntimeError("boom")
    with ota._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM ota2_meta WHERE key='probe'").fetchone()[0] == 0
        c.execute("INSERT INTO ota2_meta(key,value,updated_at) VALUES('probe','y',0)")
    with ota._connect() as c:
        assert c.execute("SELECT value FROM ota2_meta WHERE key='probe'").fetchone()[0] == "y"


def test_repeated_force_while_waiting_returns_same_batch(ota):
    rel = _release(ota)
    _bound(ota, A, [device("K1", profile_id=M910["profile_id"]), device("K2", profile_id=M910["profile_id"], online=False)])
    first = ota.create_batch(A, rel["release_id"], force=True)
    again = ota.create_batch(A, rel["release_id"], force=True)
    assert again["batch_id"] == first["batch_id"] and again.get("reused") is True
    assert len(ota.list_batches(A)) == 1
    assert {t["status"] for t in ota.list_targets(A)["items"]} == {"QUEUED", "WAITING_OFFLINE"}
    # once K1 finished, a new force is a NEW command (re-installs once more)
    offer = _check(ota, "K1", profile_id=M910["profile_id"])
    _install(ota, "K1", offer)
    third = ota.create_batch(A, rel["release_id"], force=True)
    assert third["batch_id"] != first["batch_id"] and not third.get("reused")
