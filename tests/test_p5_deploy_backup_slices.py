"""P5 Multi-server -- vertical slice 2 (Remote TEST deployment) and
vertical slice 3 (Remote backup), against a REAL second `agent.py` process
plus (for the backup slice) a real throwaway PostgreSQL container. Also
covers section 83/89: deployment routing and Production-approval gating.

SAFETY NOTE (read before touching the deploy tests in this file): an
earlier version of this file called the real MESFlow /upload endpoint
against a spawned "throwaway" remote agent using the real, locally-built
71.0.0.4 release artifact. That endpoint ALWAYS auto-triggers a real local
deploy job, which runs real docker-compose operations against container
names fixed by the release's own compose.yml (e.g. "mesflow-app").
Because the spawned remote agent shares THIS HOST's Docker daemon,
container names are global -- the "throwaway" deploy actually stopped the
REAL production mesflow-app container (see reports/P5_MULTI_SERVER.md,
"Safety incident"). This was caught, mesflow-app was restarted and
confirmed healthy, and a permanent code-level guard
(LOOPBACK_DEPLOY_REFUSED in agent.py's _deploy_release_to_server) now
refuses any deploy whose target resolves to this same host unless
MESFLOW_ALLOW_LOOPBACK_DEPLOY=1 is explicitly set -- which this test suite
deliberately never sets. The test below verifies that guard rather than
exercising a real deploy; end-to-end real-deploy verification requires
genuinely separate hosts (or non-colliding container names), which this
sandbox does not have.
"""
from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from conftest import ADMIN_PASSWORD, spawn_agent, stop_agent

ROOT = Path(__file__).resolve().parents[1]
REAL_ARTIFACT = ROOT.parent / "artifacts" / "releases" / "71.0.0.4" / "MESFlow_71.0.0.4.deploy.zip"
DOCKER_AVAILABLE = shutil.which("docker") is not None


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
    os.environ.pop("MESFLOW_PRODUCTION_PROMOTE_ENABLED", None)
    # Backups (never deploys -- see the module safety note) are explicitly
    # allowed to loopback targets in this file only, and only because
    # conftest.spawn_agent() independently forces every spawned remote's
    # MESFLOW_POSTGRES_CONTAINER to a safe, provably nonexistent placeholder
    # unless a test opts a specific remote into a real throwaway container.
    os.environ["MESFLOW_ALLOW_LOOPBACK_BACKUP"] = "1"
    os.environ.pop("MESFLOW_ALLOW_LOOPBACK_DEPLOY", None)
    spec = importlib.util.spec_from_file_location(f"agent_p5_dev2_{uuid.uuid4().hex[:6]}", ROOT / "agent.py")
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
    proc, base_url = spawn_agent(tmp_path_factory.mktemp("remote_test2"), port=18192, server_role="PRODUCTION_TEST")
    try:
        yield base_url
    finally:
        stop_agent(proc)


def _register_test_server(client, remote_url, name="mesflow-test"):
    reg = client.post("/api/servers", json={"name": name, "environment": "TEST",
        "agent_url": remote_url, "agent_user": "admin", "agent_password": ADMIN_PASSWORD}, headers=CSRF).get_json()["item"]
    client.post(f"/api/servers/{reg['server_id']}/test-connection", headers=CSRF)
    return reg["server_id"]


# -- Vertical slice 2: Remote TEST deployment ----------------------------
#
# Real end-to-end artifact-transfer + digest-match evidence for this slice
# was captured ONCE, before the loopback-deploy risk above was understood,
# and DID show a correct byte-identical digest match (local_sha256 ==
# remote_sha256 == the real 71.0.0.4 artifact's sha256) -- but is not
# reproduced here because doing so again would re-trigger the same real
# deploy against this shared-daemon host. What IS safely and repeatedly
# verified below is every piece of control-plane logic around that
# transfer: server_id resolution (never a raw URL), the artifact/digest
# self-check, and -- the direct product of this incident -- the new
# same-host safety guard that now refuses the dangerous call outright.

def test_deploy_to_loopback_target_is_refused_by_the_safety_guard(dev_agent, remote_test_server):
    """Direct regression test for the incident: a registered server whose
    agent_url resolves to this same host must be refused for deploy,
    never silently attempted -- MESFLOW_ALLOW_LOOPBACK_DEPLOY is NOT set
    in this test process, matching a real operator's default posture."""
    import os
    assert os.environ.get("MESFLOW_ALLOW_LOOPBACK_DEPLOY") is None
    client = _client(dev_agent)
    sid = _register_test_server(client, remote_test_server)
    r = client.post(f"/api/servers/{sid}/deploy", json={"version": "71.0.0.4"}, headers=CSRF)
    assert r.status_code == 403
    assert "LOOPBACK_DEPLOY_REFUSED" in r.get_json().get("error", "")
    # refused before ever resolving/reading the local artifact or opening
    # a remote session -- no REMOTE_DEPLOY_STARTED audit entry exists.
    audit_actions = [e["action"] for e in client.get("/api/ops/db-audit").get_json()["items"]]
    assert "REMOTE_DEPLOY_STARTED" not in audit_actions


@pytest.mark.skipif(not REAL_ARTIFACT.is_file(), reason="no real built release artifact available in this environment")
def test_deploy_artifact_self_check_uses_real_release_digest(dev_agent):
    """The pre-flight digest self-check (artifact resolution + sha256,
    section 21) is real, local-only logic -- verified directly, without
    any remote call, so it's exercised on every run regardless of the
    loopback guard above."""
    artifact = dev_agent._artifact_for_release("71.0.0.4")
    assert artifact["package"] and Path(artifact["package"]).is_file()
    local_sha = hashlib.sha256(REAL_ARTIFACT.read_bytes()).hexdigest()
    assert artifact["package_sha256"] == local_sha


def test_deploy_missing_version_rejected(dev_agent, remote_test_server):
    client = _client(dev_agent)
    sid = _register_test_server(client, remote_test_server)
    r = client.post(f"/api/servers/{sid}/deploy", json={}, headers=CSRF)
    assert r.status_code == 400


def test_deploy_to_unregistered_server_not_found(dev_agent):
    client = _client(dev_agent)
    r = client.post("/api/servers/does-not-exist/deploy", json={"version": "1.0.0"}, headers=CSRF)
    assert r.status_code == 404


# -- Section 89: Production requires explicit approval -------------------

def test_production_deploy_blocked_without_approval(dev_agent, remote_test_server):
    client = _client(dev_agent)
    reg = client.post("/api/servers", json={"name": "mesflow-prod", "environment": "PRODUCTION",
        "agent_url": remote_test_server, "agent_user": "admin", "agent_password": ADMIN_PASSWORD,
        "production_confirmed": True}, headers=CSRF).get_json()["item"]
    sid = reg["server_id"]
    # ACTIVE first, so the assertion below isolates the production-approval
    # gate specifically, not an incidental PENDING/registration-status one.
    client.post(f"/api/servers/{sid}/test-connection", headers=CSRF)

    # No MESFLOW_PRODUCTION_PROMOTE_ENABLED, no confirm -> blocked outright,
    # before any artifact is even resolved or any remote call attempted.
    r = client.post(f"/api/servers/{sid}/deploy", json={"version": "71.0.0.4"}, headers=CSRF)
    assert r.status_code == 403
    assert "PRODUCTION_DEPLOY_NOT_APPROVED" in r.get_json().get("error", "")


def test_production_backup_blocked_without_approval(dev_agent, remote_test_server):
    client = _client(dev_agent)
    reg = client.post("/api/servers", json={"name": "mesflow-prod-2", "environment": "PRODUCTION",
        "agent_url": remote_test_server, "agent_user": "admin", "agent_password": ADMIN_PASSWORD,
        "production_confirmed": True}, headers=CSRF).get_json()["item"]
    sid = reg["server_id"]
    r = client.post(f"/api/servers/{sid}/backups", json={}, headers=CSRF)
    assert r.status_code == 403
    assert r.get_json()["error"] == "PRODUCTION_BACKUP_NOT_APPROVED"


def test_test_environment_deploy_never_requires_production_flags(dev_agent, remote_test_server):
    """Section 72: a request cannot pretend an environment override -- the
    server was registered as TEST, so deploy/backup proceed WITHOUT the
    production gate, regardless of anything in the request body."""
    client = _client(dev_agent)
    sid = _register_test_server(client, remote_test_server, name="mesflow-test-noflag")
    r = client.post(f"/api/servers/{sid}/backups", json={"environment": "production"}, headers=CSRF)
    # environment field in the body is ignored -- registry says TEST, so
    # this must not 403 on a production gate.
    assert r.status_code != 403


# -- Vertical slice 4: PRODUCTION registered read-only ------------------

def test_production_server_read_only_paths_all_work(dev_agent, remote_test_server):
    """Section 93/94 acceptance matrix: a PRODUCTION-registered server's
    health/storage/incidents/backup-inventory (all read-only) must work
    normally -- only MUTATIONS (deploy, backup-create, restore, retention)
    are gated. Registering as PRODUCTION does not mean "inaccessible"."""
    client = _client(dev_agent)
    reg = client.post("/api/servers", json={"name": "mesflow-prod-readonly", "environment": "PRODUCTION",
        "agent_url": remote_test_server, "agent_user": "admin", "agent_password": ADMIN_PASSWORD,
        "production_confirmed": True}, headers=CSRF).get_json()["item"]
    sid = reg["server_id"]

    conn = client.post(f"/api/servers/{sid}/test-connection", headers=CSRF)
    assert conn.status_code == 200
    assert conn.get_json()["item"]["registration_status"] == "ACTIVE"
    # Registration state and health state are deliberately separate
    # (section 11) -- a successful handshake alone does not set
    # health_status; that's the poll cycle's job.
    dev_agent._fleet_health_service.poll_once()

    health = client.get(f"/api/servers/{sid}/health")
    assert health.status_code == 200
    assert health.get_json()["item"]["health_status"] in ("ONLINE", "DEGRADED")

    storage = client.get(f"/api/servers/{sid}/storage")
    assert storage.status_code == 200
    assert storage.get_json()["ok"] is True

    incidents = client.get(f"/api/servers/{sid}/incidents")
    assert incidents.status_code == 200
    assert incidents.get_json()["server_id"] == sid

    backups = client.get(f"/api/servers/{sid}/backups")
    assert backups.status_code == 200
    assert backups.get_json()["server_id"] == sid

    fleet = client.get("/api/fleet/summary").get_json()
    assert fleet["counts"]["production"] == 1

    # ...but every mutation remains gated (re-confirms sections 22/72 in
    # the same test that proves read access, so the two properties are
    # verified together against the SAME registered PRODUCTION server).
    deploy = client.post(f"/api/servers/{sid}/deploy", json={"version": "71.0.0.4"}, headers=CSRF)
    assert deploy.status_code == 403
    backup = client.post(f"/api/servers/{sid}/backups", json={}, headers=CSRF)
    assert backup.status_code == 403


# -- Vertical slice 3: Remote backup (real throwaway Postgres) ----------

def _docker(*args, timeout=30):
    return subprocess.run(["docker", *args], capture_output=True, timeout=timeout)


@pytest.fixture(scope="module")
def remote_test_server_with_postgres(tmp_path_factory):
    if not DOCKER_AVAILABLE:
        pytest.skip("docker not available in this environment")
    pg_name = f"p5-pytest-postgres-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", pg_name,
         "-e", "POSTGRES_USER=mesflow", "-e", "POSTGRES_PASSWORD=testpw123", "-e", "POSTGRES_DB=mesflow",
         "-p", "15933:5432", "postgres:17-alpine"], capture_output=True, timeout=60)
    if run.returncode != 0:
        pytest.skip(f"could not start throwaway postgres: {run.stderr.decode(errors='replace')}")
    try:
        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            if _docker("exec", pg_name, "pg_isready", "-U", "mesflow").returncode == 0:
                ready = True
                break
            time.sleep(1)
        if not ready:
            pytest.skip("throwaway postgres never became ready")
        seed = subprocess.run(
            ["docker", "exec", "-i", pg_name, "sh", "-lc", "PGPASSWORD=testpw123 psql -U mesflow -d mesflow -v ON_ERROR_STOP=1"],
            input=b"CREATE TABLE employees(id serial primary key, name text);"
                  b"INSERT INTO employees(name) VALUES ('Test Worker');"
                  b"CREATE TABLE alembic_version(version_num varchar(32) primary key);"
                  b"INSERT INTO alembic_version VALUES ('0037_v72');",
            capture_output=True, timeout=20)
        if seed.returncode != 0:
            pytest.skip(f"could not seed throwaway postgres: {seed.stderr.decode(errors='replace')}")

        proc, base_url = spawn_agent(tmp_path_factory.mktemp("remote_test_pg"), port=18193, server_role="PRODUCTION_TEST",
                                      extra_env={"MESFLOW_POSTGRES_CONTAINER": pg_name})
        try:
            yield base_url
        finally:
            stop_agent(proc)
    finally:
        subprocess.run(["docker", "stop", "-t", "3", pg_name], capture_output=True, timeout=20)


def test_remote_backup_full_lifecycle(dev_agent, remote_test_server_with_postgres):
    """Section 92: DEV UI -> TEST server -> Remote Agent backup -> verify
    -> central metadata/history, with real server ownership preserved."""
    client = _client(dev_agent)
    sid = _register_test_server(client, remote_test_server_with_postgres, name="mesflow-test-pg")

    r = client.post(f"/api/servers/{sid}/backups", json={"reason": "P5 vertical slice 3"}, headers=CSRF)
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    item = body["item"]
    assert item["status"] == "SUCCESS"
    assert item["verification_status"] == "VERIFIED"
    assert item["sha256"]

    # central metadata/history: DEV's own view of that server's backups
    # lists it, preserving server ownership (section 26/27).
    listed = client.get(f"/api/servers/{sid}/backups").get_json()
    assert listed["server_id"] == sid
    assert any(b["backup_id"] == item["backup_id"] for b in listed["items"])

    audit_actions = [e["action"] for e in client.get("/api/ops/db-audit").get_json()["items"]]
    assert "REMOTE_BACKUP_STARTED" in audit_actions
    assert "REMOTE_BACKUP_COMPLETED" in audit_actions


def test_remote_backup_routes_to_correct_server_only(dev_agent, remote_test_server, remote_test_server_with_postgres):
    """Section 85: a backup request for server A (the one WITH postgres)
    must never reach server B (the one without) -- and vice versa. Reuses
    the SAME two already-running remote fixtures from earlier in this
    module (no new process spawn) -- only the routing/ownership property
    is new here; GET-level cross-server isolation for storage/backups/
    incidents is already proven in test_p5_fleet_routes.py::
    test_server_routing_reaches_only_the_target_server."""
    client = _client(dev_agent)
    sid_a = _register_test_server(client, remote_test_server_with_postgres, name="server-with-pg")
    sid_b = _register_test_server(client, remote_test_server, name="server-without-pg")

    rb = client.post(f"/api/servers/{sid_b}/backups", json={}, headers=CSRF)
    # server B has no real postgres container configured -- its own local
    # backup attempt fails as server B's OWN failure, never silently
    # succeeding using server A's data. Two failure shapes are both
    # acceptable evidence of that: a routed-but-failed backup (server_id
    # present, not SUCCESS) or a remote-communication failure (no item at
    # all) -- what must NEVER happen is server A's data coming back.
    body_b = rb.get_json()
    assert body_b.get("ok") is False
    if body_b.get("server_id") is not None:
        assert body_b["server_id"] == sid_b
        assert body_b.get("item", {}).get("status") != "SUCCESS"
    else:
        assert body_b.get("error") in ("REMOTE_AGENT_UNREACHABLE", "REMOTE_TIMEOUT", "REMOTE_OPERATION_FAILED")

    listed_a = client.get(f"/api/servers/{sid_a}/backups").get_json()["items"]
    listed_b = client.get(f"/api/servers/{sid_b}/backups").get_json()["items"]
    ids_a = {b["backup_id"] for b in listed_a}
    ids_b = {b["backup_id"] for b in listed_b}
    assert ids_a.isdisjoint(ids_b)
