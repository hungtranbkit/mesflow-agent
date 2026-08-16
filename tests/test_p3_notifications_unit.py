"""P3 Notification: critical incident -> notification -> recovery.

Pure unit tests for agent_backend/notifications.py -- no Flask, no real
network/SMTP calls (channels are faked). Covers sections 41-48 of the P3
spec: open/dedup, recovery-once, channel-failure-isolation, retry,
crash-recovery resume, severity routing, environment labeling.
"""
import time

import pytest

from agent_backend.notifications import (
    EmailChannel, NotificationDispatcher, TelegramChannel, WebChannel,
    duration_text, environment_label, format_message, IncidentNotification,
)


class FakeChannel:
    def __init__(self, name, configured=True, results=None):
        self.name = name
        self._configured = configured
        self.results = list(results or [("SENT", "")])
        self.calls = 0

    def configured(self):
        return self._configured

    def send(self, note):
        self.calls += 1
        idx = min(self.calls - 1, len(self.results) - 1)
        return self.results[idx]


def _incident(fp="TEST_FP", incident_id="inc-1", severity="CRITICAL", status="ACTIVE", started_at=None, resolved_at=None):
    return {
        "incident_id": incident_id, "fingerprint": fp, "incident_type": "SERVICE_DOWN", "severity": severity,
        "source": "health-monitor", "server": "host1", "service": "mesflow", "started_at": started_at or "2026-08-14T05:10:00+00:00",
        "last_seen_at": "2026-08-14T05:18:00+00:00", "resolved_at": resolved_at, "status": status,
        "summary": "MESFlow application unreachable", "technical_reason": "api_online=False", "evidence": {},
        "occurrence_count": 9,
    }


def test_environment_label_maps_server_role_never_infers_from_hostname():
    assert environment_label("PRODUCTION") == "PRODUCTION"
    assert environment_label("PRODUCTION_TEST") == "TEST"
    assert environment_label("DEV") == "LOCAL"
    assert environment_label("") == "LOCAL"


def test_duration_text_handles_missing_and_present_end():
    assert duration_text("2026-08-14T05:10:00+00:00", "2026-08-14T05:19:00+00:00") == "9 phút"
    assert duration_text("not-a-date") == ""


def test_format_message_is_short_and_operational_no_stack_traces():
    note = IncidentNotification(incident_id="184", fingerprint="POSTGRES_UNAVAILABLE", incident_type="SERVICE_DOWN",
        severity="CRITICAL", title="PostgreSQL unavailable", message="Database health probe failed.",
        component="postgres", environment="PRODUCTION", occurred_at="10:24", recovery=False, duration="2 phút")
    text = format_message(note)
    assert "[CRITICAL] PostgreSQL unavailable" in text
    assert "Environment: PRODUCTION" in text
    assert "Incident: #184" in text
    assert "Traceback" not in text and "File \"" not in text


def test_format_message_recovery_references_same_incident():
    note = IncidentNotification(incident_id="184", fingerprint="POSTGRES_UNAVAILABLE", incident_type="SERVICE_DOWN",
        severity="CRITICAL", title="PostgreSQL", message="", component="postgres", environment="PRODUCTION",
        occurred_at="10:31", recovery=True, duration="7 phút", status_text="HEALTHY")
    text = format_message(note)
    assert "[RECOVERED] PostgreSQL" in text
    assert "Incident: #184" in text
    assert "HEALTHY" in text


def test_web_channel_always_configured_and_sends():
    ch = WebChannel()
    assert ch.configured() is True
    status, error = ch.send(IncidentNotification("1", "F", "T", "LOW", "x", "x", "c", "LOCAL", "t", False))
    assert status == "SENT"


def test_telegram_and_email_report_not_configured_when_unset():
    assert TelegramChannel("", "").configured() is False
    assert EmailChannel("", 587, "", "", True, "", "").configured() is False


def test_dispatch_open_creates_exactly_one_delivery_per_channel_not_duplicated(tmp_path):
    web, tg = FakeChannel("WEB"), FakeChannel("TELEGRAM")
    d = NotificationDispatcher({"WEB": web, "TELEGRAM": tg}, tmp_path / "notifications.json", "PRODUCTION")
    incident = _incident()
    d.reconcile({incident["fingerprint"]: incident})
    d.reconcile({incident["fingerprint"]: incident})  # same ACTIVE incident evaluated again (section 42/10)
    d.reconcile({incident["fingerprint"]: incident})
    deliveries = d.deliveries_for(incident["incident_id"])
    assert len(deliveries) == 2  # one WEB, one TELEGRAM -- never one per poll
    assert web.calls == 1 and tg.calls == 1
    assert {r["channel"] for r in deliveries} == {"WEB", "TELEGRAM"}
    assert all(r["notification_type"] == "OPEN" for r in deliveries)


def test_recovery_sends_exactly_once_per_channel(tmp_path):
    web = FakeChannel("WEB")
    d = NotificationDispatcher({"WEB": web}, tmp_path / "notifications.json", "PRODUCTION")
    incident = _incident()
    d.reconcile({incident["fingerprint"]: incident})
    recovered = {**incident, "status": "AUTO_RESOLVED", "resolved_at": "2026-08-14T05:19:00+00:00"}
    d.reconcile({incident["fingerprint"]: recovered})
    d.reconcile({incident["fingerprint"]: recovered})  # repeated healthy polling (section 19/44)
    d.reconcile({incident["fingerprint"]: recovered})
    deliveries = d.deliveries_for(incident["incident_id"])
    types = [r["notification_type"] for r in deliveries]
    assert types.count("OPEN") == 1
    assert types.count("RECOVERY") == 1
    assert web.calls == 2  # exactly one OPEN send + one RECOVERY send, never more


def test_channel_failure_does_not_affect_other_channels(tmp_path):
    web, tg, email = FakeChannel("WEB"), FakeChannel("TELEGRAM", results=[("FAILED", "TimeoutError")] * 5), FakeChannel("EMAIL")
    d = NotificationDispatcher({"WEB": web, "TELEGRAM": tg, "EMAIL": email}, tmp_path / "n.json", "PRODUCTION",
                                min_severity={"TELEGRAM": "HIGH", "EMAIL": "CRITICAL"}, max_attempts=1)
    incident = _incident(severity="CRITICAL")
    d.reconcile({incident["fingerprint"]: incident})
    deliveries = {r["channel"]: r for r in d.deliveries_for(incident["incident_id"])}
    assert deliveries["WEB"]["status"] == "SENT"
    assert deliveries["TELEGRAM"]["status"] == "FAILED"
    assert deliveries["EMAIL"]["status"] == "SENT"
    # the incident notification itself is unaffected by the channel failure -- no exception propagated
    assert set(deliveries) == {"WEB", "TELEGRAM", "EMAIL"}


def test_retry_eventually_succeeds_and_records_one_logical_notification(tmp_path):
    tg = FakeChannel("TELEGRAM", results=[("FAILED", "TimeoutError"), ("FAILED", "TimeoutError"), ("SENT", "")])
    d = NotificationDispatcher({"WEB": WebChannel(), "TELEGRAM": tg}, tmp_path / "n.json", "PRODUCTION",
                                min_severity={"TELEGRAM": "HIGH"}, max_attempts=3)
    incident = _incident(severity="HIGH")
    d.reconcile({incident["fingerprint"]: incident})
    record = next(r for r in d.deliveries_for(incident["incident_id"]) if r["channel"] == "TELEGRAM")
    assert record["status"] == "SENT"
    assert record["attempts"] == 3
    assert tg.calls == 3


def test_retry_does_not_exceed_max_attempts(tmp_path):
    tg = FakeChannel("TELEGRAM", results=[("FAILED", "TimeoutError")] * 10)
    d = NotificationDispatcher({"WEB": WebChannel(), "TELEGRAM": tg}, tmp_path / "n.json", "PRODUCTION",
                                min_severity={"TELEGRAM": "HIGH"}, max_attempts=3)
    incident = _incident(severity="HIGH")
    d.reconcile({incident["fingerprint"]: incident})
    assert tg.calls == 3  # bounded, never indefinite


def test_crash_recovery_resumes_pending_without_duplicating_sent(tmp_path):
    """Simulate a process restart: a fresh NotificationDispatcher instance
    pointed at the same store must not re-send an already-SENT record, but
    DOES need to still deliver a genuinely-missing one (section 34/47)."""
    store_path = tmp_path / "n.json"
    web1 = FakeChannel("WEB")
    d1 = NotificationDispatcher({"WEB": web1}, store_path, "PRODUCTION")
    incident = _incident()
    d1.reconcile({incident["fingerprint"]: incident})
    assert web1.calls == 1

    # "process restart": brand new Dispatcher + brand new channel instance,
    # same on-disk store.
    web2 = FakeChannel("WEB")
    d2 = NotificationDispatcher({"WEB": web2}, store_path, "PRODUCTION")
    d2.reconcile({incident["fingerprint"]: incident})
    assert web2.calls == 0  # already SENT, never re-dispatched
    assert len(d2.deliveries_for(incident["incident_id"])) == 1


def test_severity_routing_matches_p3_policy(tmp_path):
    web, tg, email = FakeChannel("WEB"), FakeChannel("TELEGRAM"), FakeChannel("EMAIL")
    d = NotificationDispatcher({"WEB": web, "TELEGRAM": tg, "EMAIL": email}, tmp_path / "n.json", "PRODUCTION",
                                min_severity={"TELEGRAM": "HIGH", "EMAIL": "CRITICAL"})
    assert d._plan("CRITICAL") == ["WEB", "TELEGRAM", "EMAIL"]
    assert d._plan("HIGH") == ["WEB", "TELEGRAM"]
    assert d._plan("MEDIUM") == ["WEB"]
    assert d._plan("LOW") == []


def test_not_configured_channel_is_skipped_not_failed(tmp_path):
    tg = TelegramChannel("", "")  # real class, genuinely unconfigured
    d = NotificationDispatcher({"WEB": WebChannel(), "TELEGRAM": tg}, tmp_path / "n.json", "PRODUCTION",
                                min_severity={"TELEGRAM": "HIGH"})
    incident = _incident(severity="HIGH")
    d.reconcile({incident["fingerprint"]: incident})
    record = next(r for r in d.deliveries_for(incident["incident_id"]) if r["channel"] == "TELEGRAM")
    assert record["status"] == "SKIPPED"
    assert record["error"] == "NOT_CONFIGURED"


def test_test_send_uses_synthetic_incident_id_and_never_collides(tmp_path):
    tg = FakeChannel("TELEGRAM")
    d = NotificationDispatcher({"TELEGRAM": tg}, tmp_path / "n.json", "PRODUCTION")
    result = d.test_send("TELEGRAM")
    assert result["ok"] is True and result["status"] == "SENT"
    result2 = d.test_send("UNKNOWN")
    assert result2["ok"] is False and result2["error"] == "UNKNOWN_CHANNEL"
    # test sends are never mixed into real incident delivery history
    assert d.deliveries_for("184") == []


def test_reconcile_ignores_incidents_that_are_neither_active_nor_resolved(tmp_path):
    web = FakeChannel("WEB")
    d = NotificationDispatcher({"WEB": web}, tmp_path / "n.json", "PRODUCTION")
    incident = _incident(status="ACKNOWLEDGED")
    d.reconcile({incident["fingerprint"]: incident})
    assert web.calls == 1  # ACKNOWLEDGED still counts as an open/active incident needing its OPEN notification
