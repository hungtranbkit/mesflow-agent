"""HTTP-level scope/auth tests for the ESP OTA routes with a fake MESFlow.

MESFlow is replaced by an in-memory double behind agent._mesflow_json, so
the tests exercise the real Flask routes, auth gates and ota_control store.
"""
from __future__ import annotations

import importlib
import io
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("WORKSHOP_AGENT_HOME", tempfile.mkdtemp(prefix="mesflow-ota-routes-"))
from esp_ota_fixtures import device, profile, release_zip  # noqa: E402

A = "inst-a"
B = "inst-b"
P = profile("es3c28p-n16r8-m910-gm65uart")


class FakeMesflow:
    def __init__(self):
        self.instance = {"instance_id": A, "state": "VERIFIED", "display_name": "Kimex", "public_url": "https://mesflow.example"}
        self.tokens = {"tok-k1": ("K1", A), "tok-b": ("K1", B)}
        self.items = [device("K1", profile_id=P["profile_id"])]
        self.up = True

    def __call__(self, path, headers, timeout=5):
        if not self.up:
            raise OSError("down")
        if path == "/api/internal/instance":
            return dict(self.instance)
        if path == "/api/internal/kiosks":
            return {"ok": True, "instance_id": self.instance["instance_id"], "items": self.items}
        if path.endswith("/ota-readiness"):
            kid = path.split("/")[-2]
            owner = self.tokens.get(headers.get("X-Kiosk-Token", ""))
            if not owner or owner[0] != kid:
                raise urllib.error.HTTPError(path, 403, "FORBIDDEN", {}, None)
            return {"ok": True, "kiosk_id": kid, "instance_id": owner[1], "instance_state": "VERIFIED", "ota_ready": True, "reason": "READY"}
        raise AssertionError(path)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSHOP_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("MESFLOW_INTERNAL_API_TOKEN", "internal")
    monkeypatch.delenv("WORKSHOP_AGENT_PUBLIC_URL", raising=False)
    import ota_control
    importlib.reload(ota_control)
    import agent
    monkeypatch.setattr(agent, "ota", ota_control)
    fake = FakeMesflow()
    monkeypatch.setattr(agent, "_mesflow_json", fake)
    agent._instance_cache.update(at=0.0, value=None)
    client = agent.app.test_client()
    with client.session_transaction() as s:
        s["authenticated"] = True
        s["csrf"] = "c"
    return agent, ota_control, fake, client


def _upload(client):
    r = client.post("/api/esp-ota/releases", headers={"X-CSRF-Token": "c"},
                    data={"package": (release_zip("5.6.3", "b1", [P]), "rel.ota.zip")}, content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    return r.get_json()["release"]


def _force(client, rid):
    prev = client.post("/api/esp-ota/preview", json={"release_id": rid}).get_json()["preview"]
    r = client.post("/api/esp-ota/force", headers={"X-CSRF-Token": "c"}, json={"release_id": rid, "confirm": True, "confirm_total": prev["total"]})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["batch"]


def test_mutations_require_csrf(env):
    agent, ota, fake, client = env
    r = client.post("/api/esp-ota/releases", data={"package": (release_zip("5.6.3", "b1", [P]), "rel.ota.zip")}, content_type="multipart/form-data")
    assert r.status_code == 403


def test_force_requires_single_confirmation_matching_preview(env):
    agent, ota, fake, client = env
    rel = _upload(client)
    r = client.post("/api/esp-ota/force", headers={"X-CSRF-Token": "c"}, json={"release_id": rel["release_id"], "confirm": True, "confirm_total": 99})
    assert r.status_code == 409 and r.get_json()["error"] == "PREVIEW_STALE"
    batch = _force(client, rel["release_id"])
    assert batch["total"] == 1 and batch["counts"] == {"QUEUED": 1}


def test_device_check_download_event_end_to_end_same_origin(env):
    agent, ota, fake, client = env
    rel = _upload(client)
    _force(client, rel["release_id"])
    h = {"X-Kiosk-Token": "tok-k1"}
    r = client.get(f"/api/esp-ota/check?kiosk_id=K1&current_version=5.6.2&hardware_model=ES3C28P&hardware_profile={P['profile_id']}&api=2", headers=h)
    offer = r.get_json()
    assert offer["update_available"] and "/api/esp-ota/file/" in offer["url"] and offer["url"].startswith("http://localhost/")
    assert "claim_id=" + offer["claim_id"] in offer["url"]
    path = "/api/esp-ota/file/" + offer["url"].split("/api/esp-ota/file/", 1)[1]
    bad = client.get(path.replace(offer["claim_id"], "forged"), headers=h)
    assert bad.status_code == 403
    dl = client.get(path, headers=h)
    assert dl.status_code == 200 and len(dl.data) == offer["size"]
    for e in ("OTA_DOWNLOAD_START", "OTA_DOWNLOAD_COMPLETE", "OTA_REBOOTING", "OTA_BOOT_NEW_VERSION", "OTA_HEALTHCHECK_OK"):
        ev = client.post("/api/esp-ota/event", headers=h, json={"kiosk_id": "K1", "status": e, "firmware_id": offer["artifact_id"],
                                                               "to_version": "5.6.3", "target_id": offer["target_id"], "claim_id": offer["claim_id"],
                                                               "build_id": offer["build"], "hardware_profile": offer["hardware_profile"]})
        assert ev.status_code == 201
    t = client.get("/api/esp-ota/targets").get_json()["items"][0]
    assert t["status"] == "SUCCESS"


def test_device_token_of_other_instance_or_other_device_is_rejected(env):
    agent, ota, fake, client = env
    rel = _upload(client)
    _force(client, rel["release_id"])
    # token belongs to K1 of instance B -> Agent bound to A refuses, no offer
    r = client.get("/api/esp-ota/check?kiosk_id=K1&current_version=5.6.2", headers={"X-Kiosk-Token": "tok-b"})
    assert r.get_json()["update_available"] is False and r.get_json()["reason"] == "INSTANCE_MISMATCH"
    # a self-declared kiosk_id that the token does not own
    r = client.get("/api/esp-ota/check?kiosk_id=K9&current_version=5.6.2", headers={"X-Kiosk-Token": "tok-k1"})
    assert r.status_code == 401
    # no token at all
    assert client.get("/api/esp-ota/check?kiosk_id=K1").status_code == 401
    # events/downloads with the foreign token are rejected too
    assert client.post("/api/esp-ota/event", headers={"X-Kiosk-Token": "tok-b"}, json={"kiosk_id": "K1", "status": "OTA_HEALTHCHECK_OK"}).get_json().get("applied") is None


def test_cloned_or_unverified_instance_fails_closed(env):
    agent, ota, fake, client = env
    rel = _upload(client)
    _force(client, rel["release_id"])
    fake.instance = {"instance_id": "inst-clone", "state": "VERIFIED"}
    agent._instance_cache.update(at=0.0, value=None)
    r = client.post("/api/esp-ota/preview", json={"release_id": rel["release_id"]})
    assert r.status_code == 409 and r.get_json()["error"] == "INSTANCE_MISMATCH"
    fake.instance = {"instance_id": A, "state": "UNVERIFIED_CLONE"}
    agent._instance_cache.update(at=0.0, value=None)
    r = client.post("/api/esp-ota/force", headers={"X-CSRF-Token": "c"}, json={"release_id": rel["release_id"], "confirm": True, "confirm_total": 1})
    assert r.status_code == 409 and r.get_json()["error"] == "INSTANCE_UNVERIFIED"


def test_mesflow_outage_is_waiting_not_failure(env):
    agent, ota, fake, client = env
    rel = _upload(client)
    _force(client, rel["release_id"])
    fake.up = False
    r = client.get("/api/esp-ota/check?kiosk_id=K1&current_version=5.6.2", headers={"X-Kiosk-Token": "tok-k1"})
    assert r.status_code == 200 and r.get_json()["reason"] == "WAITING_MESFLOW_STATE"
    # admin reads still work against the bound instance during the outage
    agent._instance_cache.update(at=0.0, value=None)
    lst = client.get("/api/esp-ota/targets").get_json()
    assert lst["total"] == 1 and lst["items"][0]["status"] == "QUEUED"


def test_legacy_bin_upload_is_rejected_and_legacy_job_routes_are_scoped(env):
    agent, ota, fake, client = env
    r = client.post("/api/esp-ota/upload", headers={"X-CSRF-Token": "c"},
                    data={"firmware": (io.BytesIO(b"\xe9" * 400), "esp.ino.bin"), "version": "5.6.3"}, content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["error"] == "OTA_RELEASE_PACKAGE_REQUIRED"
    rel = _upload(client)
    batch = _force(client, rel["release_id"])
    assert client.get(f"/api/esp-ota/status/{batch['batch_id']}").status_code == 200
    assert client.get("/api/esp-ota/jobs").get_json()["items"][0]["batch_id"] == batch["batch_id"]


def test_rebind_is_only_an_explicit_confirmed_owner_action(env):
    agent, ota, fake, client = env
    rel = _upload(client)
    _force(client, rel["release_id"])
    # Agent now talks to another MESFlow instance: nothing rebinds implicitly.
    fake.instance = {"instance_id": B, "state": "VERIFIED"}
    agent._instance_cache.update(at=0.0, value=None)
    assert client.post("/api/esp-ota/preview", json={"release_id": rel["release_id"]}).get_json()["error"] == "INSTANCE_MISMATCH"
    client.get("/api/esp-ota/check?kiosk_id=K1&current_version=5.6.2", headers={"X-Kiosk-Token": "tok-b"})
    client.get("/api/esp-ota/overview")
    assert ota.bound_instance() == A
    # no session / no CSRF / wrong or missing confirmation -> refused, still bound to A
    anon = agent.app.test_client()
    assert anon.post("/api/esp-ota/instance/rebind", json={"confirm_instance_id": B}).status_code in (302, 401)  # login gate
    assert client.post("/api/esp-ota/instance/rebind", json={"confirm_instance_id": B}).status_code == 403
    r = client.post("/api/esp-ota/instance/rebind", headers={"X-CSRF-Token": "c"}, json={"confirm_instance_id": A})
    assert r.status_code == 400 and r.get_json()["error"] == "CONFIRMATION_REQUIRED"
    assert ota.bound_instance() == A
    # explicit confirmed owner action: bound to B, A's open targets cancelled
    r = client.post("/api/esp-ota/instance/rebind", headers={"X-CSRF-Token": "c"}, json={"confirm_instance_id": B})
    assert r.status_code == 200 and r.get_json()["previous_instance_id"] == A and r.get_json()["cancelled_targets"] == 1
    assert ota.bound_instance() == B
    assert ota.list_targets(A)["items"][0]["status"] == "CANCELLED"


def test_inventory_from_another_instance_is_refused_as_mismatch(env):
    agent, ota, fake, client = env
    rel = _upload(client)
    _force(client, rel["release_id"])
    # MESFlow switches instance while the Agent still holds a cached identity
    fake.instance = {"instance_id": B, "state": "VERIFIED"}
    r = client.post("/api/esp-ota/force", headers={"X-CSRF-Token": "c"}, json={"release_id": rel["release_id"], "confirm": True, "confirm_total": 1})
    assert r.status_code == 409 and r.get_json()["error"] == "INSTANCE_MISMATCH"
    r = client.post("/api/esp-ota/preview", json={"release_id": rel["release_id"]})
    assert r.status_code == 409 and r.get_json()["error"] == "INSTANCE_MISMATCH"
    assert {t["kiosk_id"] for t in ota.list_targets(A)["items"]} == {"K1"} and ota.list_targets(B)["total"] == 0


def test_scope_recovers_immediately_when_mesflow_returns_to_bound_instance(env):
    agent, ota, fake, client = env
    rel = _upload(client)
    assert client.post("/api/esp-ota/preview", json={"release_id": rel["release_id"]}).status_code == 200  # binds A
    fake.instance = {"instance_id": B, "state": "VERIFIED"}
    agent._instance_cache.update(at=0.0, value=None)
    r = client.post("/api/esp-ota/preview", json={"release_id": rel["release_id"]})
    assert r.status_code == 409 and r.get_json()["error"] == "INSTANCE_MISMATCH"
    fake.instance = {"instance_id": A, "state": "VERIFIED"}
    r = client.post("/api/esp-ota/preview", json={"release_id": rel["release_id"]})
    assert r.status_code == 200 and ota.bound_instance() == A
