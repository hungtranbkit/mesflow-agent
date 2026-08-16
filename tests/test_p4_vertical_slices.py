"""P4 Backup/DB/Storage Center -- the four required vertical slices, in the
spec's own explicit priority order:

  1. Manual PostgreSQL Backup
  2. Backup Retention
  3. Restore Drill
  4. Storage Pressure

Each test drives the real Flask routes end-to-end against a throwaway,
self-managed PostgreSQL container (never mesflow-postgres), proving the
full observe -> diagnose -> backup -> verify -> retain -> restore/recover
-> audit flow this task set out to build.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

DOCKER_AVAILABLE = shutil.which("docker") is not None


def _docker(*args, timeout=30):
    return subprocess.run(["docker", *args], capture_output=True, timeout=timeout)


@pytest.fixture(scope="module")
def throwaway_container():
    if not DOCKER_AVAILABLE:
        pytest.skip("docker not available in this environment")
    name = f"p4-pytest-slices-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name,
         "-e", "POSTGRES_USER=mesflow", "-e", "POSTGRES_PASSWORD=testpw123", "-e", "POSTGRES_DB=mesflow",
         "-p", "15833:5432", "postgres:17-alpine"],
        capture_output=True, timeout=60,
    )
    if run.returncode != 0:
        pytest.skip(f"could not start throwaway postgres: {run.stderr.decode(errors='replace')}")
    try:
        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            if _docker("exec", name, "pg_isready", "-U", "mesflow").returncode == 0:
                ready = True
                break
            time.sleep(1)
        if not ready:
            pytest.skip("throwaway postgres never became ready")
        seed = subprocess.run(
            ["docker", "exec", "-i", name, "sh", "-lc", "PGPASSWORD=testpw123 psql -U mesflow -d mesflow -v ON_ERROR_STOP=1"],
            input=(
                b"CREATE TABLE employees(id serial primary key, name text);"
                b"INSERT INTO employees(name) VALUES ('Test Worker 1'),('Test Worker 2'),('Test Worker 3');"
                b"CREATE TABLE alembic_version(version_num varchar(32) primary key);"
                b"INSERT INTO alembic_version VALUES ('0037_v72');"
            ),
            capture_output=True, timeout=20,
        )
        if seed.returncode != 0:
            pytest.skip(f"could not seed throwaway postgres: {seed.stderr.decode(errors='replace')}")
        yield name
    finally:
        subprocess.run(["docker", "stop", "-t", "3", name], capture_output=True, timeout=20)


@pytest.fixture()
def agent(throwaway_container, tmp_path):
    import os
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["WORKSHOP_AGENT_HOME"] = str(tmp_path)
    os.environ.setdefault("MESFLOW_AGENT_RUNTIME", "linux")
    os.environ.setdefault("MESFLOW_AGENT_ADMIN_PASSWORD", "TestPw1234567")
    os.environ["SERVER_ROLE"] = "PRODUCTION_TEST"  # -> TEST environment label
    os.environ["MESFLOW_POSTGRES_CONTAINER"] = throwaway_container
    os.environ.pop("MESFLOW_TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("MESFLOW_SMTP_HOST", None)
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(f"agent_p4_slices_{uuid.uuid4().hex[:6]}", root / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod.app.testing = True
    return mod


def _client(agent):
    client = agent.app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "test-csrf"
        sess["username"] = "test-admin"
    return client


CSRF = {"X-CSRF-Token": "test-csrf"}


def test_vertical_slice_1_manual_postgres_backup(agent):
    """Observe -> Backup -> Verify -> Audit: a human triggers a manual
    backup; it completes, checksums, and is immediately verified via a
    real pg_restore -l manifest check -- not just a size check."""
    client = _client(agent)

    # 1. Observe: DB Center summary reachable before anything else happens.
    summary = client.get("/api/ops/db/summary").get_json()
    assert summary["ok"] is True and summary["available"] is True

    # 2. Backup: a real pg_dump -Fc against the throwaway target.
    r = client.post("/api/ops/db-backups", json={"reason": "vertical slice 1"}, headers=CSRF)
    assert r.status_code == 200
    item = r.get_json()["item"]
    assert item["status"] == "SUCCESS"
    assert item["type"] == "pg_dump_custom"
    assert item["size_bytes"] > 100
    assert item["sha256"]

    # 3. Verify: BACKUP_SUCCESS and BACKUP_VERIFIED are distinct, both true here.
    assert item["verification_status"] == "VERIFIED"
    assert item["manifest_ok"] is True
    # RESTORE_TESTED is a third, separate state -- not yet true at this point.
    assert item["restore_tested"] is False

    # 4. Audit: every step left a structured trail.
    audit_actions = [e["action"] for e in client.get("/api/ops/db-audit").get_json()["items"]]
    assert {"BACKUP_STARTED", "BACKUP_COMPLETED", "BACKUP_VERIFIED"} <= set(audit_actions)


def test_vertical_slice_2_backup_retention(agent):
    """Diagnose -> Retain: an older, unpinned, non-latest backup becomes
    eligible under a tightened policy and is removed; the pinned backup and
    the most recent verified backup are always protected, and nothing is
    deleted without an explicit confirm flag."""
    client = _client(agent)

    b1 = client.post("/api/ops/db-backups", headers=CSRF).get_json()["item"]
    time.sleep(1.1)
    b2 = client.post("/api/ops/db-backups", headers=CSRF).get_json()["item"]
    time.sleep(1.1)
    b3 = client.post("/api/ops/db-backups", headers=CSRF).get_json()["item"]
    client.post(f"/api/ops/db-backups/{b1['backup_id']}/pin", headers=CSRF)

    agent._db_retention_service.policy["manual"] = 1
    preview = client.get("/api/ops/storage/retention/preview").get_json()
    # b1 is pinned (protected), b3 is newest (protected) -- only b2 is eligible.
    assert {r["backup_id"] for r in preview["eligible"]} == {b2["backup_id"]}
    protected_ids = {r["backup_id"] for r in preview["protected"]}
    assert b1["backup_id"] in protected_ids and b3["backup_id"] in protected_ids

    # without confirm, nothing happens
    r_blocked = client.post("/api/ops/storage/retention/apply", headers=CSRF)
    assert r_blocked.status_code == 400
    assert len(client.get("/api/ops/db-backups").get_json()["items"]) == 3

    r = client.post("/api/ops/storage/retention/apply", json={"confirm": True, "reason": "slice 2"}, headers=CSRF)
    assert r.status_code == 200
    body = r.get_json()
    assert body["deleted_count"] == 1
    assert body["deleted"][0]["backup_id"] == b2["backup_id"]

    remaining_ids = {x["backup_id"] for x in client.get("/api/ops/db-backups").get_json()["items"]}
    assert remaining_ids == {b1["backup_id"], b3["backup_id"]}

    audit_actions = [e["action"] for e in client.get("/api/ops/db-audit").get_json()["items"]]
    assert "BACKUP_DELETED_BY_RETENTION" in audit_actions
    assert "RETENTION_APPLIED" in audit_actions


def test_vertical_slice_3_restore_drill(agent):
    """Restore/recover when explicitly requested -> Audit: a real restore
    into an isolated, generated-name temp database on the SAME (throwaway,
    LOCAL/TEST) Postgres server, validated, and cleaned up -- never a
    caller-supplied database name, never Production."""
    client = _client(agent)
    item = client.post("/api/ops/db-backups", headers=CSRF).get_json()["item"]
    assert item["restore_tested"] is False

    r = client.post(f"/api/ops/db-backups/{item['backup_id']}/restore-test", json={"confirm": True}, headers=CSRF)
    assert r.status_code == 200
    drill = r.get_json()["item"]
    assert drill["status"] == "SUCCESS"
    assert drill["temp_database"].startswith("mesflow_restore_drill_")
    assert drill["cleanup_ok"] is True
    assert drill["validation"]["passed"] is True
    assert drill["validation"]["table_count"] >= 2
    assert drill["validation"]["db_revision"] == "0037_v72"

    # BACKUP_SUCCESS / BACKUP_VERIFIED / RESTORE_TESTED are three distinct
    # states -- restore_tested only flips after a real successful drill.
    detail = client.get(f"/api/ops/db-backups/{item['backup_id']}").get_json()["item"]
    assert detail["status"] == "SUCCESS"
    assert detail["verification_status"] == "VERIFIED"
    assert detail["restore_tested"] is True

    audit_actions = [e["action"] for e in client.get("/api/ops/db-audit").get_json()["items"]]
    assert "RESTORE_DRILL_STARTED" in audit_actions
    assert "RESTORE_DRILL_COMPLETED" in audit_actions


def test_vertical_slice_4_storage_pressure(agent):
    """Observe -> Diagnose -> Relieve: the Backups category grows as
    backups accumulate, Storage Center reports it accurately, retention
    identifies reclaimable bytes, and applying it measurably shrinks the
    category -- a real observe/diagnose/retain loop, not just a static
    number. Filesystem-level OK/WARNING/CRITICAL classification is proven
    separately (pure threshold function, tests/test_p4_db_backup.py)."""
    client = _client(agent)

    before = client.get("/api/ops/storage").get_json()
    assert before["ok"] is True
    before_bytes = before["categories"]["backups"]["bytes"]

    older = client.post("/api/ops/db-backups", headers=CSRF).get_json()["item"]
    time.sleep(1.1)
    client.post("/api/ops/db-backups", headers=CSRF)  # newer, protected by retention

    grown = client.get("/api/ops/storage").get_json()
    grown_bytes = grown["categories"]["backups"]["bytes"]
    assert grown_bytes > before_bytes, "Backups category must visibly grow as backups accumulate"
    assert "pressure_level" in grown["filesystem"]
    assert grown["filesystem"]["pressure_level"] in ("OK", "WARNING", "CRITICAL")

    agent._db_retention_service.policy["manual"] = 1
    preview = client.get("/api/ops/storage/retention/preview").get_json()
    assert preview["eligible_bytes"] > 0
    assert preview["eligible_bytes"] == older["size_bytes"]

    client.post("/api/ops/storage/retention/apply", json={"confirm": True}, headers=CSRF)
    relieved = client.get("/api/ops/storage").get_json()
    relieved_bytes = relieved["categories"]["backups"]["bytes"]
    assert relieved_bytes < grown_bytes, "applying retention must measurably relieve category pressure"
