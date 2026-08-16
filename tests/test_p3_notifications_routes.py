"""P3 Notification: Flask-route-level wiring -- the incident monitor loop's
reconcile() call, and the three new API routes, against a real (isolated)
Agent instance. Complements test_p3_notifications_unit.py, which covers the
dispatcher logic itself without Flask.
"""
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def agent(monkeypatch, tmp_path):
    import os
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["WORKSHOP_AGENT_HOME"] = str(tmp_path)
    os.environ.setdefault("MESFLOW_AGENT_RUNTIME", "linux")
    os.environ.setdefault("MESFLOW_AGENT_ADMIN_PASSWORD", "TestPw1234567")
    os.environ["SERVER_ROLE"] = "PRODUCTION_TEST"
    os.environ.pop("MESFLOW_TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("MESFLOW_TELEGRAM_CHAT_ID", None)
    os.environ.pop("MESFLOW_SMTP_HOST", None)
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("agent_p3_notifications", root / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_p3_notifications"] = mod
    spec.loader.exec_module(mod)
    mod.app.testing = True
    return mod


def _client(agent):
    client = agent.app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "test-csrf"
    return client


def test_notification_channels_endpoint_reports_web_always_configured(agent):
    client = _client(agent)
    r = client.get("/api/notification-channels")
    assert r.status_code == 200
    body = r.get_json()
    assert body["channels"]["WEB"]["configured"] is True
    assert body["channels"]["TELEGRAM"]["configured"] is False  # unset in this test env
    assert body["channels"]["EMAIL"]["configured"] is False
    assert body["environment"] == "TEST"  # SERVER_ROLE=PRODUCTION_TEST -> TEST label


def test_notification_channels_requires_auth(agent):
    client = agent.app.test_client()  # no session
    r = client.get("/api/notification-channels")
    assert r.status_code == 401


def test_test_send_requires_real_login_and_csrf(agent):
    client = agent.app.test_client()
    r = client.post("/api/notification-channels/WEB/test")
    assert r.status_code in (302, 401)  # redirected to login or rejected -- never silently allowed


def test_test_send_admin_only_never_creates_a_fake_incident(agent):
    client = _client(agent)
    r = client.post("/api/notification-channels/WEB/test", headers={"X-CSRF-Token": "test-csrf"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["status"] == "SENT"
    # no incident was created by the test send
    assert client.get("/api/incidents").get_json()["items"] == []


def test_incident_monitor_reconcile_creates_and_exposes_notifications(agent):
    """End-to-end through the real dispatcher instance the app wires up --
    not a mock -- proving sync_incidents() -> reconcile() -> the API
    actually agree with each other."""
    client = _client(agent)
    conditions = [("MESFLOW_DOWN", "SERVICE_DOWN", "CRITICAL", "health-monitor", "host1", "mesflow",
                    "MESFlow application unreachable", "api_online=False", {})]
    items = agent.sync_incidents(conditions)
    agent._notification_dispatcher.reconcile(items)
    incident_id = items["MESFLOW_DOWN"]["incident_id"]

    r = client.get(f"/api/incidents/{incident_id}/notifications")
    assert r.status_code == 200
    deliveries = r.get_json()["items"]
    assert any(d["channel"] == "WEB" and d["notification_type"] == "OPEN" and d["status"] == "SENT" for d in deliveries)

    # condition recovers
    recovered_items = agent.sync_incidents([])
    agent._notification_dispatcher.reconcile(recovered_items)
    r2 = client.get(f"/api/incidents/{incident_id}/notifications")
    deliveries2 = r2.get_json()["items"]
    assert any(d["notification_type"] == "RECOVERY" and d["status"] == "SENT" for d in deliveries2)
    # WEB is the only configured channel in this test env -- exactly one
    # OPEN and one RECOVERY delivery for it, never duplicated across the
    # two reconcile() calls (TELEGRAM/EMAIL also appear once each, as
    # SKIPPED/NOT_CONFIGURED records -- one row per channel is correct).
    web_deliveries = [d for d in deliveries2 if d["channel"] == "WEB"]
    assert sum(1 for d in web_deliveries if d["notification_type"] == "OPEN") == 1
    assert sum(1 for d in web_deliveries if d["notification_type"] == "RECOVERY") == 1
    assert len(deliveries2) == 6  # 3 channels x (OPEN + RECOVERY), never more


def test_ops_summary_exposes_notification_subsystem_health(agent):
    client = _client(agent)
    agent._notification_health["last_run_at"] = "2026-08-14T05:10:00"
    agent._notification_health["last_success_at"] = "2026-08-14T05:10:00"
    r = client.get("/api/ops/summary")
    assert r.status_code == 200
    body = r.get_json()
    assert "notifications" in body
    assert body["notifications"]["last_run_at"] == "2026-08-14T05:10:00"
    assert "channels" in body["notifications"]
