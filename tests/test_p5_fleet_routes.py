"""P5 Multi-server -- Flask-route-level wiring, against a REAL second
`agent.py` process acting as a Remote Agent (real HTTP, real login/CSRF,
real /api/status) -- never mocked. This is vertical slice 1 (TEST server
registration + health) plus the required registry/health/routing tests
(spec sections 81, 82, 87, 88, 89).
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

from conftest import ADMIN_PASSWORD, spawn_agent, stop_agent

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def dev_agent(tmp_path):
    import os
    home = tmp_path / "dev_home"
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "data" / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["WORKSHOP_AGENT_HOME"] = str(home)
    os.environ.setdefault("MESFLOW_AGENT_RUNTIME", "linux")
    os.environ["MESFLOW_AGENT_ADMIN_PASSWORD"] = ADMIN_PASSWORD
    os.environ["SERVER_ROLE"] = "DEV"
    spec = importlib.util.spec_from_file_location(f"agent_p5_dev_{uuid.uuid4().hex[:6]}", ROOT / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod.app.testing = True
    mod._register_local_server()
    return mod


def _client(agent):
    client = agent.app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "test-csrf"
        sess["username"] = "test-admin"
    return client


CSRF = {"X-CSRF-Token": "test-csrf"}


@pytest.fixture(scope="module")
def remote_test_server(tmp_path_factory):
    proc, base_url = spawn_agent(tmp_path_factory.mktemp("remote_test"), port=18191, server_role="PRODUCTION_TEST")
    try:
        yield base_url
    finally:
        stop_agent(proc)


# -- registration --------------------------------------------------------

def test_local_dev_node_self_registered(dev_agent):
    client = _client(dev_agent)
    items = client.get("/api/servers").get_json()["items"]
    local = next(s for s in items if s["server_id"] == "local")
    assert local["environment"] == "DEV"
    assert local["registration_status"] == "ACTIVE"
    assert "CONTROL" in local["roles"]
    assert "agent_password" not in local  # never leaked, even for the local record


def test_register_requires_login(dev_agent):
    client = dev_agent.app.test_client()
    r = client.post("/api/servers", json={"name": "x", "environment": "TEST"})
    assert r.status_code in (302, 401)


def test_register_production_without_confirmation_is_blocked(dev_agent):
    client = _client(dev_agent)
    r = client.post("/api/servers", json={"name": "mesflow-prod", "environment": "PRODUCTION",
                                           "agent_url": "http://example.invalid"}, headers=CSRF)
    assert r.status_code == 403
    assert r.get_json()["error"] == "PRODUCTION_CONFIRMATION_REQUIRED"


def test_register_unknown_environment_rejected(dev_agent):
    client = _client(dev_agent)
    r = client.post("/api/servers", json={"name": "x", "environment": "STAGING"}, headers=CSRF)
    assert r.status_code == 400


# -- vertical slice 1: TEST registration + real handshake + health ------

def test_register_and_handshake_real_remote_test_agent(dev_agent, remote_test_server):
    client = _client(dev_agent)
    r = client.post("/api/servers", json={
        "name": "mesflow-test", "environment": "TEST", "roles": ["MESFLOW", "POSTGRESQL"],
        "agent_url": remote_test_server, "agent_user": "admin", "agent_password": ADMIN_PASSWORD,
    }, headers=CSRF)
    assert r.status_code == 200
    server = r.get_json()["item"]
    assert server["registration_status"] == "PENDING"  # never healthy before handshake (section 59)
    sid = server["server_id"]

    r2 = client.post(f"/api/servers/{sid}/test-connection", headers=CSRF)
    assert r2.status_code == 200
    body = r2.get_json()
    assert body["ok"] is True
    assert body["item"]["registration_status"] == "ACTIVE"
    assert body["identity"]["agent_version"]
    assert "health_status" in body["identity"] or "capabilities" in body["identity"]
    assert body["compatibility"]["status"] in ("COMPATIBLE", "UPDATE_AVAILABLE")

    # real background-poll-equivalent: synchronous poll_once against the
    # real remote process
    dev_agent._fleet_health_service.poll_once()
    detail = client.get(f"/api/servers/{sid}").get_json()["item"]
    assert detail["health_status"] == "ONLINE"
    assert detail["last_seen"]


def test_fleet_summary_reflects_dev_and_test(dev_agent, remote_test_server):
    client = _client(dev_agent)
    reg = client.post("/api/servers", json={"name": "mesflow-test", "environment": "TEST",
        "agent_url": remote_test_server, "agent_user": "admin", "agent_password": ADMIN_PASSWORD}, headers=CSRF).get_json()["item"]
    client.post(f"/api/servers/{reg['server_id']}/test-connection", headers=CSRF)
    dev_agent._fleet_health_service.poll_once()
    summary = client.get("/api/fleet/summary").get_json()
    assert summary["ok"] is True
    assert summary["counts"]["total"] == 2  # local + TEST
    assert summary["counts"]["online"] == 2


def test_server_routing_reaches_only_the_target_server(dev_agent, remote_test_server):
    """Section 83/85: a request scoped to the TEST server_id must reach the
    TEST remote agent specifically, never the DEV agent's own data."""
    client = _client(dev_agent)
    reg = client.post("/api/servers", json={"name": "mesflow-test", "environment": "TEST",
        "agent_url": remote_test_server, "agent_user": "admin", "agent_password": ADMIN_PASSWORD}, headers=CSRF).get_json()["item"]
    sid = reg["server_id"]
    client.post(f"/api/servers/{sid}/test-connection", headers=CSRF)

    remote_incidents = client.get(f"/api/servers/{sid}/incidents")
    assert remote_incidents.status_code == 200
    assert remote_incidents.get_json()["server_id"] == sid

    remote_storage = client.get(f"/api/servers/{sid}/storage")
    assert remote_storage.status_code == 200
    assert remote_storage.get_json()["ok"] is True

    remote_backups = client.get(f"/api/servers/{sid}/backups")
    assert remote_backups.status_code == 200
    assert remote_backups.get_json()["server_id"] == sid

    # the LOCAL server's own storage/backups are independently reachable
    # and do not depend on the remote at all.
    local_storage = client.get("/api/servers/local/storage")
    assert local_storage.status_code == 200


# -- section 81/89: duplicate identity, disable, production safety ------

def test_duplicate_agent_identity_rejected_via_route(dev_agent, remote_test_server):
    client = _client(dev_agent)
    r1 = client.post("/api/servers", json={"name": "test-a", "environment": "TEST",
        "agent_url": remote_test_server, "agent_user": "admin", "agent_password": ADMIN_PASSWORD}, headers=CSRF).get_json()["item"]
    r2 = client.post("/api/servers", json={"name": "test-b-duplicate", "environment": "TEST",
        "agent_url": remote_test_server, "agent_user": "admin", "agent_password": ADMIN_PASSWORD}, headers=CSRF).get_json()["item"]
    ok1 = client.post(f"/api/servers/{r1['server_id']}/test-connection", headers=CSRF)
    assert ok1.status_code == 200
    conflict = client.post(f"/api/servers/{r2['server_id']}/test-connection", headers=CSRF)
    assert conflict.status_code == 409
    assert conflict.get_json()["error"] == "DUPLICATE_AGENT_IDENTITY"


def test_authentication_failure_reported_not_silently_treated_as_offline(dev_agent, remote_test_server):
    client = _client(dev_agent)
    reg = client.post("/api/servers", json={"name": "bad-creds", "environment": "TEST",
        "agent_url": remote_test_server, "agent_user": "admin", "agent_password": "WRONG-PASSWORD-xyz"}, headers=CSRF).get_json()["item"]
    r = client.post(f"/api/servers/{reg['server_id']}/test-connection", headers=CSRF)
    assert r.status_code == 502
    assert r.get_json()["error"] in ("REMOTE_AUTH_FAILED", "REMOTE_OPERATION_FAILED")
    detail = client.get(f"/api/servers/{reg['server_id']}").get_json()["item"]
    # This server was never successfully handshaken before -- it correctly
    # stays PENDING (not "lost", since it was never reachable to begin
    # with); UNREACHABLE is reserved for a previously-ACTIVE server that
    # stops responding (section 11: registration state != health state).
    assert detail["registration_status"] == "PENDING"
    assert detail["health_status"] == "OFFLINE"
    assert "LOGIN_FAILED" in detail["last_error"] or "auth" in detail["last_error"].lower()

    # Now prove the OTHER half of that same distinction: a server that WAS
    # active and then fails really does become UNREACHABLE.
    good = client.post("/api/servers", json={"name": "was-active", "environment": "TEST",
        "agent_url": remote_test_server, "agent_user": "admin", "agent_password": ADMIN_PASSWORD}, headers=CSRF).get_json()["item"]
    client.post(f"/api/servers/{good['server_id']}/test-connection", headers=CSRF)
    assert client.get(f"/api/servers/{good['server_id']}").get_json()["item"]["registration_status"] == "ACTIVE"
    dev_agent._server_registry.record_handshake_failure(good["server_id"], "simulated connection drop")
    assert client.get(f"/api/servers/{good['server_id']}").get_json()["item"]["registration_status"] == "UNREACHABLE"


def test_wrong_target_server_id_returns_not_found(dev_agent):
    client = _client(dev_agent)
    r = client.get("/api/servers/does-not-exist/incidents")
    assert r.status_code == 404


def test_disabled_server_excluded_from_polling_but_still_listed(dev_agent, remote_test_server):
    client = _client(dev_agent)
    reg = client.post("/api/servers", json={"name": "mesflow-test", "environment": "TEST",
        "agent_url": remote_test_server, "agent_user": "admin", "agent_password": ADMIN_PASSWORD}, headers=CSRF).get_json()["item"]
    sid = reg["server_id"]
    client.post(f"/api/servers/{sid}/test-connection", headers=CSRF)
    client.post(f"/api/servers/{sid}/disable", headers=CSRF)
    dev_agent._fleet_health_service.poll_once()
    detail = client.get(f"/api/servers/{sid}").get_json()["item"]
    assert detail["registration_status"] == "DISABLED"
    # still listed (history preserved, section 57) -- just not polled/health-updated
    assert any(s["server_id"] == sid for s in client.get("/api/servers").get_json()["items"])


# -- section 87: remote agent goes offline mid-fleet, section 46: isolation --

def test_one_unreachable_server_does_not_break_fleet_summary(dev_agent, remote_test_server):
    client = _client(dev_agent)
    good = client.post("/api/servers", json={"name": "mesflow-test", "environment": "TEST",
        "agent_url": remote_test_server, "agent_user": "admin", "agent_password": ADMIN_PASSWORD}, headers=CSRF).get_json()["item"]
    bad = client.post("/api/servers", json={"name": "unreachable-node", "environment": "TEST",
        "agent_url": "http://127.0.0.1:1", "agent_user": "admin", "agent_password": "x"}, headers=CSRF).get_json()["item"]
    client.post(f"/api/servers/{good['server_id']}/test-connection", headers=CSRF)
    client.post(f"/api/servers/{bad['server_id']}/test-connection", headers=CSRF)  # expected to fail, must not raise

    dev_agent._fleet_health_service.poll_once()  # must not hang/crash on the bad one
    summary = client.get("/api/fleet/summary").get_json()
    assert summary["ok"] is True
    assert summary["counts"]["total"] == 3  # local + good + bad
    assert summary["counts"]["online"] == 2  # local + good
    assert summary["counts"]["offline"] >= 1

    good_detail = client.get(f"/api/servers/{good['server_id']}").get_json()["item"]
    assert good_detail["health_status"] == "ONLINE"


# -- section 8: two real agents on the same software version must never
# collide on identity (regression test -- found live while seeding P5 UI
# screenshots: /api/status previously had no real per-instance identity
# field, so record_handshake()'s agent_id fallback used agent_version,
# which every same-build instance shares) -------------------------------

def test_two_real_agents_same_version_get_distinct_identities(dev_agent, tmp_path):
    from conftest import spawn_agent, stop_agent
    proc_a, base_a = spawn_agent(tmp_path, port=18194, server_role="PRODUCTION_TEST")
    proc_b, base_b = spawn_agent(tmp_path, port=18195, server_role="PRODUCTION_TEST")
    try:
        client = _client(dev_agent)
        a = client.post("/api/servers", json={"name": "node-a", "environment": "TEST",
            "agent_url": base_a, "agent_user": "admin", "agent_password": ADMIN_PASSWORD}, headers=CSRF).get_json()["item"]
        b = client.post("/api/servers", json={"name": "node-b", "environment": "TEST",
            "agent_url": base_b, "agent_user": "admin", "agent_password": ADMIN_PASSWORD}, headers=CSRF).get_json()["item"]

        ra = client.post(f"/api/servers/{a['server_id']}/test-connection", headers=CSRF)
        assert ra.status_code == 200, ra.get_json()
        rb = client.post(f"/api/servers/{b['server_id']}/test-connection", headers=CSRF)
        assert rb.status_code == 200, rb.get_json()  # must NOT be 409 DUPLICATE_AGENT_IDENTITY

        agent_id_a = ra.get_json()["identity"]["agent_id"]
        agent_id_b = rb.get_json()["identity"]["agent_id"]
        assert agent_id_a and agent_id_b
        assert agent_id_a != agent_id_b
        # same software build -- proving the distinction is real identity,
        # not accidentally still keyed off version.
        assert ra.get_json()["identity"]["agent_version"] == rb.get_json()["identity"]["agent_version"]
    finally:
        stop_agent(proc_a)
        stop_agent(proc_b)
