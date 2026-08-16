"""P4 Backup/DB/Storage Center -- BackupService/RetentionService/
RestoreDrillService/StorageService.

These tests run against a throwaway, self-managed PostgreSQL container
(never mesflow-postgres) -- the same "ephemeral test container" pattern
used elsewhere in this project. The whole module is skipped, not failed,
in any environment without docker (e.g. some CI runners), matching how
other docker-dependent suites in this repo behave.
"""
from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from agent_backend.db_backup import (
    BackupService,
    ConflictError,
    PostgresTarget,
    RestoreDrillError,
    RestoreDrillService,
    RetentionService,
    StorageService,
    _bytes_to_human,
    load_json,
    postgres_summary,
    save_json,
    storage_pressure_level,
)

DOCKER_AVAILABLE = shutil.which("docker") is not None


def _docker(*args, timeout=30, input_bytes=None):
    return subprocess.run(["docker", *args], capture_output=True, timeout=timeout, input=input_bytes)


@pytest.fixture(scope="module")
def pg_target():
    if not DOCKER_AVAILABLE:
        pytest.skip("docker not available in this environment")
    name = f"p4-pytest-postgres-{uuid.uuid4().hex[:8]}"
    port = 15533
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name,
         "-e", "POSTGRES_USER=mesflow", "-e", "POSTGRES_PASSWORD=testpw123", "-e", "POSTGRES_DB=mesflow",
         "-p", f"{port}:5432", "postgres:17-alpine"],
        capture_output=True, timeout=60,
    )
    if run.returncode != 0:
        pytest.skip(f"could not start throwaway postgres container: {run.stderr.decode(errors='replace')}")
    target = PostgresTarget(container=name)
    try:
        # wait for readiness
        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            probe = _docker("exec", name, "pg_isready", "-U", "mesflow")
            if probe.returncode == 0:
                ready = True
                break
            time.sleep(1)
        if not ready:
            pytest.skip("throwaway postgres container never became ready")
        seed_sql = (
            "CREATE TABLE employees(id serial primary key, name text, created_at timestamptz default now());"
            "INSERT INTO employees(name) VALUES ('Test Worker 1'),('Test Worker 2'),('Test Worker 3');"
            "CREATE TABLE alembic_version(version_num varchar(32) primary key);"
            "INSERT INTO alembic_version VALUES('0037_v72');"
        )
        seed = _docker("exec", name, "sh", "-lc",
                        f'PGPASSWORD=testpw123 psql -U mesflow -d mesflow -v ON_ERROR_STOP=1 -c "{seed_sql}"')
        if seed.returncode != 0:
            pytest.skip(f"could not seed throwaway postgres: {seed.stderr.decode(errors='replace')}")
        yield target
    finally:
        subprocess.run(["docker", "stop", "-t", "3", name], capture_output=True, timeout=20)


@pytest.fixture()
def svc(pg_target, tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    audit_log = []
    service = BackupService(pg_target, backup_dir, tmp_path / "backups.json",
                             audit=lambda action, data: audit_log.append((action, data)))
    service._audit_log = audit_log  # exposed for assertions
    return service


# ---------------------------------------------------------------------
# Pure-logic tests (no docker required)
# ---------------------------------------------------------------------

def test_bytes_to_human():
    assert _bytes_to_human(0) == "0.0 B"
    assert _bytes_to_human(1024) == "1.0 KB"
    assert _bytes_to_human(1024 * 1024 * 3) == "3.0 MB"
    assert _bytes_to_human(None) == "0.0 B"


def test_load_json_missing_file_returns_default(tmp_path):
    assert load_json(tmp_path / "nope.json", {"x": 1}) == {"x": 1}


def test_save_json_then_load_json_roundtrip(tmp_path):
    path = tmp_path / "sub" / "data.json"
    save_json(path, {"a": [1, 2, 3]})
    assert load_json(path, None) == {"a": [1, 2, 3]}


def test_storage_service_category_sizes_and_missing_category(tmp_path):
    cat_dir = tmp_path / "backups"
    cat_dir.mkdir()
    (cat_dir / "one.bin").write_bytes(b"x" * 2048)
    (cat_dir / "sub").mkdir()
    (cat_dir / "sub" / "two.bin").write_bytes(b"y" * 1024)
    storage = StorageService(categories={"backups": cat_dir, "missing": tmp_path / "does-not-exist"})
    sizes = storage.category_sizes()
    assert sizes["backups"]["exists"] is True
    assert sizes["backups"]["bytes"] == 3072
    assert sizes["missing"]["exists"] is False
    assert sizes["missing"]["bytes"] == 0


def test_storage_service_filesystem_summary_shape(tmp_path):
    storage = StorageService(categories={})
    summary = storage.filesystem_summary(str(tmp_path))
    assert summary["total_bytes"] >= summary["used_bytes"] >= 0
    assert 0 <= summary["percent"] <= 100
    assert summary["pressure_level"] in ("OK", "WARNING", "CRITICAL")


def test_storage_pressure_level_thresholds():
    assert storage_pressure_level(0) == "OK"
    assert storage_pressure_level(79.9) == "OK"
    assert storage_pressure_level(80.0) == "WARNING"
    assert storage_pressure_level(89.9) == "WARNING"
    assert storage_pressure_level(90.0) == "CRITICAL"
    assert storage_pressure_level(100) == "CRITICAL"
    # custom thresholds
    assert storage_pressure_level(50, warning=40, critical=60) == "WARNING"


def test_retention_service_protects_last_verified_even_with_zero_policy():
    class FakeBackups:
        def list(self):
            return [{"backup_id": "only", "created_at": "2026-01-01T00:00:00+00:00",
                      "status": "SUCCESS", "verification_status": "VERIFIED", "pinned": False,
                      "retention_class": "manual", "size_bytes": 100}]

    retention = RetentionService(FakeBackups(), policy={"manual": 0})
    preview = retention.preview()
    assert preview["eligible_count"] == 0
    assert preview["protected"][0]["backup_id"] == "only"


def test_retention_service_protects_active_restore_source():
    class FakeBackups:
        def list(self):
            return [
                {"backup_id": "old", "created_at": "2026-01-01T00:00:00+00:00",
                 "status": "SUCCESS", "verification_status": "VERIFIED", "pinned": False,
                 "retention_class": "manual", "size_bytes": 100},
                {"backup_id": "newer", "created_at": "2026-01-02T00:00:00+00:00",
                 "status": "SUCCESS", "verification_status": "VERIFIED", "pinned": False,
                 "retention_class": "manual", "size_bytes": 100},
            ]

    retention = RetentionService(FakeBackups(), policy={"manual": 0})
    # without an active restore source, only "newer" (max created_at) is protected -> "old" is eligible
    preview_no_source = retention.preview()
    assert {r["backup_id"] for r in preview_no_source["eligible"]} == {"old"}

    # with "old" as the active restore source, it must also be protected
    preview_with_source = retention.preview(active_restore_source="old")
    assert preview_with_source["eligible_count"] == 0


# ---------------------------------------------------------------------
# Live tests against a real, throwaway Postgres container
# ---------------------------------------------------------------------

def test_create_backup_success_verified_atomic(svc):
    record = svc.create(retention_class="manual", triggered_by="pytest", reason="p4 unit test")
    assert record["status"] == "SUCCESS"
    assert record["verification_status"] == "VERIFIED"
    assert record["manifest_ok"] is True
    assert record["sha256"]
    assert record["size_bytes"] > 100
    assert record["db_revision"] == "0037_v72"
    path = Path(record["storage_path"])
    assert path.is_file()
    assert path.suffix == ".dump"
    assert not path.with_suffix(".dump.tmp").exists(), "tmp file must not remain after atomic rename"
    import hashlib
    assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]


def test_create_backup_audit_events_in_order(svc):
    svc.create(retention_class="manual")
    actions = [a for a, _ in svc._audit_log]
    assert actions == ["BACKUP_STARTED", "BACKUP_COMPLETED", "BACKUP_VERIFIED"]


def test_create_backup_rejects_unknown_retention_class(svc):
    with pytest.raises((ValueError, AssertionError)):
        svc.create(retention_class="not-a-real-class")


def test_conflict_error_when_backup_already_running(svc):
    store = svc._load()
    store["items"]["fake-running"] = {
        "backup_id": "fake-running", "status": "RUNNING", "created_at": "2020-01-01T00:00:00+00:00",
        "retention_class": "manual", "pinned": False, "verification_status": "NOT_VERIFIED",
        "storage_path": "", "sha256": "", "size_bytes": None,
    }
    svc._save(store)
    with pytest.raises(ConflictError):
        svc.create(retention_class="manual")


def test_verify_detects_checksum_mismatch_after_tampering(svc):
    record = svc.create(retention_class="manual")
    path = Path(record["storage_path"])
    original = path.read_bytes()
    path.write_bytes(original + b"\x00corruption")
    result = svc.verify(record["backup_id"])
    assert result["verification_status"] == "VERIFY_FAILED"
    # restore original bytes so nothing else in this test is affected
    path.write_bytes(original)


def test_pin_unpin_roundtrip(svc):
    record = svc.create(retention_class="manual")
    pinned = svc.pin(record["backup_id"], actor="pytest")
    assert pinned["pinned"] is True
    unpinned = svc.unpin(record["backup_id"], actor="pytest")
    assert unpinned["pinned"] is False


def test_list_sorted_newest_first(svc):
    r1 = svc.create(retention_class="manual")
    time.sleep(1.1)
    r2 = svc.create(retention_class="manual")
    items = svc.list()
    assert items[0]["backup_id"] == r2["backup_id"]
    assert items[1]["backup_id"] == r1["backup_id"]


def test_retention_apply_deletes_only_eligible_and_protects_newest(svc):
    older = svc.create(retention_class="manual", triggered_by="older")
    time.sleep(1.1)
    newer = svc.create(retention_class="manual", triggered_by="newer")
    retention = RetentionService(svc, policy={"manual": 1})
    result = retention.apply(actor="pytest")
    assert result["deleted_count"] == 1
    assert result["deleted"][0]["backup_id"] == older["backup_id"]
    assert not Path(older["storage_path"]).exists()
    assert Path(newer["storage_path"]).exists()
    remaining_ids = {r["backup_id"] for r in svc.list()}
    assert remaining_ids == {newer["backup_id"]}


def test_retention_apply_never_deletes_pinned_backup_even_at_zero_policy(svc):
    record = svc.create(retention_class="manual")
    svc.pin(record["backup_id"])
    retention = RetentionService(svc, policy={"manual": 0})
    result = retention.apply(actor="pytest")
    assert result["deleted_count"] == 0
    assert Path(record["storage_path"]).exists()


def test_restore_drill_success_validates_and_cleans_up(svc, pg_target):
    record = svc.create(retention_class="manual")
    drill_service = RestoreDrillService(pg_target, svc.store_path.with_name("drills.json"))
    drill = drill_service.run(record)
    assert drill["status"] == "SUCCESS"
    assert drill["cleanup_ok"] is True
    assert drill["validation"]["passed"] is True
    assert drill["validation"]["table_count"] >= 2
    assert drill["validation"]["db_revision"] == "0037_v72"
    assert drill["temp_database"].startswith("mesflow_restore_drill_")
    # the temp database must really be gone after cleanup
    check = _docker("exec", pg_target.container, "sh", "-lc",
                     f'PGPASSWORD=testpw123 psql -U mesflow -d mesflow -Atqc '
                     f'"SELECT 1 FROM pg_database WHERE datname=\'{drill["temp_database"]}\';"')
    assert check.stdout.decode().strip() == ""


def test_restore_drill_rejects_unverified_backup(svc, pg_target):
    record = svc.create(retention_class="manual")
    store = svc._load()
    store["items"][record["backup_id"]]["verification_status"] = "NOT_VERIFIED"
    svc._save(store)
    drill_service = RestoreDrillService(pg_target, svc.store_path.with_name("drills2.json"))
    with pytest.raises(RestoreDrillError):
        drill_service.run(svc.get(record["backup_id"]))


def test_restore_drill_list_returns_newest_first(svc, pg_target):
    record = svc.create(retention_class="manual")
    drill_service = RestoreDrillService(pg_target, svc.store_path.with_name("drills3.json"))
    d1 = drill_service.run(record)
    items = drill_service.list()
    assert items[0]["drill_id"] == d1["drill_id"]


# ---------------------------------------------------------------------
# PostgreSQL Center summary (live)
# ---------------------------------------------------------------------

def test_postgres_summary_reports_size_revision_and_tables(pg_target):
    summary = postgres_summary(pg_target)
    assert summary["available"] is True
    assert summary["database_size_bytes"] > 0
    assert summary["db_revision"] == "0037_v72"
    assert summary["connections"]["total"] >= 1
    assert summary["connections"]["max"] > 0
    assert summary["blocked_lock_count"] == 0
    assert summary["long_running_queries"] == []
    table_names = {t["table"] for t in summary["largest_tables"]}
    assert "public.employees" in table_names
    assert "public.alembic_version" in table_names


def test_postgres_summary_detects_long_running_query_and_blocked_lock(pg_target):
    holder = subprocess.Popen(
        ["docker", "exec", "-i", pg_target.container, "sh", "-lc",
         'PGPASSWORD=testpw123 psql -U mesflow -d mesflow -c '
         '"BEGIN; UPDATE employees SET name=name WHERE id=1; SELECT pg_sleep(6);"'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    waiter = None
    try:
        time.sleep(1.5)
        waiter = subprocess.Popen(
            ["docker", "exec", "-i", pg_target.container, "sh", "-lc",
             'PGPASSWORD=testpw123 psql -U mesflow -d mesflow -c "UPDATE employees SET name=name WHERE id=1;"'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)
        summary = postgres_summary(pg_target, long_query_seconds=1)
        assert summary["blocked_lock_count"] >= 1
        assert len(summary["long_running_queries"]) >= 1
        assert any("pg_sleep" in q["query"] or "UPDATE" in q["query"] for q in summary["long_running_queries"])
    finally:
        holder.wait(timeout=15)
        if waiter is not None:
            waiter.wait(timeout=15)


def test_postgres_summary_unreachable_container_reports_not_available():
    bad_target = PostgresTarget(container=f"no-such-container-{uuid.uuid4().hex[:8]}")
    summary = postgres_summary(bad_target)
    assert summary["available"] is False
    assert summary["error"]
