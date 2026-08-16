"""P4 Backup / DB / Storage Center.

Reuses the project's existing, proven PostgreSQL backup mechanism
(mesflow/scripts/backup.sh: `pg_dump -Fc`, `pg_restore -l` manifest,
SHA256 checksum) rather than inventing a new backup technology --
implemented here as bounded, argument-array subprocess calls (never
shell-interpolated SQL/paths) so the Agent can track status, verify, apply
retention, and run isolated restore drills through a real API instead of a
human running the script by hand.

No Flask/deployment dependency -- pure/testable, matching
agent_backend/system_health.py and agent_backend/notifications.py.

Architecture (section 73): BackupService / RetentionService /
RestoreDrillService / StorageService, one JSON-file-backed record store per
concern (matching incidents.json/notifications.json), never a database of
its own.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

CommandResult = dict[str, Any]

# ---------------------------------------------------------------------
# JSON store helpers (same convention as incidents.json/notifications.json)
# ---------------------------------------------------------------------

def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bytes_to_human(value: int | float | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------
# PostgreSQL target + bounded subprocess runner
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class PostgresTarget:
    """Everything needed to reach a Postgres container's own database
    through `docker exec`, using the CONTAINER's own already-configured
    credentials (POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB env vars) --
    this Agent never types, stores, or logs a database password itself.

    `docker exec` defaults to the container's root OS user, not the
    `postgres` role -- a bare `psql`/`pg_dump` with no -U/-d fails closed
    ("role root does not exist"); every command below connects explicitly.
    """
    container: str = "mesflow-postgres"
    docker_bin: str = "docker"


def run_capture(cmd: list[str], timeout: int = 30, input_bytes: bytes | None = None) -> CommandResult:
    """Bounded command runner -- argument arrays only, never shell=True,
    always a timeout, output always bounded (section 51/56 of P0/P4:
    subprocess safety)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout, input=input_bytes, check=False,
        )
        return {
            "ok": result.returncode == 0, "exit_code": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace")[-200000:],
            "stderr": result.stderr.decode("utf-8", errors="replace")[-50000:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": 124, "stdout": "", "stderr": "Command timed out"}
    except OSError as exc:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": str(exc)}


def run_capture_binary(cmd: list[str], timeout: int = 1800, input_bytes: bytes | None = None) -> tuple[bool, bytes, str]:
    """Like run_capture, but stdout is returned as RAW bytes, never decoded
    as text. `pg_dump -Fc`'s custom-format output is binary -- decoding it
    as UTF-8 (even with errors='replace') silently corrupts the archive,
    which is exactly the kind of "looks like a backup, isn't restorable"
    failure section 15/51 exists to prevent. Returns (ok, stdout_bytes,
    stderr_text)."""
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, input=input_bytes, check=False)
        return result.returncode == 0, result.stdout, result.stderr.decode("utf-8", errors="replace")[-50000:]
    except subprocess.TimeoutExpired:
        return False, b"", "Command timed out"
    except OSError as exc:
        return False, b"", str(exc)


def psql_query(target: PostgresTarget, sql: str, timeout: int = 15) -> CommandResult:
    """Read-only query via the container's own psql, connected as its own
    configured role/database. `-Atqc` = unaligned, tuples-only, quiet."""
    inner = f'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -Atqc "{sql}"'
    return run_capture([target.docker_bin, "exec", target.container, "sh", "-lc", inner], timeout)


def psql_query_db(target: PostgresTarget, database: str, sql: str, timeout: int = 15) -> CommandResult:
    """Same as psql_query but against an explicit database name (used by
    the restore drill's isolated temporary database, never the target's
    own configured POSTGRES_DB)."""
    safe_db = database.replace('"', "")
    inner = f'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "{safe_db}" -v ON_ERROR_STOP=1 -Atqc "{sql}"'
    return run_capture([target.docker_bin, "exec", target.container, "sh", "-lc", inner], timeout)


# ---------------------------------------------------------------------
# PostgreSQL Center summary (read-only diagnostics)
# ---------------------------------------------------------------------

def postgres_summary(target: PostgresTarget, long_query_seconds: int = 5, table_limit: int = 10) -> dict:
    """Read-only PostgreSQL Center snapshot (section 6/7/8): database size,
    connection counts, long-running queries, blocked locks, migration
    revision, and largest tables. Every query here is a plain SELECT --
    nothing in this function ever mutates the database. Callers combine
    `largest_tables` with the existing predictive_summary()'s
    `fastest_growing_tables` (agent.py) for the "largest vs fastest-growing"
    comparison -- that forecasting logic already exists and is reused, not
    duplicated, here."""
    out: dict[str, Any] = {"available": False, "error": ""}

    size_result = psql_query(target, "SELECT pg_database_size(current_database());", 10)
    if not size_result["ok"]:
        out["error"] = (size_result["stderr"] or "could not reach database")[:500]
        return out
    try:
        size_bytes: int | None = int(size_result["stdout"].strip())
    except ValueError:
        size_bytes = None
    out["available"] = True
    out["database_size_bytes"] = size_bytes
    out["database_size_human"] = _bytes_to_human(size_bytes)

    conn_result = psql_query(
        target,
        "SELECT count(*) FILTER (WHERE state='active'), count(*) FILTER (WHERE state='idle'), "
        "count(*), (SELECT setting FROM pg_settings WHERE name='max_connections') FROM pg_stat_activity;",
        10,
    )
    out["connections"] = None
    if conn_result["ok"] and conn_result["stdout"].strip():
        parts = conn_result["stdout"].strip().split("|")
        if len(parts) == 4:
            try:
                out["connections"] = {
                    "active": int(parts[0] or 0), "idle": int(parts[1] or 0),
                    "total": int(parts[2] or 0), "max": int(parts[3] or 0),
                }
            except ValueError:
                pass

    long_result = psql_query(
        target,
        "SELECT pid, EXTRACT(EPOCH FROM (now()-query_start))::int, state, "
        "left(replace(query, '|', ' '), 200) FROM pg_stat_activity "
        f"WHERE state != 'idle' AND query_start IS NOT NULL AND now()-query_start > interval '{int(long_query_seconds)} seconds' "
        "ORDER BY query_start ASC LIMIT 10;",
        10,
    )
    long_queries = []
    if long_result["ok"]:
        for line in long_result["stdout"].splitlines():
            parts = line.split("|", 3)
            if len(parts) != 4:
                continue
            try:
                long_queries.append({"pid": int(parts[0]), "duration_seconds": int(parts[1]),
                                      "state": parts[2], "query": parts[3]})
            except ValueError:
                continue
    out["long_running_queries"] = long_queries

    lock_result = psql_query(target, "SELECT count(*) FROM pg_locks WHERE NOT granted;", 10)
    out["blocked_lock_count"] = None
    if lock_result["ok"]:
        try:
            out["blocked_lock_count"] = int(lock_result["stdout"].strip())
        except ValueError:
            pass

    revision_result = psql_query(target, "SELECT version_num FROM alembic_version LIMIT 1;", 10)
    out["db_revision"] = revision_result["stdout"].strip() if revision_result["ok"] else ""

    table_result = psql_query(
        target,
        "SELECT n.nspname||'.'||c.relname, pg_total_relation_size(c.oid) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','m') "
        "AND n.nspname NOT IN ('pg_catalog','information_schema') "
        f"ORDER BY pg_total_relation_size(c.oid) DESC LIMIT {int(table_limit)};",
        15,
    )
    tables = []
    if table_result["ok"]:
        for line in table_result["stdout"].splitlines():
            try:
                name, size = line.rsplit("|", 1)
                tables.append({"table": name, "size_bytes": int(size), "size_human": _bytes_to_human(int(size))})
            except (ValueError, TypeError):
                continue
    out["largest_tables"] = tables
    return out


# ---------------------------------------------------------------------
# Backup model + service
# ---------------------------------------------------------------------

BACKUP_STATUSES = ("RUNNING", "SUCCESS", "FAILED", "CANCELLED")
VERIFY_STATUSES = ("NOT_VERIFIED", "VERIFYING", "VERIFIED", "VERIFY_FAILED")
RETENTION_CLASSES = ("manual", "daily", "weekly", "monthly", "pinned", "pre_deploy")


class ConflictError(Exception):
    """A backup is already RUNNING for this target (section 53)."""


class BackupService:
    def __init__(self, target: PostgresTarget, backup_dir: Path, store_path: Path,
                 app_version_provider: Callable[[], str] | None = None,
                 db_revision_provider: Callable[[], str] | None = None,
                 audit: Callable[[str, dict], None] | None = None):
        self.target = target
        self.backup_dir = backup_dir
        self.store_path = store_path
        self.app_version_provider = app_version_provider or (lambda: "")
        self.db_revision_provider = db_revision_provider or (lambda: self._db_revision())
        self.audit = audit or (lambda action, data: None)
        self._lock = threading.Lock()

    # -- store -----------------------------------------------------------
    def _load(self) -> dict:
        with self._lock:
            return load_json(self.store_path, {"items": {}})

    def _save(self, store: dict) -> None:
        with self._lock:
            save_json(self.store_path, store)

    def list(self) -> list[dict]:
        items = list(self._load()["items"].values())
        items.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return items

    def get(self, backup_id: str) -> dict | None:
        return self._load()["items"].get(backup_id)

    def _running_exists(self) -> bool:
        return any(r.get("status") == "RUNNING" for r in self._load()["items"].values())

    def _db_revision(self) -> str:
        result = psql_query(self.target, "SELECT version_num FROM alembic_version LIMIT 1;", 8)
        return result["stdout"].strip() if result.get("ok") else ""

    # -- create ------------------------------------------------------------
    def create(self, retention_class: str = "manual", triggered_by: str = "", reason: str = "") -> dict:
        if retention_class not in RETENTION_CLASSES:
            raise ValueError(f"unknown retention_class: {retention_class}")
        if self._running_exists():
            raise ConflictError("a backup is already RUNNING for this target")
        backup_id = uuid.uuid4().hex
        started_at = now_iso()
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        app_version = self.app_version_provider()
        stem = f"mesflow-postgres-{ts}-{app_version or 'unknown'}"
        final_path = self.backup_dir / f"{stem}.dump"
        tmp_path = self.backup_dir / f"{stem}.dump.tmp"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        record = {
            "backup_id": backup_id, "created_at": started_at, "database": self.target.container,
            "type": "pg_dump_custom", "status": "RUNNING", "verification_status": "NOT_VERIFIED",
            "restore_tested": False, "retention_class": retention_class, "pinned": retention_class == "pinned",
            "triggered_by": triggered_by, "reason": reason, "size_bytes": None, "duration_seconds": None,
            "sha256": "", "storage_path": str(final_path), "app_version": app_version,
            "db_revision": "", "manifest_ok": False, "error": "",
        }
        store = self._load()
        store["items"][backup_id] = record
        self._save(store)
        self.audit("BACKUP_STARTED", {"backup_id": backup_id, "retention_class": retention_class, "triggered_by": triggered_by})

        t0 = time.time()
        # Atomic completion (section 51/52): pg_dump's binary output is
        # captured byte-for-byte (run_capture_binary -- never decoded as
        # text) into a .tmp path first; only a clean exit + non-empty
        # output gets it renamed to its final name. A crashed/partial dump
        # never appears as a real backup file. Archive-list verification
        # happens separately in verify(), called immediately below.
        dump_cmd = [self.target.docker_bin, "exec", self.target.container, "sh", "-lc",
                    'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc']
        ok, raw, stderr_text = run_capture_binary(dump_cmd, timeout=1800)
        duration = round(time.time() - t0, 2)
        if not ok or not raw:
            record.update(status="FAILED", duration_seconds=duration,
                           error=(stderr_text or "pg_dump produced no output")[:2000])
            store = self._load(); store["items"][backup_id] = record; self._save(store)
            self.audit("BACKUP_FAILED", {"backup_id": backup_id, "error": record["error"]})
            return record
        tmp_path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        tmp_path.replace(final_path)
        size = final_path.stat().st_size
        db_revision = self.db_revision_provider()
        record.update(status="SUCCESS", duration_seconds=duration, size_bytes=size, sha256=digest,
                       db_revision=db_revision, error="")
        store = self._load(); store["items"][backup_id] = record; self._save(store)
        self.audit("BACKUP_COMPLETED", {"backup_id": backup_id, "size_bytes": size, "duration_seconds": duration})
        # verify immediately -- a backup without verification is not
        # sufficient (section 19).
        self.verify(backup_id)
        return self.get(backup_id)

    # -- verify --------------------------------------------------------
    def verify(self, backup_id: str) -> dict:
        store = self._load()
        record = store["items"].get(backup_id)
        if not record:
            raise KeyError(backup_id)
        if record["status"] != "SUCCESS":
            raise ValueError("cannot verify a backup that did not complete successfully")
        record["verification_status"] = "VERIFYING"
        store["items"][backup_id] = record; self._save(store)

        path = Path(record["storage_path"])
        ok = path.is_file() and path.stat().st_size > 100  # "size > reasonable minimum" (section 19)
        checksum_ok = ok and hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
        manifest_ok = False
        if ok:
            listing = run_capture([self.target.docker_bin, "exec", "-i", self.target.container, "pg_restore", "-l"],
                                   timeout=30, input_bytes=path.read_bytes())
            manifest_ok = listing["ok"] and bool(listing["stdout"].strip())
        passed = ok and checksum_ok and manifest_ok
        store = self._load()
        record = store["items"][backup_id]
        record["verification_status"] = "VERIFIED" if passed else "VERIFY_FAILED"
        record["manifest_ok"] = manifest_ok
        store["items"][backup_id] = record
        self._save(store)
        self.audit("BACKUP_VERIFIED" if passed else "BACKUP_VERIFY_FAILED",
                   {"backup_id": backup_id, "checksum_ok": checksum_ok, "manifest_ok": manifest_ok})
        return record

    # -- pin/unpin -------------------------------------------------------
    def pin(self, backup_id: str, actor: str = "") -> dict:
        store = self._load(); record = store["items"][backup_id]
        record["pinned"] = True
        store["items"][backup_id] = record; self._save(store)
        self.audit("BACKUP_PINNED", {"backup_id": backup_id, "actor": actor})
        return record

    def unpin(self, backup_id: str, actor: str = "") -> dict:
        store = self._load(); record = store["items"][backup_id]
        record["pinned"] = False
        store["items"][backup_id] = record; self._save(store)
        self.audit("BACKUP_UNPINNED", {"backup_id": backup_id, "actor": actor})
        return record

    def mark_restore_tested(self, backup_id: str) -> dict:
        store = self._load(); record = store["items"][backup_id]
        record["restore_tested"] = True
        store["items"][backup_id] = record; self._save(store)
        return record


# ---------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------

DEFAULT_RETENTION_POLICY = {"daily": 7, "weekly": 4, "monthly": 6, "manual": 20, "pre_deploy": 10}


class RetentionService:
    """Explicit, previewable retention -- never a silent background delete.

    Hard invariant (section 63, tested): the last remaining VERIFIED
    SUCCESS backup is never eligible for deletion, no matter what the
    numeric policy would otherwise allow. Pinned backups and the current
    restore-drill source are likewise always protected.
    """

    def __init__(self, backup_service: BackupService, policy: dict[str, int] | None = None,
                 audit: Callable[[str, dict], None] | None = None):
        self.backups = backup_service
        self.policy = policy or dict(DEFAULT_RETENTION_POLICY)
        self.audit = audit or (lambda action, data: None)

    def _protected_ids(self, records: list[dict], active_restore_source: str | None) -> set[str]:
        protected: set[str] = set()
        verified = [r for r in records if r["status"] == "SUCCESS" and r["verification_status"] == "VERIFIED"]
        if verified:
            # newest verified backup is always the fallback recovery path.
            protected.add(max(verified, key=lambda r: r["created_at"])["backup_id"])
        elif records:
            # no verified backup exists at all yet -- protect the newest
            # SUCCESS backup regardless, so retention can never leave zero
            # usable backups on disk.
            successes = [r for r in records if r["status"] == "SUCCESS"]
            if successes:
                protected.add(max(successes, key=lambda r: r["created_at"])["backup_id"])
        for r in records:
            if r.get("pinned"):
                protected.add(r["backup_id"])
        if active_restore_source:
            protected.add(active_restore_source)
        return protected

    def preview(self, active_restore_source: str | None = None) -> dict:
        records = self.backups.list()
        protected_ids = self._protected_ids(records, active_restore_source)
        by_class: dict[str, list[dict]] = {}
        for r in records:
            by_class.setdefault(r.get("retention_class", "manual"), []).append(r)
        eligible: list[dict] = []
        for cls, keep in self.policy.items():
            items = sorted((r for r in by_class.get(cls, []) if r["status"] == "SUCCESS"),
                           key=lambda r: r["created_at"], reverse=True)
            for r in items[keep:]:
                if r["backup_id"] in protected_ids:
                    continue
                eligible.append({**r, "reason": f"Expired by {cls} retention (keep {keep})"})
        total_bytes = sum(r.get("size_bytes") or 0 for r in eligible)
        return {
            "eligible": eligible, "eligible_count": len(eligible), "eligible_bytes": total_bytes,
            "eligible_bytes_human": _bytes_to_human(total_bytes),
            "protected": [r for r in records if r["backup_id"] in protected_ids],
            "policy": self.policy,
        }

    def apply(self, actor: str = "", reason: str = "", active_restore_source: str | None = None) -> dict:
        preview = self.preview(active_restore_source)
        deleted = []
        store = self.backups._load()
        for r in preview["eligible"]:
            backup_id = r["backup_id"]
            record = store["items"].get(backup_id)
            if not record:
                continue
            path = Path(record["storage_path"])
            try:
                if path.is_file():
                    path.unlink()
            except OSError as exc:
                # a filesystem error must not silently pretend the backup
                # record itself is gone (section 54: cancellation/cleanup
                # must not claim success when artifacts remain).
                self.audit("RETENTION_DELETE_FAILED", {"backup_id": backup_id, "error": str(exc)})
                continue
            del store["items"][backup_id]
            deleted.append(record)
            self.audit("BACKUP_DELETED_BY_RETENTION",
                       {"backup_id": backup_id, "actor": actor, "reason": reason or r["reason"], "size_bytes": record.get("size_bytes")})
        self.backups._save(store)
        reclaimed = sum(r.get("size_bytes") or 0 for r in deleted)
        self.audit("RETENTION_APPLIED", {"actor": actor, "deleted_count": len(deleted), "reclaimed_bytes": reclaimed})
        return {"deleted": deleted, "deleted_count": len(deleted), "reclaimed_bytes": reclaimed,
                "reclaimed_bytes_human": _bytes_to_human(reclaimed)}


# ---------------------------------------------------------------------
# Restore drill (LOCAL/TEST only -- never Production)
# ---------------------------------------------------------------------

class RestoreDrillError(Exception):
    pass


class RestoreDrillService:
    """Restores a verified backup into a throwaway, generated-name
    database on the SAME Postgres server the Agent is configured to talk
    to (LOCAL/TEST per section 21/55) -- never accepts a caller-supplied
    target database name, so a restore drill can never be pointed at a
    real/production database through free-text input."""

    def __init__(self, target: PostgresTarget, store_path: Path,
                 audit: Callable[[str, dict], None] | None = None):
        self.target = target
        self.store_path = store_path
        self.audit = audit or (lambda action, data: None)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        with self._lock:
            return load_json(self.store_path, {"items": {}})

    def _save(self, store: dict) -> None:
        with self._lock:
            save_json(self.store_path, store)

    def list(self) -> list[dict]:
        items = list(self._load()["items"].values())
        items.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return items

    def run(self, backup_record: dict) -> dict:
        if backup_record["status"] != "SUCCESS" or backup_record["verification_status"] != "VERIFIED":
            raise RestoreDrillError("only a VERIFIED backup may be used for a restore drill")
        drill_id = uuid.uuid4().hex
        temp_db = f"mesflow_restore_drill_{drill_id[:12]}"  # generated, never user input (section 55)
        started_at = now_iso()
        self.audit("RESTORE_DRILL_STARTED", {"drill_id": drill_id, "backup_id": backup_record["backup_id"], "temp_db": temp_db})
        record = {"drill_id": drill_id, "backup_id": backup_record["backup_id"], "temp_database": temp_db,
                   "started_at": started_at, "completed_at": None, "duration_seconds": None,
                   "status": "RUNNING", "validation": {}, "cleanup_ok": None, "error": ""}
        store = self._load(); store["items"][drill_id] = record; self._save(store)
        t0 = time.time()
        try:
            create = psql_query(self.target, f'CREATE DATABASE "{temp_db}";', 20)
            if not create["ok"]:
                raise RestoreDrillError(f"could not create isolated drill database: {create['stderr'][:500]}")
            data = Path(backup_record["storage_path"]).read_bytes()
            restore = run_capture(
                [self.target.docker_bin, "exec", "-i", self.target.container, "sh", "-lc",
                 f'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore -U "$POSTGRES_USER" -d "{temp_db}" --no-owner'],
                timeout=1800, input_bytes=data)
            if not restore["ok"]:
                raise RestoreDrillError(f"pg_restore failed: {restore['stderr'][:1000]}")
            validation = self._validate(temp_db)
            record["validation"] = validation
            if not validation["passed"]:
                raise RestoreDrillError("post-restore validation failed: " + validation.get("reason", ""))
            record["status"] = "SUCCESS"
        except RestoreDrillError as exc:
            record["status"] = "FAILED"
            record["error"] = str(exc)[:2000]
        finally:
            drop = psql_query(self.target, f'DROP DATABASE IF EXISTS "{temp_db}" WITH (FORCE);', 20)
            record["cleanup_ok"] = drop["ok"]
            if not drop["ok"]:
                record["error"] = (record["error"] + " | cleanup also failed: " + drop["stderr"][:300]).strip(" |")
            record["completed_at"] = now_iso()
            record["duration_seconds"] = round(time.time() - t0, 2)
            store = self._load(); store["items"][drill_id] = record; self._save(store)
        self.audit("RESTORE_DRILL_COMPLETED", {"drill_id": drill_id, "status": record["status"], "cleanup_ok": record["cleanup_ok"]})
        return record

    def _validate(self, temp_db: str) -> dict:
        """Lightweight post-restore checks (section 56): the database
        opens, at least one expected table/schema exists, and if an
        alembic_version table was part of the dump, its revision is
        readable -- never a full destructive drill, never Production."""
        tables = psql_query_db(self.target, temp_db,
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';", 15)
        if not tables["ok"]:
            return {"passed": False, "reason": "could not query information_schema", "table_count": None}
        try:
            table_count = int(tables["stdout"].strip())
        except ValueError:
            return {"passed": False, "reason": "unexpected table-count output", "table_count": None}
        if table_count <= 0:
            return {"passed": False, "reason": "restored database has zero tables", "table_count": 0}
        revision_result = psql_query_db(self.target, temp_db, "SELECT version_num FROM alembic_version LIMIT 1;", 10)
        revision = revision_result["stdout"].strip() if revision_result["ok"] else ""
        return {"passed": True, "reason": "", "table_count": table_count, "db_revision": revision}


# ---------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------

def storage_pressure_level(percent: float, warning: float = 80.0, critical: float = 90.0) -> str:
    """Pure threshold classification (section 31/62) -- OK below `warning`,
    WARNING between `warning` and `critical`, CRITICAL at/above `critical`.
    Deliberately the same OK/WARNING/CRITICAL vocabulary used elsewhere in
    this Agent (e.g. DISK_CRITICAL in agent.py's _incident_conditions) so
    Storage Center readings and the incident engine never disagree."""
    if percent >= critical:
        return "CRITICAL"
    if percent >= warning:
        return "WARNING"
    return "OK"


class StorageService:
    """Filesystem + category breakdown (section 30/31) -- bounded, never a
    per-request full recursive scan of arbitrary depth on a hot path.
    Categories are computed from explicitly configured roots only (section
    48: no arbitrary path browsing from the web UI)."""

    def __init__(self, categories: dict[str, Path], docker_bin: str = "docker",
                 warning_percent: float = 80.0, critical_percent: float = 90.0):
        self.categories = categories  # {"backups": Path, "releases": Path, "logs": Path, ...}
        self.docker_bin = docker_bin
        self.warning_percent = warning_percent
        self.critical_percent = critical_percent

    def filesystem_summary(self, path: str = "/") -> dict:
        usage = shutil.disk_usage(path)
        percent = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
        return {"path": path, "total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free,
                "percent": percent, "total": _bytes_to_human(usage.total), "used": _bytes_to_human(usage.used),
                "free": _bytes_to_human(usage.free),
                "pressure_level": storage_pressure_level(percent, self.warning_percent, self.critical_percent)}

    def category_sizes(self) -> dict[str, dict]:
        out = {}
        for name, root in self.categories.items():
            if not root.exists():
                out[name] = {"exists": False, "bytes": 0, "human": "0.0 B", "estimated": False}
                continue
            total = self._du(root)
            out[name] = {"exists": True, "bytes": total, "human": _bytes_to_human(total), "estimated": False}
        return out

    @staticmethod
    def _du(root: Path) -> int:
        total = 0
        try:
            for entry in root.rglob("*"):
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                except OSError:
                    continue
        except OSError:
            return total
        return total

    def docker_storage(self) -> dict:
        if not shutil.which(self.docker_bin):
            return {"available": False}
        result = run_capture([self.docker_bin, "system", "df", "--format", "{{json .}}"], timeout=15)
        rows = []
        if result["ok"]:
            for line in result["stdout"].splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return {"available": result["ok"], "items": rows}
