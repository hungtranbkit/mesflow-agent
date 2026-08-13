"""Small, Agent-owned ESP OTA store.

The operator flow is deliberately one action: upload a .bin, select kiosks,
and flash.  There is no draft/activate/stage/deployment policy model here.
"""
from __future__ import annotations

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
def _connect():
    OTA_ROOT.mkdir(parents=True, exist_ok=True); ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(OTA_DB, timeout=15); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); return c
def _row(row): return dict(row) if row else None
def _version_key(value):
    nums = re.findall(r"\d+", str(value or "")); return tuple(int(x) for x in nums) if nums else (0,)


def init_ota_db():
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


init_ota_db()
