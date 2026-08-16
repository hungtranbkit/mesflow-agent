"""P5 Multi-server: Server Registry + Fleet Health, control-plane side.

Architecture (spec section 63): ServerRegistryService owns server identity/
lifecycle; FleetHealthService polls registered servers through an injected
client callable and caches results with per-server bounded timeouts and
failure isolation, so one offline server never blocks the rest of the fleet
(section 46). Kept decoupled from Flask/urllib -- like agent_backend/
db_backup.py and agent_backend/notifications.py -- so this module is
directly unit-testable without a real HTTP stack.

Communication itself deliberately reuses this project's EXISTING, already
tested Agent-to-Agent session/CSRF mechanism (agent.py's
_remote_agent_client / _remote_agent_get_json / _remote_agent_upload /
_remote_agent_deploy -- the same code scripts/promote-test.sh has used
since before this task) rather than inventing a parallel token protocol
(spec section 9: "use the project's existing secure communication mechanism
if already available"). Each registered server's credential is its own
admin username + password -- a genuine per-server credential, generalized
from the pre-existing two fixed env-var-configured targets
(MESFLOW_PRODUCTION_AGENT_USER/PASSWORD etc, still honored for backward
compatibility) into an arbitrary, persisted registry. Agent self-update
already has its own separate bearer-token protocol (_agent_updater_target/
_push_agent_update) which this module leaves untouched and reuses as-is.
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ENVIRONMENTS = ("DEV", "TEST", "PRODUCTION", "OTHER")
REGISTRATION_STATUSES = ("PENDING", "ACTIVE", "DISABLED", "UNREACHABLE", "REVOKED")
HEALTH_STATUSES = ("ONLINE", "DEGRADED", "OFFLINE", "UNKNOWN")
# Open-ended by design (section 5): a server may carry roles outside this
# list -- these are just the ones named in the spec, not an enum to enforce.
KNOWN_ROLES = ("MESFLOW", "POSTGRESQL", "GATEWAY", "QA", "DEPLOY_AGENT", "BACKUP",
               "STORAGE", "KIOSK_GATEWAY", "UTILITY", "CONTROL")

# Normalized remote-failure categories (section 65) -- the UI shows one of
# these, never a raw exception string as the primary signal.
ERROR_UNREACHABLE = "REMOTE_AGENT_UNREACHABLE"
ERROR_AUTH_FAILED = "REMOTE_AUTH_FAILED"
ERROR_TIMEOUT = "REMOTE_TIMEOUT"
ERROR_CAPABILITY_UNSUPPORTED = "REMOTE_CAPABILITY_UNSUPPORTED"
ERROR_OPERATION_FAILED = "REMOTE_OPERATION_FAILED"
ERROR_VERSION_INCOMPATIBLE = "REMOTE_VERSION_INCOMPATIBLE"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def new_server_id() -> str:
    # Stable, opaque, never derived from hostname/IP (section 58: identity
    # must survive a hostname/IP change for what is genuinely the same host).
    return "srv-" + uuid.uuid4().hex[:16]


class RegistryError(Exception):
    pass


class ProductionConfirmationRequired(RegistryError):
    """Raised when registering/reclassifying a server as PRODUCTION without
    the explicit confirmation flag (section 61)."""


class DuplicateAgentIdentity(RegistryError):
    """Raised when a handshake reports an agent_id already claimed by a
    DIFFERENT, still-active server record -- one Agent must not silently
    end up representing two unrelated registry entries (section 8)."""


@dataclass(frozen=True)
class RemoteCallResult:
    ok: bool
    server_id: str
    operation: str
    data: Any = None
    error: str = ""
    error_code: str = ""
    correlation_id: str = field(default_factory=new_correlation_id)
    duration_ms: int = 0


class ServerRegistryService:
    """Persisted server registry -- one JSON-file-backed store, matching the
    incidents.json/notifications.json/db_backups.json convention. Never a
    hard delete (section 57): disable/revoke only, history stays attached."""

    def __init__(self, store_path: Path, audit: Callable[[str, dict], None] | None = None):
        self.store_path = store_path
        self.audit = audit or (lambda action, data: None)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        with self._lock:
            try:
                return json.loads(Path(self.store_path).read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                return {"items": {}}

    def _save(self, store: dict) -> None:
        with self._lock:
            path = Path(self.store_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)

    def list(self, environment: str | None = None, status: str | None = None,
              enabled: bool | None = None) -> list[dict]:
        items = list(self._load()["items"].values())
        if environment:
            items = [s for s in items if s.get("environment") == environment]
        if status:
            items = [s for s in items if s.get("registration_status") == status]
        if enabled is not None:
            items = [s for s in items if bool(s.get("enabled")) == enabled]
        items.sort(key=lambda s: (s.get("environment") != "DEV", s.get("name") or ""))
        return [self._redacted(s) for s in items]

    def get(self, server_id: str) -> dict | None:
        record = self._load()["items"].get(server_id)
        return self._redacted(record) if record else None

    def get_raw(self, server_id: str) -> dict | None:
        """Internal use only (e.g. RemoteAgentClient needs the real
        credential to make the call) -- never expose this return value to
        an API response or audit payload."""
        return self._load()["items"].get(server_id)

    def get_by_agent_id(self, agent_id: str) -> dict | None:
        if not agent_id:
            return None
        for s in self._load()["items"].values():
            if s.get("agent_id") == agent_id and s.get("registration_status") != "REVOKED":
                return self._redacted(s)
        return None

    def register(self, *, name: str, environment: str, roles: list[str] | None = None,
                 agent_url: str = "", agent_user: str = "", agent_password: str = "",
                 hostname: str = "", tags: list[str] | None = None,
                 production_confirmed: bool = False, actor: str = "",
                 server_id: str | None = None) -> dict:
        environment = (environment or "").strip().upper()
        if environment not in ENVIRONMENTS:
            raise ValueError(f"unknown environment: {environment}")
        if environment == "PRODUCTION" and not production_confirmed:
            # section 61: registering a PRODUCTION node requires an explicit,
            # separate confirmation -- never inferred from the name/URL.
            raise ProductionConfirmationRequired(
                "Registering this node as PRODUCTION requires production_confirmed=true; "
                "Production safety policies will apply.")
        sid = server_id or new_server_id()
        record = {
            "server_id": sid, "name": name, "environment": environment,
            "roles": sorted(set(roles or [])), "hostname": hostname,
            "agent_url": (agent_url or "").rstrip("/"), "agent_user": agent_user,
            # Credential stored for repeated calls -- redacted by callers before
            # it ever reaches an API response (section 9/56: never expose to
            # frontend JS). Never written into audit payloads either (section 56).
            "agent_password": agent_password,
            "agent_id": "", "agent_version": "", "capabilities": [],
            "enabled": True, "tags": sorted(set(tags or [])),
            # PENDING until a real handshake succeeds (section 59: never save
            # a server as healthy before successful handshake).
            "registration_status": "PENDING",
            "health_status": "UNKNOWN", "health_detail": {},
            "last_seen": None, "last_health_sync": None, "last_error": "",
            "created_at": now_iso(), "updated_at": now_iso(),
        }
        store = self._load()
        store["items"][sid] = record
        self._save(store)
        self.audit("SERVER_REGISTERED", {
            "server_id": sid, "name": name, "environment": environment,
            "roles": record["roles"], "actor": actor,
        })
        return self._redacted(record)

    def record_handshake(self, server_id: str, identity: dict, actor: str = "system") -> dict:
        """Applies a successful GET /api/status-style identity response.
        Enforces section 8: an agent_id already claimed by a DIFFERENT
        active server raises DuplicateAgentIdentity rather than silently
        reassigning ownership."""
        store = self._load()
        record = store["items"].get(server_id)
        if not record:
            raise KeyError(server_id)
        agent_id = str(identity.get("agent_id") or identity.get("agent_version") or "")
        for other_id, other in store["items"].items():
            if other_id == server_id:
                continue
            if agent_id and other.get("agent_id") == agent_id and other.get("registration_status") == "ACTIVE":
                raise DuplicateAgentIdentity(
                    f"agent_id {agent_id!r} is already claimed by server {other_id!r} "
                    f"({other.get('name')}) -- explicit re-registration required")
        record.update(
            agent_id=agent_id, agent_version=str(identity.get("agent_version") or ""),
            capabilities=sorted(identity.get("capabilities") or []),
            hostname=identity.get("hostname") or record.get("hostname") or "",
            registration_status="ACTIVE", last_seen=now_iso(), last_error="",
            updated_at=now_iso(),
        )
        store["items"][server_id] = record
        self._save(store)
        self.audit("SERVER_HANDSHAKE_OK", {"server_id": server_id, "agent_id": agent_id,
                                            "agent_version": record["agent_version"], "actor": actor})
        return self._redacted(record)

    def record_handshake_failure(self, server_id: str, error: str, actor: str = "system") -> dict:
        store = self._load()
        record = store["items"].get(server_id)
        if not record:
            raise KeyError(server_id)
        was_active = record.get("registration_status") == "ACTIVE"
        record.update(last_error=error[:500], updated_at=now_iso())
        if was_active:
            # Registration state and health state stay separate (section 11):
            # a previously-ACTIVE server that fails a handshake becomes
            # UNREACHABLE, never silently reverts to PENDING.
            record["registration_status"] = "UNREACHABLE"
        record["health_status"] = "OFFLINE"
        store["items"][server_id] = record
        self._save(store)
        self.audit("SERVER_HANDSHAKE_FAILED", {"server_id": server_id, "error": error[:500], "actor": actor})
        return self._redacted(record)

    def update_health_cache(self, server_id: str, health_status: str, detail: dict,
                             last_seen: str | None = None) -> dict:
        if health_status not in HEALTH_STATUSES:
            raise ValueError(f"unknown health_status: {health_status}")
        store = self._load()
        record = store["items"].get(server_id)
        if not record:
            raise KeyError(server_id)
        record.update(health_status=health_status, health_detail=detail,
                       last_health_sync=now_iso(), updated_at=now_iso())
        if last_seen:
            record["last_seen"] = last_seen
        store["items"][server_id] = record
        self._save(store)
        return self._redacted(record)

    def disable(self, server_id: str, actor: str = "") -> dict:
        return self._set_status(server_id, registration_status="DISABLED", enabled=False,
                                 audit_action="SERVER_DISABLED", actor=actor)

    def enable(self, server_id: str, actor: str = "") -> dict:
        # Re-enabling does not itself prove reachability -- registration
        # goes back to PENDING until the next successful handshake
        # (mirrors section 59's "never healthy before handshake" for the
        # initial registration case).
        return self._set_status(server_id, registration_status="PENDING", enabled=True,
                                 audit_action="SERVER_ENABLED", actor=actor)

    def revoke(self, server_id: str, actor: str = "") -> dict:
        return self._set_status(server_id, registration_status="REVOKED", enabled=False,
                                 audit_action="SERVER_REVOKED", actor=actor)

    def _set_status(self, server_id, *, registration_status, enabled, audit_action, actor):
        store = self._load()
        record = store["items"].get(server_id)
        if not record:
            raise KeyError(server_id)
        record.update(registration_status=registration_status, enabled=enabled, updated_at=now_iso())
        store["items"][server_id] = record
        self._save(store)
        self.audit(audit_action, {"server_id": server_id, "actor": actor})
        return self._redacted(record)

    @staticmethod
    def _redacted(record: dict) -> dict:
        # Never return the stored credential to any caller, including the
        # DEV admin's own browser (section 9/56).
        return {k: v for k, v in record.items() if k != "agent_password"}


def classify_health(last_seen_iso: str | None, *, now: datetime | None = None,
                     fresh_seconds: int = 90, stale_seconds: int = 300) -> str:
    """Pure heartbeat-freshness classification (section 15). Thresholds are
    parameters, not constants, so operators can tune them per deployment."""
    if not last_seen_iso:
        return "UNKNOWN"
    try:
        seen = datetime.fromisoformat(str(last_seen_iso).replace("Z", "+00:00"))
    except ValueError:
        return "UNKNOWN"
    now = now or datetime.now(timezone.utc)
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    age = (now - seen).total_seconds()
    if age <= fresh_seconds:
        return "ONLINE"
    if age <= stale_seconds:
        return "DEGRADED"
    return "OFFLINE"


class FleetHealthService:
    """Background poller: one bounded-timeout call per enabled, ACTIVE
    server via an injected `poll_fn` (kept decoupled from urllib/Flask for
    testability). One server raising/timing out must never prevent the
    others from being polled or their cached state from being returned
    (section 46)."""

    def __init__(self, registry: ServerRegistryService,
                 poll_fn: Callable[[dict], RemoteCallResult]):
        self.registry = registry
        self.poll_fn = poll_fn
        self._lock = threading.Lock()
        self._last_results: dict[str, RemoteCallResult] = {}

    def poll_once(self) -> dict[str, RemoteCallResult]:
        results: dict[str, RemoteCallResult] = {}
        for server in self.registry.list(enabled=True):
            if server.get("registration_status") not in ("ACTIVE", "UNREACHABLE"):
                continue
            sid = server["server_id"]
            try:
                result = self.poll_fn(server)
            except Exception as exc:  # a poll_fn bug must not break the whole fleet either
                result = RemoteCallResult(ok=False, server_id=sid, operation="health",
                                           error=f"{type(exc).__name__}: {exc}", error_code="REMOTE_OPERATION_FAILED")
            results[sid] = result
            try:
                self._apply(server, result)
            except Exception:
                pass  # registry write failure for one server must not stop the loop
        with self._lock:
            self._last_results.update(results)
        return results

    def _apply(self, server: dict, result: RemoteCallResult) -> None:
        sid = server["server_id"]
        if result.ok:
            health = classify_health(now_iso())  # a call that just succeeded is fresh by construction
            self.registry.update_health_cache(sid, health, result.data or {}, last_seen=now_iso())
            if server.get("registration_status") == "UNREACHABLE":
                self.registry.record_handshake(sid, result.data or {})
        else:
            self.registry.record_handshake_failure(sid, result.error or result.error_code)

    def cached(self) -> dict[str, RemoteCallResult]:
        with self._lock:
            return dict(self._last_results)

    def fleet_summary(self) -> dict:
        servers = self.registry.list()
        counts = {"total": len(servers), "online": 0, "degraded": 0, "offline": 0, "unknown": 0,
                  "production": 0, "enabled": 0, "disabled": 0}
        for s in servers:
            status = s.get("health_status", "UNKNOWN").lower()
            if status in ("online", "degraded", "offline", "unknown"):
                counts[status] += 1
            if s.get("environment") == "PRODUCTION":
                counts["production"] += 1
            counts["enabled" if s.get("enabled") else "disabled"] += 1
        return counts
