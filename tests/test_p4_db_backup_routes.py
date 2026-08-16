"""P4 Backup/DB/Storage Center -- Flask-route-level wiring against a real
(isolated) Agent instance, pointed at a throwaway PostgreSQL container
(never mesflow-postgres). Complements test_p4_db_backup.py, which covers
the service classes directly without Flask.
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
    name = f"p4-pytest-routes-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name,
         "-e", "POSTGRES_USER=mesflow", "-e", "POSTGRES_PASSWORD=testpw123", "-e", "POSTGRES_DB=mesflow",
         "-p", "15733:5432", "postgres:17-alpine"],
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
                b"INSERT INTO employees(name) VALUES ('Test Worker 1'),('Test Worker 2');"
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
    os.environ["SERVER_ROLE"] = "PRODUCTION_TEST"  # -> TEST environment label, restore drills allowed
    os.environ["MESFLOW_POSTGRES_CONTAINER"] = throwaway_container
    os.environ.pop("MESFLOW_TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("MESFLOW_SMTP_HOST", None)
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(f"agent_p4_routes_{uuid.uuid4().hex[:6]}", root / "agent.py")
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


CSRF_HEADER = {"X-CSRF-Token": "test-csrf"}


def test_db_summary_requires_auth(agent):
    client = agent.app.test_client()
    assert client.get("/api/ops/db/summary").status_code == 401


def test_db_summary_reports_real_target(agent):
    client = _client(agent)
    r = client.get("/api/ops/db/summary")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["available"] is True
    assert body["db_revision"] == "0037_v72"
    assert body["database_size_bytes"] > 0
    assert "fastest_growing_tables" in body


def test_create_backup_requires_login_and_csrf(agent):
    client = agent.app.test_client()
    r = client.post("/api/ops/db-backups")
    assert r.status_code in (302, 401)


def test_create_backup_end_to_end(agent):
    client = _client(agent)
    r = client.post("/api/ops/db-backups", json={"reason": "route test"}, headers=CSRF_HEADER)
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    item = body["item"]
    assert item["status"] == "SUCCESS"
    assert item["verification_status"] == "VERIFIED"
    assert item["db_revision"] == "0037_v72"

    # shows up in the list
    listed = client.get("/api/ops/db-backups").get_json()["items"]
    assert any(x["backup_id"] == item["backup_id"] for x in listed)

    # detail route
    detail = client.get(f"/api/ops/db-backups/{item['backup_id']}").get_json()
    assert detail["item"]["backup_id"] == item["backup_id"]


def test_create_backup_rejects_second_concurrent_run(agent):
    client = _client(agent)
    store = agent._db_backup_service._load()
    store["items"]["fake-running"] = {
        "backup_id": "fake-running", "status": "RUNNING", "created_at": "2020-01-01T00:00:00+00:00",
        "retention_class": "manual", "pinned": False, "verification_status": "NOT_VERIFIED",
        "storage_path": "", "sha256": "", "size_bytes": None,
    }
    agent._db_backup_service._save(store)
    r = client.post("/api/ops/db-backups", headers=CSRF_HEADER)
    assert r.status_code == 409
    assert r.get_json()["error"] == "BACKUP_ALREADY_RUNNING"


def test_pin_unpin_roundtrip_via_routes(agent):
    client = _client(agent)
    item = client.post("/api/ops/db-backups", headers=CSRF_HEADER).get_json()["item"]
    r1 = client.post(f"/api/ops/db-backups/{item['backup_id']}/pin", headers=CSRF_HEADER)
    assert r1.get_json()["item"]["pinned"] is True
    r2 = client.post(f"/api/ops/db-backups/{item['backup_id']}/unpin", headers=CSRF_HEADER)
    assert r2.get_json()["item"]["pinned"] is False


def test_restore_test_full_lifecycle_and_marks_restore_tested(agent):
    client = _client(agent)
    item = client.post("/api/ops/db-backups", headers=CSRF_HEADER).get_json()["item"]
    # missing confirm -> rejected
    r_no_confirm = client.post(f"/api/ops/db-backups/{item['backup_id']}/restore-test", headers=CSRF_HEADER)
    assert r_no_confirm.status_code == 400
    assert r_no_confirm.get_json()["error"] == "CONFIRMATION_REQUIRED"

    r = client.post(f"/api/ops/db-backups/{item['backup_id']}/restore-test", json={"confirm": True}, headers=CSRF_HEADER)
    assert r.status_code == 200
    drill = r.get_json()["item"]
    assert drill["status"] == "SUCCESS"
    assert drill["cleanup_ok"] is True

    detail = client.get(f"/api/ops/db-backups/{item['backup_id']}").get_json()["item"]
    assert detail["restore_tested"] is True

    drills = client.get("/api/ops/restore-drills").get_json()["items"]
    assert any(d["drill_id"] == drill["drill_id"] for d in drills)


def test_restore_test_blocked_in_production(throwaway_container, tmp_path):
    import os
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["WORKSHOP_AGENT_HOME"] = str(tmp_path)
    os.environ.setdefault("MESFLOW_AGENT_RUNTIME", "linux")
    os.environ.setdefault("MESFLOW_AGENT_ADMIN_PASSWORD", "TestPw1234567")
    os.environ["SERVER_ROLE"] = "PRODUCTION"  # -> PRODUCTION environment label
    os.environ["MESFLOW_POSTGRES_CONTAINER"] = throwaway_container
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(f"agent_p4_prod_{uuid.uuid4().hex[:6]}", root / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod.app.testing = True
    client = mod.app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "test-csrf"

    item = client.post("/api/ops/db-backups", headers=CSRF_HEADER).get_json()["item"]
    r = client.post(f"/api/ops/db-backups/{item['backup_id']}/restore-test", json={"confirm": True}, headers=CSRF_HEADER)
    assert r.status_code == 403
    assert r.get_json()["error"] == "RESTORE_DRILL_BLOCKED_IN_PRODUCTION"


def test_retention_preview_and_apply_via_routes(agent):
    client = _client(agent)
    older = client.post("/api/ops/db-backups", headers=CSRF_HEADER).get_json()["item"]
    time.sleep(1.1)
    newer = client.post("/api/ops/db-backups", headers=CSRF_HEADER).get_json()["item"]

    preview = client.get("/api/ops/storage/retention/preview").get_json()
    assert preview["ok"] is True
    # default policy keeps 20 manual backups -- nothing eligible yet
    assert preview["eligible_count"] == 0

    # tighten the policy in-process to actually exercise deletion
    agent._db_retention_service.policy["manual"] = 1
    preview2 = client.get("/api/ops/storage/retention/preview").get_json()
    assert {r["backup_id"] for r in preview2["eligible"]} == {older["backup_id"]}

    # missing confirm -> rejected, nothing deleted
    r_no_confirm = client.post("/api/ops/storage/retention/apply", headers=CSRF_HEADER)
    assert r_no_confirm.status_code == 400

    r = client.post("/api/ops/storage/retention/apply", json={"confirm": True, "reason": "route test"}, headers=CSRF_HEADER)
    assert r.status_code == 200
    body = r.get_json()
    assert body["deleted_count"] == 1
    assert body["deleted"][0]["backup_id"] == older["backup_id"]

    remaining = client.get("/api/ops/db-backups").get_json()["items"]
    assert {x["backup_id"] for x in remaining} == {newer["backup_id"]}


def test_storage_route_reports_categories(agent):
    client = _client(agent)
    client.post("/api/ops/db-backups", headers=CSRF_HEADER)  # ensure the backups category is non-empty
    r = client.get("/api/ops/storage")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert "filesystem" in body and "categories" in body
    assert body["categories"]["backups"]["exists"] is True
    assert body["categories"]["backups"]["bytes"] > 0


def test_db_audit_trail_records_backup_lifecycle(agent):
    client = _client(agent)
    item = client.post("/api/ops/db-backups", headers=CSRF_HEADER).get_json()["item"]
    client.post(f"/api/ops/db-backups/{item['backup_id']}/pin", headers=CSRF_HEADER)
    r = client.get("/api/ops/db-audit")
    assert r.status_code == 200
    actions = [e["action"] for e in r.get_json()["items"]]
    assert "BACKUP_STARTED" in actions
    assert "BACKUP_COMPLETED" in actions
    assert "BACKUP_VERIFIED" in actions
    assert "BACKUP_PINNED" in actions
    # most recent first
    assert r.get_json()["items"][0]["action"] == "BACKUP_PINNED"
