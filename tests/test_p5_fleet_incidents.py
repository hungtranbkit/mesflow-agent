"""P5 Multi-server -- central incident aggregation (section 33/34/86).
Every registered server (including the local/DEV one, which uses the same
registry model as any remote node) is tagged with server_id/server_name/
environment in the central Fleet view. Runs entirely in-process against the
LOCAL server -- no subprocess/real network needed, since the aggregation
logic itself (not the remote transport, already proven in
test_p5_fleet_routes.py) is what's under test here.
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def dev_agent(tmp_path):
    import os
    home = tmp_path / "dev_home"
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "data" / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["WORKSHOP_AGENT_HOME"] = str(home)
    os.environ.setdefault("MESFLOW_AGENT_RUNTIME", "linux")
    os.environ["MESFLOW_AGENT_ADMIN_PASSWORD"] = "TestPw1234567"
    os.environ["SERVER_ROLE"] = "DEV"
    spec = importlib.util.spec_from_file_location(f"agent_p5_incidents_{uuid.uuid4().hex[:6]}", ROOT / "agent.py")
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


def test_fleet_summary_tags_incident_with_server_identity(dev_agent):
    """Section 33/34: a fingerprint like CONTAINER_DOWN:mesflow-postgres,
    once surfaced through the Fleet view, must be attributable to a
    specific registered server (id/name/environment) -- never ambiguous."""
    client = _client(dev_agent)
    condition = ("POSTGRES_UNAVAILABLE:mesflow-postgres", "CONTAINER_DOWN", "CRITICAL", "docker", "dev-host", "mesflow-postgres",
                 "PostgreSQL container is not running", "state=exited", {})
    dev_agent.sync_incidents([condition])

    r = client.get("/api/fleet/summary")
    assert r.status_code == 200
    body = r.get_json()
    assert body["counts"]["active_incidents"] == 1
    incident = body["active_incidents"][0]
    assert incident["server_id"] == "local"
    assert incident["environment"] == "DEV"
    assert incident["incident_type"] == "CONTAINER_DOWN"
    assert incident["severity"] == "CRITICAL"

    # section 86: recovery reconciles centrally too -- once the condition
    # clears, the fleet summary's active_incidents count drops back to 0.
    dev_agent.sync_incidents([])
    r2 = client.get("/api/fleet/summary")
    assert r2.get_json()["counts"]["active_incidents"] == 0


def test_server_scoped_incidents_route_matches_fleet_aggregation(dev_agent):
    client = _client(dev_agent)
    condition = ("QA_DOWN", "SERVICE_DOWN", "HIGH", "health-monitor", "dev-host", "qa-center",
                 "QA Center unreachable", "online=False", {})
    dev_agent.sync_incidents([condition])

    scoped = client.get("/api/servers/local/incidents").get_json()
    assert scoped["server_id"] == "local"
    assert any(i["incident_type"] == "SERVICE_DOWN" for i in scoped["items"])

    fleet = client.get("/api/fleet/summary").get_json()
    assert any(i["server_id"] == "local" and i["incident_type"] == "SERVICE_DOWN" for i in fleet["active_incidents"])


def test_two_registered_servers_incidents_never_cross_contaminate(dev_agent):
    """A second registered server (here: a second 'local'-style record
    pointed at a distinct, unregistered agent_url so it never actually
    resolves) must never show the local server's incidents, and vice
    versa -- proving the aggregation is genuinely per-server, not global."""
    client = _client(dev_agent)
    dev_agent.sync_incidents([("MESFLOW_DOWN", "SERVICE_DOWN", "CRITICAL", "health-monitor", "dev-host", "mesflow",
                                "MESFlow unreachable", "api_online=False", {})])

    other = client.post("/api/servers", json={"name": "other-node", "environment": "TEST",
        "agent_url": "http://127.0.0.1:1", "agent_user": "admin", "agent_password": "x"}, headers=CSRF).get_json()["item"]

    local_incidents = client.get("/api/servers/local/incidents").get_json()["items"]
    assert any(i["incident_type"] == "SERVICE_DOWN" for i in local_incidents)

    # the other (unreachable, never handshaken) server's incident view must
    # fail cleanly -- never silently return the LOCAL server's incidents.
    other_incidents = client.get(f"/api/servers/{other['server_id']}/incidents")
    assert other_incidents.status_code in (200, 502)
    if other_incidents.status_code == 200:
        assert other_incidents.get_json()["items"] == []
