import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
os.environ["WORKSHOP_AGENT_HOME"] = tempfile.mkdtemp(prefix="mesflow-ota-test-")
import ota_control


def test_agent_requires_explicit_mesflow_ota_ready():
    assert ota_control.readiness_allows_update({"available": True, "ota_ready": True})
    assert not ota_control.readiness_allows_update({"available": True, "ota_ready": False})
    assert not ota_control.readiness_allows_update({"available": False, "ota_ready": True})


def test_pending_job_is_claimed_when_device_poll_reaches_agent():
    ota_control.init_ota_db()
    firmware_id = "fw-ready-test"
    job_id = "job-ready-test"
    now = ota_control._now()
    with ota_control._connect() as db:
        db.execute(
            "INSERT INTO ota_firmware(id,version,hardware_model,filename,storage_path,size,sha256,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (firmware_id, "5.5.1", "ES3C28P", "kiosk.bin", "/tmp/kiosk.bin", 10, "a" * 64, now),
        )
        db.execute("INSERT INTO ota_jobs(id,firmware_id,created_at,status) VALUES(?,?,?,?)", (job_id, firmware_id, now, "RUNNING"))
        db.execute("INSERT INTO ota_job_devices(job_id,kiosk_id,status,updated_at) VALUES(?,?,?,?)", (job_id, "KIOSK-READY", "PENDING", now))
    result = ota_control.check_update("KIOSK-READY", "5.5.0", "ES3C28P")
    assert result["update_available"] is True
    with ota_control._connect() as db:
        status = db.execute("SELECT status FROM ota_job_devices WHERE job_id=?", (job_id,)).fetchone()[0]
    assert status == "DOWNLOADING"
