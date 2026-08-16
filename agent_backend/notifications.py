"""P3 Notification: critical incident -> notify -> recovery.

Deliberately small and reliable (see reports/P3_NOTIFICATION.md). This does
NOT create a second alert system -- it reacts to the Deploy Agent's own
existing incident engine (agent.py: _incident_conditions()/sync_incidents(),
built for the System Log/Audit Separation task) by reconciling, on every
poll, which ACTIVE incidents still need an OPEN notification dispatched and
which AUTO_RESOLVED/RESOLVED incidents still need a RECOVERY notification
dispatched.

State-based reconciliation (not one-shot event handling) is deliberate: it
is what makes this crash-safe for free. If the process dies between an
incident opening and its notification being persisted as SENT, the next
poll cycle finds the same ACTIVE incident still missing an OPEN delivery
record and retries -- no in-memory queue, no lost alert.

No Flask/deployment dependency, so this is directly unit-testable, matching
the same style as agent_backend/system_health.py.
"""
from __future__ import annotations

import json
import smtplib
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def _meets_min(severity: str, min_severity: str) -> bool:
    return SEVERITY_RANK.get(severity, 9) <= SEVERITY_RANK.get(min_severity, 0)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def environment_label(server_role: str) -> str:
    """SERVER_ROLE (DEV/PRODUCTION_TEST/PRODUCTION, agent.py's existing env
    var) -> the LOCAL/TEST/PRODUCTION vocabulary section 25/41 asks for.
    Never inferred from a hostname string -- always from this configured
    role."""
    role = (server_role or "").strip().upper()
    if role == "PRODUCTION":
        return "PRODUCTION"
    if role in ("PRODUCTION_TEST", "TEST"):
        return "TEST"
    return "LOCAL"


def duration_text(started_at: str, ended_at: str | None = None) -> str:
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at) if ended_at else datetime.now(timezone.utc)
        seconds = max(0, int((end - start).total_seconds()))
    except (TypeError, ValueError):
        return ""
    if seconds < 60:
        return f"{seconds} giây"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} phút"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}g {minutes}p"


@dataclass(frozen=True)
class IncidentNotification:
    """Normalized command handed to a channel -- channels never see raw
    detector/incident-store internals (section 7)."""
    incident_id: str
    fingerprint: str
    incident_type: str
    severity: str
    title: str
    message: str
    component: str
    environment: str
    occurred_at: str
    recovery: bool
    duration: str = ""
    status_text: str = ""


def format_message(note: IncidentNotification) -> str:
    """Short, operational text (section 8/9) -- never logs or stack traces."""
    if note.recovery:
        lines = [
            f"[RECOVERED] {note.title}",
            "",
            f"Environment: {note.environment}",
            f"Recovered: {note.occurred_at}",
            f"Incident duration: {note.duration}",
        ]
        if note.status_text:
            lines += ["", "Current status:", note.status_text]
        lines += ["", f"Incident: #{note.incident_id}"]
    else:
        lines = [
            f"[{note.severity}] {note.title}",
            "",
            f"Environment: {note.environment}",
            f"Detected: {note.occurred_at}",
            f"Duration: {note.duration}",
            "",
            note.message,
            "",
            f"Incident: #{note.incident_id}",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------

class WebChannel:
    name = "WEB"

    def configured(self) -> bool:
        return True

    def send(self, note: IncidentNotification) -> tuple[str, str]:
        # The web channel's "delivery" is simply recording the notification
        # (see NotificationDispatcher) -- Operations Center reads the same
        # store to render the badge/unread count. Nothing to transmit.
        return "SENT", ""


class TelegramChannel:
    name = "TELEGRAM"

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 8.0):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout

    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, note: IncidentNotification) -> tuple[str, str]:
        text = format_message(note)
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        req = urllib.request.Request(
            url,
            data=json.dumps({"chat_id": self.chat_id, "text": text[:4000]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ok = bool(json.loads(resp.read()).get("ok"))
            return ("SENT" if ok else "FAILED"), ("" if ok else "TELEGRAM_NOT_OK")
        except Exception as exc:  # noqa: BLE001 - channel failures must never propagate (section 15)
            return "FAILED", type(exc).__name__


class EmailChannel:
    name = "EMAIL"

    def __init__(self, host: str, port: int, user: str, password: str, use_tls: bool,
                 mail_from: str, mail_to: str, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.use_tls = use_tls
        self.mail_from = mail_from
        self.mail_to = mail_to
        self.timeout = timeout

    def configured(self) -> bool:
        return bool(self.host and self.mail_from and self.mail_to)

    def send(self, note: IncidentNotification) -> tuple[str, str]:
        subject_tag = "RECOVERED" if note.recovery else note.severity
        subject = f"[MESFlow][{subject_tag}] {note.title}"
        body = format_message(note)
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = self.mail_from
        msg["To"] = self.mail_to
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.sendmail(self.mail_from, [x.strip() for x in self.mail_to.split(",") if x.strip()], msg.as_string())
            return "SENT", ""
        except Exception as exc:  # noqa: BLE001 - channel failures must never propagate (section 15)
            return "FAILED", type(exc).__name__


# ---------------------------------------------------------------------
# Dispatcher
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


class NotificationDispatcher:
    """Reconciles incident-store state against notification-delivery state.

    Call `reconcile(incidents)` once per poll cycle with the current
    `{fingerprint: incident_dict}` snapshot (agent.py's sync_incidents()
    return value). Idempotent and crash-safe by construction: delivery
    identity is `incident_id:channel:notification_type`, so re-running this
    on the same incident state never re-sends an already-SENT/SKIPPED
    notification (section 11/12/34), and a still-pending one (process died
    mid-send) is retried on the next call.
    """

    def __init__(self, channels: dict[str, Any], store_path: Path, environment: str,
                 min_severity: dict[str, str] | None = None, max_attempts: int = 3):
        self.channels = channels
        self.store_path = store_path
        self.environment = environment
        # section 5: CRITICAL -> web+telegram+email; HIGH -> web+telegram
        # (email configurable); MEDIUM -> web only; LOW -> none.
        self.min_severity = min_severity or {"TELEGRAM": "HIGH", "EMAIL": "CRITICAL"}
        self.max_attempts = max_attempts
        self._lock = threading.Lock()

    def _load(self) -> dict:
        with self._lock:
            return load_json(self.store_path, {"items": {}})

    def _save(self, store: dict) -> None:
        with self._lock:
            save_json(self.store_path, store)

    def _plan(self, severity: str) -> list[str]:
        if severity == "LOW":
            return []
        plan = ["WEB"]
        for ch_name in ("TELEGRAM", "EMAIL"):
            min_sev = self.min_severity.get(ch_name)
            if min_sev and _meets_min(severity, min_sev):
                plan.append(ch_name)
        return plan

    def _build_note(self, incident: dict, recovery: bool) -> IncidentNotification:
        return IncidentNotification(
            incident_id=incident["incident_id"], fingerprint=incident["fingerprint"],
            incident_type=incident["incident_type"], severity=incident["severity"],
            title=incident["summary"], message=incident.get("technical_reason", ""),
            component=incident.get("service") or incident.get("server") or "", environment=self.environment,
            occurred_at=(incident.get("resolved_at") if recovery else incident.get("started_at")) or now_iso(),
            recovery=recovery,
            duration=duration_text(incident.get("started_at"), incident.get("resolved_at") if recovery else None),
            status_text="HEALTHY" if recovery else "",
        )

    def _dispatch_one(self, incident: dict, notification_type: str) -> list[dict]:
        note = self._build_note(incident, recovery=(notification_type == "RECOVERY"))
        results = []
        for ch_name in self._plan(incident["severity"]):
            channel = self.channels.get(ch_name)
            if channel is None:
                continue
            identity = f"{incident['incident_id']}:{ch_name}:{notification_type}"
            store = self._load()
            existing = store["items"].get(identity)
            if existing and existing.get("status") in ("SENT", "SKIPPED"):
                results.append(existing)
                continue  # already delivered (or deliberately skipped) -- idempotent, never re-send (section 11/12)
            if not channel.configured():
                record = {"identity": identity, "incident_id": incident["incident_id"], "fingerprint": incident["fingerprint"],
                          "channel": ch_name, "notification_type": notification_type, "status": "SKIPPED",
                          "attempted_at": now_iso(), "sent_at": None, "error": "NOT_CONFIGURED", "attempts": 0}
                store["items"][identity] = record
                self._save(store)
                results.append(record)
                continue
            status, error, attempts = self._attempt(channel, note)
            store = self._load()  # re-read: another thread may have written meanwhile
            record = {"identity": identity, "incident_id": incident["incident_id"], "fingerprint": incident["fingerprint"],
                      "channel": ch_name, "notification_type": notification_type, "status": status,
                      "attempted_at": now_iso(), "sent_at": now_iso() if status == "SENT" else None,
                      "error": error, "attempts": attempts}
            store["items"][identity] = record
            self._save(store)
            results.append(record)
        return results

    def _attempt(self, channel: Any, note: IncidentNotification) -> tuple[str, str, int]:
        status, error = "FAILED", ""
        attempts = 0
        for attempts in range(1, self.max_attempts + 1):
            status, error = channel.send(note)
            if status in ("SENT", "SKIPPED"):
                break
            if attempts < self.max_attempts:
                time.sleep(min(attempts, 2))
        return status, error, attempts

    def reconcile(self, incidents: dict[str, dict]) -> dict[str, list[dict]]:
        """incidents: {fingerprint: incident_dict} from sync_incidents().
        Returns {incident_id: [delivery_records]} for whatever was
        newly-dispatched or already-on-record this call (diagnostic value
        only; callers do not need to branch on it)."""
        out: dict[str, list[dict]] = {}
        for incident in incidents.values():
            status = incident.get("status")
            if status in ("ACTIVE", "ACKNOWLEDGED"):
                out[incident["incident_id"]] = self._dispatch_one(incident, "OPEN")
            elif status in ("AUTO_RESOLVED", "RESOLVED"):
                out[incident["incident_id"]] = self._dispatch_one(incident, "RECOVERY")
        return out

    def deliveries_for(self, incident_id: str) -> list[dict]:
        store = self._load()
        return sorted(
            (v for v in store["items"].values() if v["incident_id"] == incident_id),
            key=lambda r: r.get("attempted_at") or "",
        )

    def channel_status(self) -> dict[str, dict]:
        return {name: {"configured": ch.configured()} for name, ch in self.channels.items()}

    def test_send(self, channel_name: str) -> dict:
        """Admin-only explicit test send (section 31) -- never a fabricated
        incident, stored under a synthetic TEST incident id so it never
        collides with real delivery identities."""
        channel = self.channels.get(channel_name)
        if not channel:
            return {"ok": False, "error": "UNKNOWN_CHANNEL"}
        note = IncidentNotification(
            incident_id=f"test-{int(time.time())}", fingerprint="TEST", incident_type="TEST", severity="LOW",
            title="MESFlow notification test", message="Đây là tin nhắn kiểm tra kênh thông báo MESFlow.",
            component="test", environment=self.environment, occurred_at=now_iso(), recovery=False, duration="0 giây",
        )
        if not channel.configured():
            return {"ok": False, "status": "NOT_CONFIGURED", "error": "NOT_CONFIGURED"}
        status, error, attempts = self._attempt(channel, note)
        return {"ok": status == "SENT", "status": status, "error": error, "attempts": attempts}
