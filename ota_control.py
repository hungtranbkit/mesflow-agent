"""Agent-owned ESP OTA engine (the only OTA engine; MESFlow owns devices).

v2 (``ota2_*`` tables, see the "v2 engine" section) is what every route uses:
instance-scoped fleet, hardware profiles, validated per-profile app
artifacts, force batches with pinned per-target artifacts, claim leases,
retry/backoff and boot-identity success.  The v1 tables/functions below are
kept read-only for history and existing callers; they are no longer served
to devices.  There is still no draft/activate/stage/canary model.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import sqlite3
import time
import uuid
import zipfile
from pathlib import Path

AGENT_HOME = Path(os.environ.get("WORKSHOP_AGENT_HOME", "/data"))
OTA_ROOT = AGENT_HOME / "ota"
OTA_DB = OTA_ROOT / "ota.sqlite3"
ARTIFACT_ROOT = OTA_ROOT / "firmware"
MAX_APP_SIZE = int(os.environ.get("MESFLOW_OTA_PARTITION_SIZE", "3342336"))
TERMINAL = {"SUCCESS", "FAILED"}


def readiness_allows_update(readiness):
    """Require an explicit MESFlow readiness decision; fail closed otherwise."""
    return bool(readiness.get("available") and readiness.get("ota_ready") is True)


def _now(): return time.time()
@contextlib.contextmanager
def _connect():
    """Short-lived connection: commit on success, rollback on error, ALWAYS close.

    sqlite3.Connection's own context manager only commits/rolls back and
    leaves the file (plus WAL/SHM handles) open, which leaked ~5 FDs per call
    and exhausted RLIMIT_NOFILE=1024 under normal polling.
    """
    OTA_ROOT.mkdir(parents=True, exist_ok=True); ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(OTA_DB, timeout=15); c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        with c:  # transaction: commit / rollback
            yield c
    finally:
        c.close()
def _row(row): return dict(row) if row else None
def _version_key(value):
    nums = re.findall(r"\d+", str(value or "")); return tuple(int(x) for x in nums) if nums else (0,)


def _init_v1_tables():
    with _connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS ota_firmware(
          id TEXT PRIMARY KEY, version TEXT NOT NULL, hardware_model TEXT NOT NULL DEFAULT '',
          filename TEXT NOT NULL, storage_path TEXT NOT NULL, size INTEGER NOT NULL,
          sha256 TEXT NOT NULL, created_at REAL NOT NULL,
          UNIQUE(version,hardware_model,sha256));
        CREATE TABLE IF NOT EXISTS ota_jobs(
          id TEXT PRIMARY KEY, firmware_id TEXT NOT NULL REFERENCES ota_firmware(id),
          created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'RUNNING');
        CREATE TABLE IF NOT EXISTS ota_job_devices(
          id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES ota_jobs(id) ON DELETE CASCADE,
          kiosk_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', error TEXT NOT NULL DEFAULT '',
          updated_at REAL NOT NULL, UNIQUE(job_id,kiosk_id));
        CREATE UNIQUE INDEX IF NOT EXISTS ota_one_open_job_device ON ota_job_devices(kiosk_id)
          WHERE status NOT IN ('SUCCESS','FAILED');
        CREATE TABLE IF NOT EXISTS ota_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, kiosk_id TEXT NOT NULL,
          status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
          client_event_id TEXT, UNIQUE(kiosk_id,client_event_id));
        CREATE TABLE IF NOT EXISTS ota_devices(
          kiosk_id TEXT PRIMARY KEY, kiosk_name TEXT NOT NULL DEFAULT '', hardware_model TEXT NOT NULL DEFAULT '',
          firmware_version TEXT NOT NULL DEFAULT '', firmware_build TEXT NOT NULL DEFAULT '', online INTEGER NOT NULL DEFAULT 0,
          updated_at REAL NOT NULL);
        """)
        # Upgrade databases created by the earlier Phase 2 OTA event schema
        # without deleting its historical columns or rows.  The simplified
        # event writer associates each event with the concrete job and uses a
        # normalized error field.
        columns = {row[1] for row in c.execute("PRAGMA table_info(ota_events)")}
        if "job_id" not in columns:
            c.execute("ALTER TABLE ota_events ADD COLUMN job_id TEXT")
        if "error" not in columns:
            c.execute("ALTER TABLE ota_events ADD COLUMN error TEXT NOT NULL DEFAULT ''")


def sync_devices(items):
    init_ota_db(); now = _now()
    with _connect() as c:
        for x in items or []:
            kid = str(x.get("kiosk_id") or x.get("id") or "").strip()
            if not kid: continue
            c.execute("""INSERT INTO ota_devices(kiosk_id,kiosk_name,hardware_model,firmware_version,firmware_build,online,updated_at)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(kiosk_id) DO UPDATE SET kiosk_name=excluded.kiosk_name,hardware_model=excluded.hardware_model,
              firmware_version=excluded.firmware_version,firmware_build=excluded.firmware_build,online=excluded.online,updated_at=excluded.updated_at""",
              (kid,str(x.get("kiosk_name") or x.get("device_name") or ""),str(x.get("hardware_model") or ""),str(x.get("firmware_version") or ""),str(x.get("firmware_build") or ""),1 if x.get("online") else 0,now))
        # A reboot can race the one-shot OTA_HEALTHCHECK_OK event (for
        # example while the Agent is restarting).  The heartbeat version is
        # authoritative evidence that the assigned image is now running, so
        # reconcile targets waiting for reboot/healthcheck without requiring a
        # duplicate job or a manual retry.
        for x in items or []:
            kid = str(x.get("kiosk_id") or x.get("id") or "").strip()
            version = str(x.get("firmware_version") or "").strip()
            if not kid or not version: continue
            rows = c.execute("""SELECT d.id,d.job_id,f.version FROM ota_job_devices d
              JOIN ota_jobs j ON j.id=d.job_id JOIN ota_firmware f ON f.id=j.firmware_id
              WHERE d.kiosk_id=? AND j.status='RUNNING' AND d.status IN ('REBOOTING','HEALTHCHECK','FLASHING')""", (kid,)).fetchall()
            for row in rows:
                if str(row["version"]) != version: continue
                c.execute("UPDATE ota_job_devices SET status='SUCCESS',error='',updated_at=? WHERE id=?", (now,row["id"]))
                c.execute("INSERT INTO ota_events(job_id,kiosk_id,status,error,created_at,client_event_id) VALUES(?,?,?,?,?,?)", (row["job_id"],kid,"SUCCESS","RECONCILED_HEARTBEAT",now,None))
                open_count = c.execute("SELECT COUNT(*) FROM ota_job_devices WHERE job_id=? AND status NOT IN ('SUCCESS','FAILED')", (row["job_id"],)).fetchone()[0]
                if not open_count:
                    c.execute("UPDATE ota_jobs SET status='SUCCESS' WHERE id=?", (row["job_id"],))
    return list_devices()


def list_devices(limit=500):
    init_ota_db()
    with _connect() as c: return [_row(x) for x in c.execute("SELECT * FROM ota_devices ORDER BY kiosk_id LIMIT ?", (limit,))]


def list_firmware(limit=100):
    init_ota_db()
    with _connect() as c: return [_row(x) for x in c.execute("SELECT * FROM ota_firmware ORDER BY created_at DESC LIMIT ?", (limit,))]


def get_firmware(fid):
    init_ota_db()
    with _connect() as c: return _row(c.execute("SELECT * FROM ota_firmware WHERE id=?", (fid,)).fetchone())


def _save_firmware(stream, filename, version, hardware=""):
    filename = Path(filename or "").name
    if not filename.lower().endswith(".bin"): raise ValueError("INVALID_FIRMWARE")
    if "merged" in filename.lower(): raise ValueError("MERGED_IMAGE_NOT_ALLOWED")
    if not version: raise ValueError("VERSION_REQUIRED")
    digest = hashlib.sha256(); size = 0; tmp = ARTIFACT_ROOT / (".upload-" + uuid.uuid4().hex)
    try:
        with tmp.open("xb") as out:
            while chunk := stream.read(1024 * 1024): out.write(chunk); digest.update(chunk); size += len(chunk)
        if size <= 0: raise ValueError("INVALID_FIRMWARE")
        if size > MAX_APP_SIZE: raise ValueError("FIRMWARE_TOO_LARGE")
        sha = digest.hexdigest(); init_ota_db()
        with _connect() as c:
            existing = c.execute("SELECT * FROM ota_firmware WHERE version=? AND hardware_model=? AND sha256=?", (version,hardware,sha)).fetchone()
            if existing: tmp.unlink(missing_ok=True); return _row(existing)
            conflict = c.execute("SELECT id FROM ota_firmware WHERE version=? AND hardware_model=?", (version,hardware)).fetchone()
            if conflict: raise ValueError("VERSION_CONFLICT")
            fid = str(uuid.uuid4()); target = ARTIFACT_ROOT / (fid + ".bin"); tmp.replace(target)
            c.execute("INSERT INTO ota_firmware VALUES(?,?,?,?,?,?,?,?)", (fid,version,hardware,filename,str(target),size,sha,_now()))
            return _row(c.execute("SELECT * FROM ota_firmware WHERE id=?", (fid,)).fetchone())
    except Exception:
        tmp.unlink(missing_ok=True); raise

def _save_firmware_package(stream, filename):
    name = Path(filename or "").name
    if not name.lower().endswith(".zip"): raise ValueError("INVALID_OTA_PACKAGE")
    raw = stream.read()
    if not raw: raise ValueError("INVALID_OTA_PACKAGE")
    try:
        z = zipfile.ZipFile(io.BytesIO(raw)); files=[x for x in z.infolist() if not x.is_dir()]
        bins=[x for x in files if x.filename.lower().endswith(".bin") and "merged" not in Path(x.filename).name.lower()]
        manifests=[x for x in files if x.filename.lower().endswith(".manifest.json")]
        if len(bins)!=1 or len(manifests)!=1 or len(files)!=2: raise ValueError("OTA_PACKAGE_CONTENT_INVALID")
        manifest=json.loads(z.read(manifests[0]).decode("utf-8")); version=str(manifest.get("version") or "").strip(); hardware=str(manifest.get("hardware_model") or "").strip()
        if not version or not hardware: raise ValueError("OTA_MANIFEST_METADATA_REQUIRED")
        binary_name=Path(bins[0].filename).name
        if manifest.get("filename") and Path(str(manifest["filename"])).name!=binary_name: raise ValueError("OTA_MANIFEST_BINARY_MISMATCH")
        return _save_firmware(io.BytesIO(z.read(bins[0])),binary_name,version,hardware)
    except zipfile.BadZipFile: raise ValueError("INVALID_OTA_PACKAGE")


def create_job(firmware, kiosk_ids):
    ids = list(dict.fromkeys(str(x).strip() for x in kiosk_ids if str(x).strip()))
    if not ids: raise ValueError("DEVICE_REQUIRED")
    init_ota_db(); now = _now(); jid = str(uuid.uuid4())
    with _connect() as c:
        for kid in ids:
            d = c.execute("SELECT online,hardware_model FROM ota_devices WHERE kiosk_id=?", (kid,)).fetchone()
            if not d: raise ValueError("DEVICE_NOT_FOUND")
            if not d["online"]: raise ValueError("DEVICE_OFFLINE")
            if firmware["hardware_model"] and d["hardware_model"] and firmware["hardware_model"] != d["hardware_model"]: raise ValueError("WRONG_HARDWARE")
            if c.execute("SELECT id FROM ota_job_devices WHERE kiosk_id=? AND status NOT IN ('SUCCESS','FAILED')", (kid,)).fetchone(): raise ValueError("OTA_ALREADY_RUNNING")
        c.execute("INSERT INTO ota_jobs VALUES(?,?,?,?)", (jid,firmware["id"],now,"RUNNING"))
        for kid in ids: c.execute("INSERT INTO ota_job_devices(job_id,kiosk_id,updated_at) VALUES(?,?,?)", (jid,kid,now))
    return get_job(jid)


def get_job(jid):
    init_ota_db()
    with _connect() as c:
        job = _row(c.execute("SELECT j.*,f.version,f.hardware_model,f.filename,f.size,f.sha256 FROM ota_jobs j JOIN ota_firmware f ON f.id=j.firmware_id WHERE j.id=?", (jid,)).fetchone())
        if not job: return None
        job["devices"] = [_row(x) for x in c.execute("SELECT * FROM ota_job_devices WHERE job_id=? ORDER BY id", (jid,))]
        return job


def cancel_job(jid):
    """Cancel an OTA job without interrupting any device already flashing."""
    init_ota_db(); now = _now()
    with _connect() as c:
        job = c.execute("SELECT status FROM ota_jobs WHERE id=?", (jid,)).fetchone()
        if not job: return None
        if job["status"] in ("SUCCESS", "FAILED", "CANCELLED"):
            return get_job(jid)
        c.execute("UPDATE ota_job_devices SET status='FAILED',error='JOB_CANCELLED',updated_at=? WHERE job_id=? AND status IN ('PENDING','DOWNLOADING')", (now,jid))
        c.execute("UPDATE ota_jobs SET status='CANCELLED' WHERE id=?", (jid,))
    return get_job(jid)


def list_jobs(limit=100):
    init_ota_db()
    with _connect() as c: return [get_job(x["id"]) for x in c.execute("SELECT id FROM ota_jobs ORDER BY created_at DESC LIMIT ?", (limit,))]


def check_update(kiosk_id, current_version, hardware_model):
    init_ota_db()
    with _connect() as c:
        row = c.execute("""SELECT d.*,j.id job_id,f.id firmware_id,f.version,f.hardware_model,f.filename,f.size,f.sha256,f.storage_path
          FROM ota_job_devices d JOIN ota_jobs j ON j.id=d.job_id JOIN ota_firmware f ON f.id=j.firmware_id
          WHERE d.kiosk_id=? AND j.status='RUNNING' AND d.status IN ('PENDING','DOWNLOADING') ORDER BY d.id LIMIT 1""", (kiosk_id,)).fetchone()
        if not row: return {"update_available":False}
        if row["hardware_model"] and row["hardware_model"] != hardware_model: return {"update_available":False,"reason":"WRONG_HARDWARE"}
        if _version_key(row["version"]) <= _version_key(current_version): return {"update_available":False,"reason":"VERSION_NOT_NEWER"}
        c.execute("UPDATE ota_job_devices SET status='DOWNLOADING',updated_at=? WHERE id=?", (_now(),row["id"]))
        return {"update_available":True,"job_id":row["job_id"],"job_device_id":row["id"],"firmware_id":row["firmware_id"],"version":row["version"],"hardware_model":row["hardware_model"],"filename":row["filename"],"size":row["size"],"sha256":row["sha256"]}


def target_for_download(kiosk_id, firmware_id):
    init_ota_db()
    with _connect() as c:
        return _row(c.execute("""SELECT f.*,d.id job_device_id,d.job_id FROM ota_job_devices d JOIN ota_jobs j ON j.id=d.job_id JOIN ota_firmware f ON f.id=j.firmware_id
          WHERE d.kiosk_id=? AND f.id=? AND j.status='RUNNING' AND d.status IN ('DOWNLOADING','FLASHING','REBOOTING')""", (kiosk_id,firmware_id)).fetchone())


def record_event(body):
    init_ota_db(); kid = str(body.get("kiosk_id") or ""); event = str(body.get("event") or body.get("status") or "").upper(); client = str(body.get("client_event_id") or "")
    mapping = {"OTA_DOWNLOAD_START":"DOWNLOADING","OTA_DOWNLOAD_COMPLETE":"FLASHING","OTA_VERIFY_OK":"FLASHING","OTA_REBOOTING":"REBOOTING","OTA_HEALTHCHECK_OK":"SUCCESS","OTA_BOOT_NEW_VERSION":"REBOOTING","OTA_FAILED":"FAILED","OTA_VERIFY_FAILED":"FAILED","OTA_ROLLBACK":"FAILED"}
    status = mapping.get(event, event if event in {"DOWNLOADING","FLASHING","REBOOTING","SUCCESS","FAILED"} else "")
    with _connect() as c:
        if client:
            old = c.execute("SELECT id FROM ota_events WHERE kiosk_id=? AND client_event_id=?", (kid,client)).fetchone()
            if old: return {"duplicate":True}
        target = c.execute("SELECT id,job_id FROM ota_job_devices WHERE kiosk_id=? AND status NOT IN ('SUCCESS','FAILED') ORDER BY id DESC LIMIT 1", (kid,)).fetchone()
        jid = target["job_id"] if target else body.get("job_id")
        c.execute("INSERT INTO ota_events(job_id,kiosk_id,status,error,created_at,client_event_id) VALUES(?,?,?,?,?,?)", (jid,kid,status or event,str(body.get("error_code") or body.get("message") or ""),_now(),client or None))
        if target and status: c.execute("UPDATE ota_job_devices SET status=?,error=?,updated_at=? WHERE id=?", (status,str(body.get("error_code") or body.get("message") or ""),_now(),target["id"]))
        if target and status in TERMINAL:
            open_count = c.execute("SELECT COUNT(*) FROM ota_job_devices WHERE job_id=? AND status NOT IN ('SUCCESS','FAILED')", (jid,)).fetchone()[0]
            if not open_count: c.execute("UPDATE ota_jobs SET status='SUCCESS' WHERE id=? AND NOT EXISTS(SELECT 1 FROM ota_job_devices WHERE job_id=? AND status='FAILED')", (jid,jid)); c.execute("UPDATE ota_jobs SET status='FAILED' WHERE id=? AND EXISTS(SELECT 1 FROM ota_job_devices WHERE job_id=? AND status='FAILED')", (jid,jid))
    return {"duplicate":False}



# =====================================================================
# v2 engine: instance scope + hardware profiles + force batches
# =====================================================================
import contextlib
import struct

MAX_ATTEMPTS = max(1, int(os.environ.get("MESFLOW_OTA_MAX_ATTEMPTS", "3")))
MAX_CONCURRENT = max(1, int(os.environ.get("MESFLOW_OTA_MAX_CONCURRENT", "5")))
OFFER_LEASE_S = 300        # an offer stays the same claim while the device reaches a safe point
DOWNLOAD_LEASE_S = 900     # download + verify + finalize on a slow Wi-Fi link
REBOOT_LEASE_S = 900       # reboot + Wi-Fi + boot self-check before the target counts as lost
BACKOFF_BASE_S = 60
BACKOFF_MAX_S = 1800
MAX_PACKAGE_BYTES = 64 * 1024 * 1024

WAITING = ("QUEUED", "WAITING_OFFLINE", "WAITING_SAFE_POINT")
IN_FLIGHT = ("DOWNLOADING", "FLASHING", "REBOOTING", "VERIFYING")
OPEN = WAITING + IN_FLIGHT
TERMINAL2 = ("SUCCESS", "FAILED", "BLOCKED", "CANCELLED", "SKIPPED")
STATUSES = OPEN + TERMINAL2
NON_RETRYABLE = {"OTA_WRONG_HARDWARE", "OTA_NO_SPACE", "OTA_PROFILE_MISMATCH", "OTA_DOWNGRADE_REFUSED",
                 "OTA_IMAGE_INVALID", "OTA_ARTIFACT_TYPE_INVALID", "OTA_ORIGIN_MISMATCH"}
CHIP_IDS = {"esp32": 0, "esp32s2": 2, "esp32c3": 5, "esp32s3": 9, "esp32c6": 13, "esp32h2": 16}
PROFILE_KEYS = ("profile_id", "hardware_model", "chip", "min_chip_revision", "flash_size", "psram",
                "partition_table_sha256", "ota_slot_size", "board", "scanner_interface", "fqbn")
PROFILE_REQUIRED_KEYS = ("profile_id", "hardware_model", "chip", "partition_table_sha256", "ota_slot_size")
IDENTITY_RE = re.compile(rb"MESFLOW_FW_IDENTITY:version=([^;\x00]{1,40});build=([^;\x00]{1,64});profile=([^;\x00]{1,80});hw=([^;\x00]{1,40});end")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_OPEN_SQL = "('" + "','".join(OPEN) + "')"
_WAITING_SQL = "('" + "','".join(WAITING) + "')"


def init_ota_db():
    _init_v1_tables()
    with _connect() as c:
        c.executescript(f"""
        CREATE TABLE IF NOT EXISTS ota2_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS ota2_profiles(
          profile_id TEXT PRIMARY KEY, definition_json TEXT NOT NULL, definition_sha256 TEXT NOT NULL,
          hardware_model TEXT NOT NULL, chip TEXT NOT NULL, ota_slot_size INTEGER NOT NULL,
          partition_table_sha256 TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS ota2_releases(
          release_id TEXT PRIMARY KEY, version TEXT NOT NULL, build_id TEXT NOT NULL, commit_sha TEXT NOT NULL DEFAULT '',
          package_sha256 TEXT NOT NULL, filename TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
          UNIQUE(version, build_id));
        CREATE TABLE IF NOT EXISTS ota2_artifacts(
          artifact_id TEXT PRIMARY KEY, release_id TEXT NOT NULL REFERENCES ota2_releases(release_id),
          profile_id TEXT NOT NULL REFERENCES ota2_profiles(profile_id), artifact_type TEXT NOT NULL,
          version TEXT NOT NULL, build_id TEXT NOT NULL, commit_sha TEXT NOT NULL DEFAULT '', hardware_model TEXT NOT NULL,
          size INTEGER NOT NULL, sha256 TEXT NOT NULL, app_elf_sha256 TEXT NOT NULL, chip TEXT NOT NULL,
          ota_slot_size INTEGER NOT NULL, partition_table_sha256 TEXT NOT NULL, config_schema INTEGER NOT NULL DEFAULT 0,
          bootloader_requirement TEXT NOT NULL DEFAULT '', storage_path TEXT NOT NULL, manifest_json TEXT NOT NULL,
          created_at REAL NOT NULL, UNIQUE(release_id, profile_id));
        CREATE TABLE IF NOT EXISTS ota2_fleet(
          instance_id TEXT NOT NULL, kiosk_id TEXT NOT NULL, kiosk_name TEXT NOT NULL DEFAULT '', station_code TEXT NOT NULL DEFAULT '',
          hardware_model TEXT NOT NULL DEFAULT '', reported_profile TEXT NOT NULL DEFAULT '', assigned_profile TEXT NOT NULL DEFAULT '',
          firmware_version TEXT NOT NULL DEFAULT '', firmware_build TEXT NOT NULL DEFAULT '', running_artifact_id TEXT NOT NULL DEFAULT '',
          online INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, last_seen_at TEXT NOT NULL DEFAULT '',
          last_check_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL, PRIMARY KEY(instance_id, kiosk_id));
        CREATE TABLE IF NOT EXISTS ota2_batches(
          batch_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, release_id TEXT NOT NULL REFERENCES ota2_releases(release_id),
          force INTEGER NOT NULL, allow_downgrade INTEGER NOT NULL DEFAULT 0, scope TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '',
          created_at REAL NOT NULL, cancelled_at REAL);
        CREATE INDEX IF NOT EXISTS ota2_batches_instance ON ota2_batches(instance_id, created_at);
        CREATE TABLE IF NOT EXISTS ota2_targets(
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL REFERENCES ota2_batches(batch_id),
          instance_id TEXT NOT NULL, kiosk_id TEXT NOT NULL, profile_id TEXT NOT NULL DEFAULT '',
          artifact_id TEXT NOT NULL DEFAULT '', artifact_sha256 TEXT NOT NULL DEFAULT '', target_version TEXT NOT NULL DEFAULT '',
          target_build TEXT NOT NULL DEFAULT '', from_version TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
          wait_reason TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', attempt INTEGER NOT NULL DEFAULT 0,
          claim_id TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0, next_retry_at REAL NOT NULL DEFAULT 0,
          progress INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL, finished_at REAL,
          UNIQUE(batch_id, kiosk_id));
        CREATE UNIQUE INDEX IF NOT EXISTS ota2_one_open_target ON ota2_targets(instance_id, kiosk_id) WHERE status IN {_OPEN_SQL};
        CREATE INDEX IF NOT EXISTS ota2_targets_scope ON ota2_targets(instance_id, batch_id, status);
        CREATE TABLE IF NOT EXISTS ota2_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, instance_id TEXT NOT NULL, kiosk_id TEXT NOT NULL, target_id INTEGER,
          event TEXT NOT NULL, applied INTEGER NOT NULL, error TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
          client_event_id TEXT, created_at REAL NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS ota2_events_idem ON ota2_events(instance_id, kiosk_id, client_event_id)
          WHERE client_event_id IS NOT NULL AND client_event_id<>'';
        CREATE INDEX IF NOT EXISTS ota2_events_target ON ota2_events(instance_id, target_id, id);
        INSERT OR IGNORE INTO ota2_meta(key,value,updated_at) VALUES('schema_version','2',{_now()});
        """)


@contextlib.contextmanager
def _tx():
    """Serialized write transaction: claims/state changes never interleave."""
    OTA_ROOT.mkdir(parents=True, exist_ok=True); ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(OTA_DB, timeout=30, isolation_level=None); c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL"); c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK"); raise
    finally:
        c.close()


def _meta(c, key, default=""):
    row = c.execute("SELECT value FROM ota2_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _set_meta(c, key, value):
    c.execute("INSERT INTO ota2_meta(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
              (key, str(value), _now()))


# ---------------------------------------------------------------- instance
def bound_instance():
    init_ota_db()
    with _tx() as c:
        return _meta(c, "bound_instance_id")


def ensure_instance(instance_id):
    """Bind this Agent's OTA store to one MESFlow instance on first contact.

    A different instance later (DB restored on another install, Agent pointed
    at another MESFlow) fails closed until an admin explicitly rebinds.
    """
    instance_id = str(instance_id or "").strip()
    if not instance_id:
        return {"ok": False, "reason": "INSTANCE_UNKNOWN", "bound_instance_id": ""}
    init_ota_db()
    with _tx() as c:
        bound = _meta(c, "bound_instance_id")
        if not bound:
            _set_meta(c, "bound_instance_id", instance_id); _set_meta(c, "bound_at", _now())
            return {"ok": True, "bound_instance_id": instance_id, "newly_bound": True}
        if bound != instance_id:
            return {"ok": False, "reason": "INSTANCE_MISMATCH", "bound_instance_id": bound, "reported_instance_id": instance_id}
        return {"ok": True, "bound_instance_id": bound}


def rebind_instance(instance_id, actor=""):
    instance_id = str(instance_id or "").strip()
    if not instance_id: raise ValueError("INSTANCE_UNKNOWN")
    init_ota_db(); now = _now()
    with _tx() as c:
        old = _meta(c, "bound_instance_id")
        cancelled = c.execute(f"UPDATE ota2_targets SET status='CANCELLED',error='INSTANCE_REBOUND',claim_id='',lease_until=0,updated_at=?,finished_at=? "
                              f"WHERE instance_id<>? AND status IN {_OPEN_SQL}", (now, now, instance_id)).rowcount
        _set_meta(c, "bound_instance_id", instance_id); _set_meta(c, "bound_at", now)
        history = json.loads(_meta(c, "rebind_history", "[]") or "[]")
        history.append({"from": old, "to": instance_id, "actor": str(actor or ""), "at": now, "cancelled_targets": cancelled})
        _set_meta(c, "rebind_history", json.dumps(history[-20:]))
    return {"ok": True, "previous_instance_id": old, "bound_instance_id": instance_id, "cancelled_targets": cancelled}


# ---------------------------------------------------------------- artifacts
def _profile_definition(p):
    if not isinstance(p, dict): raise ValueError("OTA_PROFILE_INVALID")
    missing = [k for k in PROFILE_REQUIRED_KEYS if p.get(k) in (None, "")]
    if missing: raise ValueError("OTA_PROFILE_INVALID")
    if not _SAFE_ID.match(str(p["profile_id"])): raise ValueError("OTA_PROFILE_INVALID")
    if str(p["chip"]) not in CHIP_IDS: raise ValueError("OTA_PROFILE_INVALID")
    d = {k: p.get(k, "") for k in PROFILE_KEYS}
    d["ota_slot_size"] = int(d["ota_slot_size"]); d["min_chip_revision"] = int(d.get("min_chip_revision") or 0)
    text = json.dumps(d, sort_keys=True, separators=(",", ":"))
    return d, text, hashlib.sha256(text.encode()).hexdigest()


def inspect_app_image(data):
    """Read the ESP image header + esp_app_desc_t; reject anything not an app."""
    if len(data) < 32 + 256 or data[0] != 0xE9: raise ValueError("OTA_IMAGE_INVALID")
    chip_id = struct.unpack_from("<H", data, 12)[0]
    min_rev = data[14]
    magic = struct.unpack_from("<I", data, 32)[0]
    if magic != 0xABCD5432: raise ValueError("OTA_IMAGE_NOT_APP")
    desc = 32
    version = data[desc + 16:desc + 48].split(b"\0", 1)[0].decode("utf-8", "replace")
    elf = data[desc + 144:desc + 176].hex()
    m = IDENTITY_RE.search(data)
    identity = None
    if m:
        identity = {"version": m.group(1).decode(), "build_id": m.group(2).decode(), "profile_id": m.group(3).decode(), "hardware_model": m.group(4).decode()}
    return {"chip_id": chip_id, "min_chip_revision": min_rev, "app_desc_version": version, "app_elf_sha256": elf, "identity": identity}


def save_release_package(stream, filename, actor=""):
    """Validate a per-profile release package and store its app artifacts.

    All metadata is cross-checked against the binaries themselves (image
    header chip id, app descriptor, embedded MESFLOW_FW_IDENTITY marker,
    SHA256, slot size); upload file names are never trusted.
    """
    name = Path(filename or "").name
    if not name.lower().endswith(".zip"): raise ValueError("INVALID_OTA_PACKAGE")
    raw = stream.read(MAX_PACKAGE_BYTES + 1)
    if not raw or len(raw) > MAX_PACKAGE_BYTES: raise ValueError("INVALID_OTA_PACKAGE")
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        files = {i.filename: i for i in z.infolist() if not i.is_dir()}
    except zipfile.BadZipFile:
        raise ValueError("INVALID_OTA_PACKAGE")
    for fname in files:
        low = Path(fname).name.lower()
        if fname.startswith("/") or ".." in Path(fname).parts or "merged" in low or "bootloader" in low or "partitions" in low or low == "boot_app0.bin":
            raise ValueError("OTA_PACKAGE_CONTENT_INVALID")
    if "release.json" not in files: raise ValueError("OTA_RELEASE_SCHEMA_INVALID")
    try: rel = json.loads(z.read("release.json").decode("utf-8"))
    except Exception: raise ValueError("OTA_RELEASE_SCHEMA_INVALID")
    if rel.get("schema") != "mesflow-esp-ota-release/2": raise ValueError("OTA_RELEASE_SCHEMA_INVALID")
    version = str(rel.get("version") or "").strip(); build = str(rel.get("build_id") or "").strip()
    if not re.match(r"^\d+\.\d+\.\d+([.-][A-Za-z0-9.-]+)?$", version) or not build or len(build) > 64:
        raise ValueError("OTA_MANIFEST_METADATA_REQUIRED")
    entries = rel.get("artifacts") or []
    if not entries: raise ValueError("OTA_PACKAGE_CONTENT_INVALID")
    listed = {"release.json"}
    for e in entries: listed.update({str(e.get("path") or ""), str(e.get("manifest") or "")})
    if listed != set(files): raise ValueError("OTA_PACKAGE_CONTENT_INVALID")
    seen, prepared = set(), []
    for e in entries:
        pid = str(e.get("profile_id") or "")
        if not pid or pid in seen: raise ValueError("OTA_PACKAGE_CONTENT_INVALID")
        seen.add(pid)
        try: m = json.loads(z.read(e["manifest"]).decode("utf-8"))
        except Exception: raise ValueError("OTA_MANIFEST_INVALID")
        if m.get("schema") != "mesflow-esp-ota-artifact/2": raise ValueError("OTA_MANIFEST_INVALID")
        if m.get("artifact_type") != "app": raise ValueError("OTA_ARTIFACT_TYPE_INVALID")
        if str(m.get("version")) != version or str(m.get("build_id")) != build: raise ValueError("OTA_MANIFEST_RELEASE_MISMATCH")
        pdef, ptext, psha = _profile_definition(m.get("profile"))
        if pdef["profile_id"] != pid: raise ValueError("OTA_MANIFEST_RELEASE_MISMATCH")
        data = z.read(e["path"])
        sha = hashlib.sha256(data).hexdigest()
        if len(data) != int(m.get("size") or -1) or sha != str(m.get("sha256") or "").lower(): raise ValueError("OTA_MANIFEST_SHA_MISMATCH")
        img = inspect_app_image(data)
        if img["chip_id"] != CHIP_IDS[pdef["chip"]]: raise ValueError("OTA_IMAGE_CHIP_MISMATCH")
        if m.get("app_elf_sha256") and str(m["app_elf_sha256"]).lower() != img["app_elf_sha256"]: raise ValueError("OTA_MANIFEST_ELF_MISMATCH")
        ident = img["identity"]
        if not ident: raise ValueError("OTA_IDENTITY_MISSING")
        if ident != {"version": version, "build_id": build, "profile_id": pid, "hardware_model": pdef["hardware_model"]}:
            raise ValueError("OTA_IDENTITY_MISMATCH")
        if len(data) > pdef["ota_slot_size"]: raise ValueError("OTA_IMAGE_TOO_LARGE")
        prepared.append((pid, pdef, ptext, psha, m, data, sha, img))
    package_sha = hashlib.sha256(raw).hexdigest()
    release_id = re.sub(r"[^A-Za-z0-9._+-]", "-", f"esp-kiosk-{version}+{build}")[:120]
    init_ota_db(); now = _now()
    with _tx() as c:
        for pid, pdef, ptext, psha, *_ in prepared:
            row = c.execute("SELECT definition_sha256 FROM ota2_profiles WHERE profile_id=?", (pid,)).fetchone()
            if row and row["definition_sha256"] != psha: raise ValueError("PROFILE_DEFINITION_CONFLICT")
        existing = c.execute("SELECT * FROM ota2_releases WHERE version=? AND build_id=?", (version, build)).fetchone()
        if existing:
            if existing["package_sha256"] != package_sha: raise ValueError("RELEASE_CONFLICT")
            rid = existing["release_id"]
        else:
            rid = release_id
            c.execute("INSERT INTO ota2_releases(release_id,version,build_id,commit_sha,package_sha256,filename,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                      (rid, version, build, str(rel.get("commit") or "")[:64], package_sha, name, str(actor or ""), now))
            for pid, pdef, ptext, psha, m, data, sha, img in prepared:
                c.execute("INSERT OR IGNORE INTO ota2_profiles(profile_id,definition_json,definition_sha256,hardware_model,chip,ota_slot_size,partition_table_sha256,created_at) VALUES(?,?,?,?,?,?,?,?)",
                          (pid, ptext, psha, pdef["hardware_model"], pdef["chip"], pdef["ota_slot_size"], pdef["partition_table_sha256"], now))
                aid = str(uuid.uuid4()); target = ARTIFACT_ROOT / f"{aid}.bin"; tmp = ARTIFACT_ROOT / f".{aid}.tmp"
                tmp.write_bytes(data); tmp.replace(target)
                c.execute("""INSERT INTO ota2_artifacts(artifact_id,release_id,profile_id,artifact_type,version,build_id,commit_sha,hardware_model,size,sha256,
                  app_elf_sha256,chip,ota_slot_size,partition_table_sha256,config_schema,bootloader_requirement,storage_path,manifest_json,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (aid, rid, pid, "app", version, build, str(m.get("commit") or "")[:64], pdef["hardware_model"], len(data), sha,
                           img["app_elf_sha256"], pdef["chip"], pdef["ota_slot_size"], pdef["partition_table_sha256"], int(m.get("config_schema") or 0),
                           str(m.get("bootloader_requirement") or ""), str(target), json.dumps(m, sort_keys=True), now))
    return get_release(rid)


def get_release(release_id):
    init_ota_db()
    with _connect() as c:
        rel = _row(c.execute("SELECT * FROM ota2_releases WHERE release_id=?", (release_id,)).fetchone())
        if not rel: return None
        rel["artifacts"] = [_artifact_public(_row(x)) for x in c.execute("SELECT * FROM ota2_artifacts WHERE release_id=? ORDER BY profile_id", (release_id,))]
        return rel


def _artifact_public(a):
    a = dict(a); a.pop("storage_path", None); a.pop("manifest_json", None); return a


def list_releases(limit=50):
    init_ota_db()
    with _connect() as c:
        ids = [r["release_id"] for r in c.execute("SELECT release_id FROM ota2_releases ORDER BY created_at DESC LIMIT ?", (limit,))]
    return [get_release(i) for i in ids]


def list_profiles():
    init_ota_db()
    with _connect() as c:
        return [dict(_row(r), definition=json.loads(r["definition_json"])) for r in c.execute("SELECT * FROM ota2_profiles ORDER BY profile_id")]


# ---------------------------------------------------------------- fleet
def sync_fleet(instance_id, items):
    """Mirror MESFlow's ACTIVE kiosk inventory for exactly one instance."""
    init_ota_db(); now = _now(); seen = set()
    with _tx() as c:
        for x in items or []:
            kid = str(x.get("kiosk_id") or "").strip()
            if not kid: continue
            seen.add(kid)
            c.execute("""INSERT INTO ota2_fleet(instance_id,kiosk_id,kiosk_name,station_code,hardware_model,reported_profile,firmware_version,firmware_build,
                running_artifact_id,online,active,last_seen_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)
                ON CONFLICT(instance_id,kiosk_id) DO UPDATE SET kiosk_name=excluded.kiosk_name,station_code=excluded.station_code,
                hardware_model=excluded.hardware_model,reported_profile=excluded.reported_profile,firmware_version=excluded.firmware_version,
                firmware_build=excluded.firmware_build,running_artifact_id=CASE WHEN excluded.running_artifact_id<>'' THEN excluded.running_artifact_id ELSE ota2_fleet.running_artifact_id END,
                online=excluded.online,active=1,last_seen_at=excluded.last_seen_at,updated_at=excluded.updated_at""",
                      (instance_id, kid, str(x.get("kiosk_name") or ""), str(x.get("station_code") or ""), str(x.get("hardware_model") or ""),
                       str(x.get("hardware_profile") or ""), str(x.get("firmware_version") or ""), str(x.get("firmware_build") or ""),
                       str(x.get("firmware_artifact_id") or ""), 1 if x.get("online") else 0, str(x.get("last_seen_at") or ""), now))
        gone = [r["kiosk_id"] for r in c.execute("SELECT kiosk_id FROM ota2_fleet WHERE instance_id=? AND active=1", (instance_id,)) if r["kiosk_id"] not in seen]
        for kid in gone:
            c.execute("UPDATE ota2_fleet SET active=0,online=0,updated_at=? WHERE instance_id=? AND kiosk_id=?", (now, instance_id, kid))
            c.execute(f"UPDATE ota2_targets SET status='CANCELLED',error='DEVICE_REMOVED_FROM_INSTANCE',claim_id='',lease_until=0,updated_at=?,finished_at=? "
                      f"WHERE instance_id=? AND kiosk_id=? AND status IN {_OPEN_SQL}", (now, now, instance_id, kid))
        # Offline/online only flips the waiting display; it never marks progress or success.
        c.execute("""UPDATE ota2_targets SET status='WAITING_OFFLINE',updated_at=? WHERE instance_id=? AND status='QUEUED'
            AND kiosk_id IN (SELECT kiosk_id FROM ota2_fleet WHERE instance_id=? AND online=0)""", (now, instance_id, instance_id))
        c.execute("""UPDATE ota2_targets SET status='QUEUED',updated_at=? WHERE instance_id=? AND status='WAITING_OFFLINE'
            AND kiosk_id IN (SELECT kiosk_id FROM ota2_fleet WHERE instance_id=? AND online=1)""", (now, instance_id, instance_id))
    return list_fleet(instance_id)


def assign_profile(instance_id, kiosk_ids, profile_id):
    profile_id = str(profile_id or "").strip()
    init_ota_db(); now = _now()
    with _tx() as c:
        if profile_id and not c.execute("SELECT 1 FROM ota2_profiles WHERE profile_id=?", (profile_id,)).fetchone():
            raise ValueError("PROFILE_UNKNOWN")
        n = 0
        for kid in dict.fromkeys(str(k).strip() for k in kiosk_ids or [] if str(k).strip()):
            n += c.execute("UPDATE ota2_fleet SET assigned_profile=?,updated_at=? WHERE instance_id=? AND kiosk_id=? AND active=1",
                           (profile_id, now, instance_id, kid)).rowcount
    return {"updated": n}


def _effective_profile(dev):
    rep, asg = str(dev["reported_profile"] or ""), str(dev["assigned_profile"] or "")
    if rep and asg and rep != asg: return "", "PROFILE_CONFLICT"
    pid = rep or asg
    return (pid, "") if pid else ("", "PROFILE_REQUIRED")


def list_fleet(instance_id, include_inactive=False):
    init_ota_db()
    with _connect() as c:
        rows = c.execute(f"""SELECT f.*, t.status target_status, t.id target_id FROM ota2_fleet f
            LEFT JOIN ota2_targets t ON t.instance_id=f.instance_id AND t.kiosk_id=f.kiosk_id AND t.status IN {_OPEN_SQL}
            WHERE f.instance_id=? {'' if include_inactive else 'AND f.active=1'} ORDER BY f.kiosk_name,f.kiosk_id""", (instance_id,)).fetchall()
    items = []
    for r in rows:
        d = _row(r); pid, why = _effective_profile(r); d["effective_profile"] = pid; d["profile_issue"] = why; items.append(d)
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------- batches
def _plan(c, instance_id, release_id, kiosk_ids, force, allow_downgrade):
    if not c.execute("SELECT 1 FROM ota2_releases WHERE release_id=?", (release_id,)).fetchone(): raise ValueError("RELEASE_NOT_FOUND")
    devs = c.execute("SELECT * FROM ota2_fleet WHERE instance_id=? AND active=1 ORDER BY kiosk_name,kiosk_id", (instance_id,)).fetchall()
    if kiosk_ids is not None:
        wanted = set(str(k).strip() for k in kiosk_ids)
        devs = [d for d in devs if d["kiosk_id"] in wanted]
    if not devs: raise ValueError("DEVICE_REQUIRED")
    plan = []
    for d in devs:
        item = {"kiosk_id": d["kiosk_id"], "kiosk_name": d["kiosk_name"], "station_code": d["station_code"], "online": bool(d["online"]),
                "current_version": d["firmware_version"], "hardware_model": d["hardware_model"], "profile_id": "", "artifact_id": "",
                "artifact_sha256": "", "target_version": "", "target_build": "", "planned_status": "", "blocked_reason": ""}
        pid, why = _effective_profile(d); item["profile_id"] = pid
        open_t = c.execute(f"SELECT status FROM ota2_targets WHERE instance_id=? AND kiosk_id=? AND status IN {_OPEN_SQL}", (instance_id, d["kiosk_id"])).fetchone()
        arts = c.execute("SELECT a.*,p.hardware_model profile_model FROM ota2_artifacts a JOIN ota2_profiles p ON p.profile_id=a.profile_id WHERE a.release_id=? AND a.profile_id=?",
                         (release_id, pid)).fetchall() if pid else []
        if why: item["blocked_reason"] = why
        elif not arts: item["blocked_reason"] = "INCOMPATIBLE_FIRMWARE"
        elif len(arts) > 1: item["blocked_reason"] = "AMBIGUOUS_ARTIFACT"
        else:
            a = arts[0]
            item.update(artifact_id=a["artifact_id"], artifact_sha256=a["sha256"], target_version=a["version"], target_build=a["build_id"])
            if d["hardware_model"] and d["hardware_model"] != a["profile_model"]: item["blocked_reason"] = "HARDWARE_MODEL_MISMATCH"
            elif not allow_downgrade and d["firmware_version"] and _version_key(a["version"]) < _version_key(d["firmware_version"]): item["blocked_reason"] = "DOWNGRADE_BLOCKED"
            elif open_t and open_t["status"] in IN_FLIGHT: item["blocked_reason"] = "OTA_IN_PROGRESS"
        if item["blocked_reason"]: item["planned_status"] = "BLOCKED"
        elif not force and d["running_artifact_id"] == item["artifact_id"]: item["planned_status"] = "SKIPPED"; item["blocked_reason"] = "SAME_ARTIFACT"
        else: item["planned_status"] = "QUEUED" if d["online"] else "WAITING_OFFLINE"
        plan.append(item)
    return plan


def _summarize(plan, key="planned_status"):
    counts, profiles = {}, {}
    for i in plan:
        counts[i[key]] = counts.get(i[key], 0) + 1
        p = profiles.setdefault(i["profile_id"] or "(none)", {"total": 0, "target_version": i.get("target_version", "")})
        p["total"] += 1
    return counts, profiles


def preview_batch(instance_id, release_id, kiosk_ids=None, force=True, allow_downgrade=False):
    init_ota_db()
    with _tx() as c:
        plan = _plan(c, instance_id, release_id, kiosk_ids, force, allow_downgrade)
    counts, profiles = _summarize(plan)
    return {"instance_id": instance_id, "release": get_release(release_id), "force": bool(force), "total": len(plan),
            "online": sum(1 for i in plan if i["online"]), "offline": sum(1 for i in plan if not i["online"]),
            "counts": counts, "by_profile": profiles, "items": plan}


def _reusable_batch(c, instance_id, release_id, force, plan):
    """The still-waiting batch of the same release/mode that already covers
    every device this command would queue, pinned to the same artifacts."""
    want = [i for i in plan if i["planned_status"] in WAITING]
    if not want: return None
    rows = c.execute(f"""SELECT t.kiosk_id,t.batch_id,t.artifact_id FROM ota2_targets t JOIN ota2_batches b ON b.batch_id=t.batch_id
        WHERE t.instance_id=? AND b.release_id=? AND b.force=? AND t.status IN {_WAITING_SQL}""",
                     (instance_id, release_id, 1 if force else 0)).fetchall()
    open_ = {r["kiosk_id"]: (r["batch_id"], r["artifact_id"]) for r in rows}
    bids = set()
    for i in want:
        o = open_.get(i["kiosk_id"])
        if not o or o[1] != i["artifact_id"]: return None
        bids.add(o[0])
    return bids.pop() if len(bids) == 1 else None


def create_batch(instance_id, release_id, kiosk_ids=None, force=True, allow_downgrade=False, actor=""):
    """Snapshot the current fleet (or a selection) into durable per-device targets."""
    init_ota_db(); now = _now(); bid = str(uuid.uuid4())
    with _tx() as c:
        plan = _plan(c, instance_id, release_id, kiosk_ids, force, allow_downgrade)
        reuse = _reusable_batch(c, instance_id, release_id, force, plan)
    if reuse:
        # Double submit / repeated click while the same command is still
        # waiting: return that batch instead of superseding it.
        b = get_batch(instance_id, reuse); b["reused"] = True
        return b
    with _tx() as c:
        plan = _plan(c, instance_id, release_id, kiosk_ids, force, allow_downgrade)
        c.execute("INSERT INTO ota2_batches(batch_id,instance_id,release_id,force,allow_downgrade,scope,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                  (bid, instance_id, release_id, 1 if force else 0, 1 if allow_downgrade else 0, "all" if kiosk_ids is None else "selected", str(actor or ""), now))
        for i in plan:
            status = i["planned_status"]
            if status in WAITING:
                c.execute(f"UPDATE ota2_targets SET status='CANCELLED',error='SUPERSEDED',claim_id='',lease_until=0,updated_at=?,finished_at=? "
                          f"WHERE instance_id=? AND kiosk_id=? AND status IN {_WAITING_SQL}", (now, now, instance_id, i["kiosk_id"]))
            c.execute("""INSERT INTO ota2_targets(batch_id,instance_id,kiosk_id,profile_id,artifact_id,artifact_sha256,target_version,target_build,from_version,
                status,error,created_at,updated_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (bid, instance_id, i["kiosk_id"], i["profile_id"], i["artifact_id"], i["artifact_sha256"], i["target_version"], i["target_build"],
                       i["current_version"], status, i["blocked_reason"], now, now, now if status in TERMINAL2 else None))
    return get_batch(instance_id, bid)


def get_batch(instance_id, batch_id):
    init_ota_db()
    with _connect() as c:
        b = _row(c.execute("SELECT b.*,r.version,r.build_id FROM ota2_batches b JOIN ota2_releases r ON r.release_id=b.release_id WHERE b.batch_id=? AND b.instance_id=?",
                           (batch_id, instance_id)).fetchone())
        if not b: return None
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status,COUNT(*) n FROM ota2_targets WHERE instance_id=? AND batch_id=? GROUP BY status", (instance_id, batch_id))}
    b["counts"] = counts; b["total"] = sum(counts.values()); b["force"] = bool(b["force"]); b["allow_downgrade"] = bool(b["allow_downgrade"])
    b["open"] = sum(v for k, v in counts.items() if k in OPEN)
    return b


def list_batches(instance_id, limit=50):
    init_ota_db()
    with _connect() as c:
        ids = [r["batch_id"] for r in c.execute("SELECT batch_id FROM ota2_batches WHERE instance_id=? ORDER BY created_at DESC LIMIT ?", (instance_id, limit))]
    return [get_batch(instance_id, i) for i in ids]


def list_targets(instance_id, batch_id=None, kiosk_id=None, status=None, profile_id=None, online=None, q=None, page=1, per_page=50):
    init_ota_db()
    where, args = ["t.instance_id=?"], [instance_id]
    if batch_id: where.append("t.batch_id=?"); args.append(batch_id)
    if kiosk_id: where.append("t.kiosk_id=?"); args.append(kiosk_id)
    if status:
        sts = [s for s in str(status).split(",") if s in STATUSES]
        if sts: where.append("t.status IN (%s)" % ",".join("?" * len(sts))); args += sts
    if profile_id: where.append("t.profile_id=?"); args.append(profile_id)
    if online in (True, False): where.append("COALESCE(f.online,0)=?"); args.append(1 if online else 0)
    if q:
        where.append("(t.kiosk_id LIKE ? OR f.kiosk_name LIKE ? OR f.station_code LIKE ?)"); args += [f"%{q}%"] * 3
    page = max(1, int(page or 1)); per_page = min(200, max(1, int(per_page or 50)))
    sql = " FROM ota2_targets t LEFT JOIN ota2_fleet f ON f.instance_id=t.instance_id AND f.kiosk_id=t.kiosk_id WHERE " + " AND ".join(where)
    with _connect() as c:
        total = c.execute("SELECT COUNT(*)" + sql, args).fetchone()[0]
        rows = c.execute("SELECT t.*,f.kiosk_name,f.station_code,f.online,f.firmware_version current_version,f.last_seen_at" + sql +
                         " ORDER BY t.id DESC LIMIT ? OFFSET ?", args + [per_page, (page - 1) * per_page]).fetchall()
    return {"items": [_row(r) for r in rows], "total": total, "page": page, "per_page": per_page}


def cancel_batch(instance_id, batch_id):
    """Cancel waiting targets only; a device already downloading/flashing finishes safely."""
    init_ota_db(); now = _now()
    with _tx() as c:
        if not c.execute("SELECT 1 FROM ota2_batches WHERE batch_id=? AND instance_id=?", (batch_id, instance_id)).fetchone(): return None
        c.execute(f"UPDATE ota2_targets SET status='CANCELLED',error='JOB_CANCELLED',claim_id='',lease_until=0,updated_at=?,finished_at=? "
                  f"WHERE instance_id=? AND batch_id=? AND status IN {_WAITING_SQL}", (now, now, instance_id, batch_id))
        c.execute("UPDATE ota2_batches SET cancelled_at=? WHERE batch_id=? AND instance_id=?", (now, batch_id, instance_id))
    return get_batch(instance_id, batch_id)


def retry_targets(instance_id, batch_id=None, target_ids=None):
    """Re-queue FAILED/BLOCKED targets of this instance against their batch's release."""
    init_ota_db(); now = _now(); n = 0
    with _tx() as c:
        where, args = ["t.instance_id=?", "t.status IN ('FAILED','BLOCKED')"], [instance_id]
        if batch_id: where.append("t.batch_id=?"); args.append(batch_id)
        if target_ids is not None:
            ids = [int(x) for x in target_ids if str(x).isdigit()]
            if not ids: return {"retried": 0}
            where.append("t.id IN (%s)" % ",".join("?" * len(ids))); args += ids
        rows = c.execute("SELECT t.*,b.release_id,b.force,b.allow_downgrade FROM ota2_targets t JOIN ota2_batches b ON b.batch_id=t.batch_id AND b.instance_id=t.instance_id WHERE "
                         + " AND ".join(where), args).fetchall()
        for t in rows:
            if c.execute(f"SELECT 1 FROM ota2_targets WHERE instance_id=? AND kiosk_id=? AND status IN {_OPEN_SQL}", (instance_id, t["kiosk_id"])).fetchone(): continue
            try: plan = _plan(c, instance_id, t["release_id"], [t["kiosk_id"]], True, bool(t["allow_downgrade"]))[0]
            except ValueError: continue
            if plan["planned_status"] not in WAITING:
                c.execute("UPDATE ota2_targets SET status='BLOCKED',error=?,updated_at=? WHERE id=?", (plan["blocked_reason"], now, t["id"])); continue
            c.execute("""UPDATE ota2_targets SET status=?,profile_id=?,artifact_id=?,artifact_sha256=?,target_version=?,target_build=?,error='',wait_reason='',
                attempt=0,claim_id='',lease_until=0,next_retry_at=0,progress=0,updated_at=?,finished_at=NULL WHERE id=?""",
                      (plan["planned_status"], plan["profile_id"], plan["artifact_id"], plan["artifact_sha256"], plan["target_version"], plan["target_build"], now, t["id"]))
            n += 1
    return {"retried": n}


# ---------------------------------------------------------------- device side
def _open_target(c, instance_id, kid):
    return c.execute(f"""SELECT t.*,b.force,b.allow_downgrade,a.hardware_model art_model,a.size art_size,a.app_elf_sha256,a.storage_path,a.version art_version
        FROM ota2_targets t JOIN ota2_batches b ON b.batch_id=t.batch_id AND b.instance_id=t.instance_id
        LEFT JOIN ota2_artifacts a ON a.artifact_id=t.artifact_id
        WHERE t.instance_id=? AND t.kiosk_id=? AND t.status IN {_OPEN_SQL} ORDER BY t.id DESC LIMIT 1""", (instance_id, kid)).fetchone()


def _set(c, t, now, **fields):
    fields["updated_at"] = now
    if fields.get("status") in TERMINAL2: fields.setdefault("finished_at", now)
    c.execute("UPDATE ota2_targets SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?", list(fields.values()) + [t["id"]])


def _fail(c, t, now, error, retryable=True):
    attempt = int(t["attempt"]) + (1 if t["status"] in WAITING else 0)
    if retryable and error not in NON_RETRYABLE and attempt < MAX_ATTEMPTS:
        delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** max(0, attempt - 1)))
        _set(c, t, now, status="QUEUED", attempt=attempt, error=error, wait_reason="RETRY_BACKOFF", claim_id="", lease_until=0, next_retry_at=now + delay, progress=0)
        return "QUEUED"
    _set(c, t, now, status="FAILED", attempt=attempt, error=error, claim_id="", lease_until=0)
    return "FAILED"


def _offer(t, claim_id, lease_until):
    return {"update_available": True, "target_id": t["id"], "job_id": t["batch_id"], "claim_id": claim_id, "lease_until": lease_until,
            "firmware_id": t["artifact_id"], "artifact_id": t["artifact_id"], "version": t["target_version"], "build": t["target_build"],
            "hardware_model": t["art_model"], "hardware_profile": t["profile_id"], "artifact_type": "app", "size": t["art_size"],
            "sha256": t["artifact_sha256"], "app_elf_sha256": t["app_elf_sha256"], "force": bool(t["force"]), "allow_downgrade": bool(t["allow_downgrade"])}


def device_check(instance_id, kid, report, readiness=None):
    """Authenticated device poll. Idempotent: repeated polls get the same claim."""
    init_ota_db(); now = _now(); report = report or {}; readiness = readiness or {}
    rep_profile = str(report.get("hardware_profile") or "").strip()
    rep_artifact = str(report.get("running_artifact_id") or "").strip()
    rep_version = str(report.get("current_version") or "").strip()
    rep_model = str(report.get("hardware_model") or "").strip()
    with _tx() as c:
        dev = c.execute("SELECT * FROM ota2_fleet WHERE instance_id=? AND kiosk_id=? AND active=1", (instance_id, kid)).fetchone()
        if not dev: return {"update_available": False, "reason": "DEVICE_NOT_IN_FLEET"}
        c.execute("""UPDATE ota2_fleet SET last_check_at=?,firmware_version=CASE WHEN ?<>'' THEN ? ELSE firmware_version END,
            reported_profile=CASE WHEN ?<>'' THEN ? ELSE reported_profile END,running_artifact_id=CASE WHEN ?<>'' THEN ? ELSE running_artifact_id END,
            online=1 WHERE instance_id=? AND kiosk_id=?""", (now, rep_version, rep_version, rep_profile, rep_profile, rep_artifact, rep_artifact, instance_id, kid))
        t = _open_target(c, instance_id, kid)
        if not t: return {"update_available": False}
        st = t["status"]
        if st in ("REBOOTING", "VERIFYING"):
            if rep_artifact and rep_artifact == t["artifact_id"]:
                if st == "REBOOTING": _set(c, t, now, status="VERIFYING", lease_until=now + REBOOT_LEASE_S)
                return {"update_available": False, "reason": "VERIFYING"}
            if t["lease_until"] < now:
                return {"update_available": False, "reason": _fail(c, t, now, "BOOT_NOT_CONFIRMED")}
            return {"update_available": False, "reason": st}
        if st == "FLASHING":
            if t["lease_until"] < now: return {"update_available": False, "reason": _fail(c, t, now, "FLASH_NOT_CONFIRMED")}
            return {"update_available": False, "reason": "FLASHING"}
        if st == "DOWNLOADING":
            if t["lease_until"] < now: return {"update_available": False, "reason": _fail(c, t, now, "DOWNLOAD_TIMEOUT")}
            return _offer(t, t["claim_id"], t["lease_until"])
        if t["next_retry_at"] > now: return {"update_available": False, "reason": "RETRY_BACKOFF"}
        if rep_profile and rep_profile != t["profile_id"]:
            _set(c, t, now, status="BLOCKED", error="PROFILE_MISMATCH", claim_id="", lease_until=0)
            return {"update_available": False, "reason": "PROFILE_MISMATCH"}
        if rep_model and t["art_model"] and rep_model != t["art_model"]:
            _set(c, t, now, status="BLOCKED", error="HARDWARE_MODEL_MISMATCH", claim_id="", lease_until=0)
            return {"update_available": False, "reason": "HARDWARE_MODEL_MISMATCH"}
        if not t["allow_downgrade"] and rep_version and _version_key(t["target_version"]) < _version_key(rep_version):
            _set(c, t, now, status="BLOCKED", error="DOWNGRADE_BLOCKED", claim_id="", lease_until=0)
            return {"update_available": False, "reason": "DOWNGRADE_BLOCKED"}
        if readiness.get("ota_ready") is not True:
            reason = str(readiness.get("reason") or "WAITING_MESFLOW_STATE")
            _set(c, t, now, status="WAITING_SAFE_POINT", wait_reason=reason)
            return {"update_available": False, "reason": reason}
        live = t["claim_id"] and t["lease_until"] > now
        if not live:
            busy = c.execute(f"""SELECT COUNT(*) FROM ota2_targets WHERE instance_id=? AND id<>? AND
                (status IN ('DOWNLOADING','FLASHING') OR (status IN {_WAITING_SQL} AND claim_id<>'' AND lease_until>?))""", (instance_id, t["id"], now)).fetchone()[0]
            if busy >= MAX_CONCURRENT:
                _set(c, t, now, wait_reason="CONCURRENCY_LIMIT", status="QUEUED" if st == "WAITING_OFFLINE" else st)
                return {"update_available": False, "reason": "CONCURRENCY_LIMIT"}
            claim, lease = uuid.uuid4().hex, now + OFFER_LEASE_S
        else:
            claim, lease = t["claim_id"], t["lease_until"]
        _set(c, t, now, claim_id=claim, lease_until=lease, status="WAITING_SAFE_POINT" if st == "WAITING_SAFE_POINT" else "QUEUED",
             wait_reason=t["wait_reason"] if st == "WAITING_SAFE_POINT" else "")
        return _offer(t, claim, lease)


def target_for_download(instance_id, kid, artifact_id, claim_id=None):
    """Authorize one download of the PINNED artifact under a live claim."""
    init_ota_db(); now = _now()
    with _tx() as c:
        t = _open_target(c, instance_id, kid)
        if not t or t["artifact_id"] != artifact_id or not t["claim_id"]: return None
        if claim_id is not None and claim_id != t["claim_id"]: return None
        if t["status"] not in WAITING + ("DOWNLOADING",) or t["lease_until"] <= now: return None
        if t["status"] != "DOWNLOADING":
            _set(c, t, now, status="DOWNLOADING", attempt=int(t["attempt"]) + 1, lease_until=now + DOWNLOAD_LEASE_S, wait_reason="", error="")
        a = _row(c.execute("SELECT * FROM ota2_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone())
    a.update(target_id=t["id"], job_id=t["batch_id"], filename=f"esp-kiosk-{a['version']}-{a['profile_id']}.bin")
    return a


BOOT_EVENTS = {"OTA_BOOT_NEW_VERSION", "OTA_HEALTHCHECK_OK", "OTA_BOOT_OK"}


def record_device_event(instance_id, kid, body):
    init_ota_db(); now = _now(); body = body or {}
    event = str(body.get("status") or body.get("event") or "").upper().strip()[:48]
    if event in ("OTA_CHECK", "OTA_AVAILABLE", ""):
        return {"applied": False, "ignored": True, "duplicate": False}
    client = str(body.get("client_event_id") or "").strip()[:80] or None
    error = str(body.get("error_code") or "").strip()[:64]
    detail = str(body.get("message") or "")[:240]
    fw = str(body.get("firmware_id") or body.get("artifact_id") or "").strip()
    applied, new_status, tid = False, "", None
    with _tx() as c:
        if client and c.execute("SELECT 1 FROM ota2_events WHERE instance_id=? AND kiosk_id=? AND client_event_id=?", (instance_id, kid, client)).fetchone():
            return {"applied": False, "duplicate": True}
        t = _open_target(c, instance_id, kid)
        stale = not t
        if t:
            tid = t["id"]; st = t["status"]
            if body.get("target_id") not in (None, "") and str(body.get("target_id")) != str(t["id"]): stale = True
            if body.get("claim_id") and t["claim_id"] and str(body["claim_id"]) != t["claim_id"]: stale = True
            if fw and fw != t["artifact_id"]: stale = True
            if event in BOOT_EVENTS:
                if fw != t["artifact_id"]: stale = True
                if body.get("to_version") and str(body["to_version"]) != t["target_version"]: stale = True
                if body.get("build_id") and str(body["build_id"]) != t["target_build"]: stale = True
                if body.get("hardware_profile") and str(body["hardware_profile"]) != t["profile_id"]: stale = True
        if not stale:
            if event in ("OTA_WAITING_IDLE", "OTA_BLOCKED_TRANSACTION") and st in WAITING:
                reason = error or ("BLOCKED_TRANSACTION" if event == "OTA_BLOCKED_TRANSACTION" else "") or detail or "DEVICE_BUSY"
                _set(c, t, now, status="WAITING_SAFE_POINT", wait_reason=reason[:64]); new_status = "WAITING_SAFE_POINT"
            elif event == "OTA_DOWNLOAD_START" and (st == "DOWNLOADING" or (st in WAITING and t["claim_id"] and t["lease_until"] > now)):
                _set(c, t, now, status="DOWNLOADING", attempt=int(t["attempt"]) + (0 if st == "DOWNLOADING" else 1),
                     lease_until=now + DOWNLOAD_LEASE_S, wait_reason=""); new_status = "DOWNLOADING"
            elif event in ("OTA_DOWNLOAD_COMPLETE", "OTA_VERIFY_OK") and st in ("DOWNLOADING", "FLASHING"):
                _set(c, t, now, status="FLASHING", progress=100, lease_until=now + DOWNLOAD_LEASE_S); new_status = "FLASHING"
            elif event == "OTA_REBOOTING" and st in ("DOWNLOADING", "FLASHING"):
                _set(c, t, now, status="REBOOTING", progress=100, lease_until=now + REBOOT_LEASE_S); new_status = "REBOOTING"
            elif event == "OTA_BOOT_NEW_VERSION" and st in ("FLASHING", "REBOOTING", "VERIFYING"):
                _set(c, t, now, status="VERIFYING", lease_until=now + REBOOT_LEASE_S); new_status = "VERIFYING"
            elif event in ("OTA_HEALTHCHECK_OK", "OTA_BOOT_OK") and st in ("FLASHING", "REBOOTING", "VERIFYING"):
                _set(c, t, now, status="SUCCESS", error="", wait_reason="", claim_id="", lease_until=0); new_status = "SUCCESS"
                c.execute("UPDATE ota2_fleet SET running_artifact_id=?,firmware_version=?,updated_at=? WHERE instance_id=? AND kiosk_id=?",
                          (t["artifact_id"], t["target_version"], now, instance_id, kid))
            elif event in ("OTA_FAILED", "OTA_VERIFY_FAILED", "OTA_ROLLBACK") and (st in IN_FLIGHT or (st in WAITING and t["claim_id"])):
                new_status = _fail(c, t, now, error or event)
            applied = bool(new_status)
        try:
            c.execute("INSERT INTO ota2_events(instance_id,kiosk_id,target_id,event,applied,error,detail,client_event_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (instance_id, kid, tid, event, 1 if applied else 0, error, detail, client, now))
        except sqlite3.IntegrityError:
            return {"applied": False, "duplicate": True}
    return {"applied": applied, "duplicate": False, "status": new_status}


def list_events(instance_id, target_id, limit=100):
    init_ota_db()
    with _connect() as c:
        return [_row(r) for r in c.execute("SELECT * FROM ota2_events WHERE instance_id=? AND target_id=? ORDER BY id DESC LIMIT ?", (instance_id, int(target_id), limit))]
