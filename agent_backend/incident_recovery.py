"""Server Incident Recovery Console -- managed-service allowlist,
dependency-aware MESFlow stack recovery, port-conflict-aware failure
diagnosis, and read-only diagnose flows.

This sits ON TOP OF the existing generic Docker/systemd operations
(agent.py's /api/ops/docker-action, /api/ops/service-action) without
changing them, and does NOT touch deployment logic
(deployment_platform.py, release upload/promote). Its entire job is: for
the fixed MESFlow stack only, know the safe recovery order, refuse
anything not on the allowlist, never restart something already healthy,
and never blindly kill a port owner.

No Flask/subprocess-execution dependency of its own -- pure/testable,
matching agent_backend/system_health.py, agent_backend/db_backup.py and
agent_backend/fleet.py. agent.py supplies the real `docker inspect`/
`docker start`/... execution via an injected run_fn (defaults to the
same shape as agent_backend.system_health.run_capture) so every code
path here is unit-testable with a fake.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

CommandResult = dict[str, Any]
RunFn = Callable[..., CommandResult]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------
# Managed service allowlist (task section 1) -- fixed recovery order
# (task section 3): Docker -> PostgreSQL -> MESFlow -> Gateway/nginx ->
# QA Center. Deploy Agent's own container is deliberately NOT here: it
# is managed by host/Docker restart policy, not by this console.
# ---------------------------------------------------------------------

DEFAULT_MANAGED_SERVICES: list[dict[str, Any]] = [
    {"id": "postgres", "label": "PostgreSQL", "container": "mesflow-postgres", "depends_on": []},
    {"id": "mesflow", "label": "MESFlow", "container": "mesflow-app", "depends_on": ["postgres"]},
    {"id": "nginx", "label": "Gateway / Nginx", "container": "mesflow-nginx", "depends_on": ["mesflow"]},
    {"id": "qa", "label": "QA Center", "container": "mesflow-qa-center", "depends_on": ["mesflow"]},
]


def service_by_id(services: list[dict], sid: str) -> dict | None:
    return next((s for s in services if s["id"] == sid), None)


class RecoveryError(Exception):
    pass


class NotManagedError(RecoveryError):
    """A service id/container outside DEFAULT_MANAGED_SERVICES -- the
    server-side guarantee behind "never control arbitrary containers"
    (task section 1/13)."""
    def __init__(self, sid: str):
        super().__init__(f"service not managed: {sid}")
        self.service_id = sid


class RecoveryAlreadyRunning(RecoveryError):
    pass


# ---------------------------------------------------------------------
# Read-only container inspection
# ---------------------------------------------------------------------

def inspect_container(container: str, run_fn: RunFn) -> dict[str, Any]:
    """`docker inspect <container>`, normalized. Never mutates anything.
    A container that was never created (never deployed) is `exists=False`
    -- distinct from `stopped`, which matters for the failure message."""
    result = run_fn(["docker", "inspect", container], timeout=12)
    if not result.get("ok"):
        return {"exists": False, "running": False, "health": "unknown", "status_text": "",
                "exit_code": None, "oom_killed": False, "started_at": "", "finished_at": "",
                "restart_count": 0, "port_bindings": {}, "image": "", "container_id": "",
                "pid": 0, "error_message": str(result.get("stderr") or "")[:500]}
    try:
        rows = json.loads(result.get("stdout") or "[]")
    except json.JSONDecodeError:
        rows = []
    if not rows:
        return {"exists": False, "running": False, "health": "unknown", "status_text": "",
                "exit_code": None, "oom_killed": False, "started_at": "", "finished_at": "",
                "restart_count": 0, "port_bindings": {}, "image": "", "container_id": "",
                "pid": 0, "error_message": ""}
    row = rows[0]
    state = row.get("State") or {}
    health = ((state.get("Health") or {}).get("Status")) or "none"
    running = bool(state.get("Running"))
    return {
        "exists": True, "running": running, "health": health,
        "status_text": state.get("Status") or ("running" if running else "exited"),
        "exit_code": state.get("ExitCode"), "oom_killed": bool(state.get("OOMKilled")),
        "started_at": state.get("StartedAt") or "", "finished_at": state.get("FinishedAt") or "",
        "restart_count": row.get("RestartCount") or 0,
        "port_bindings": (row.get("HostConfig") or {}).get("PortBindings") or {},
        "image": (row.get("Config") or {}).get("Image") or "", "container_id": row.get("Id") or "",
        "pid": state.get("Pid") or 0, "error_message": state.get("Error") or "",
    }


def is_healthy(inspection: dict) -> bool:
    if not inspection.get("exists") or not inspection.get("running"):
        return False
    # "none" = the image has no HEALTHCHECK defined; running is the best
    # signal available in that case, matching agent.py's _docker_mes_state().
    return inspection.get("health") in ("healthy", "none")


def _state_label(inspection: dict) -> str:
    if not inspection.get("exists"):
        return "missing"
    if not inspection.get("running"):
        return "stopped"
    if inspection.get("health") not in ("healthy", "none"):
        return "unhealthy"
    return "running"


# ---------------------------------------------------------------------
# Port-conflict detection (task section 7) -- read-only, never kills
# the owner. Only meaningful for a container that is about to be
# started (a running container legitimately holds its own ports).
# ---------------------------------------------------------------------

def parse_expected_ports(port_bindings: dict) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for key, bindings in (port_bindings or {}).items():
        if not bindings:
            continue
        proto = "udp" if str(key).endswith("/udp") else "tcp"
        for binding in bindings:
            host_port = binding.get("HostPort")
            if host_port and str(host_port).isdigit():
                out.append((int(host_port), proto))
    return out


def check_port_conflicts(inspection: dict, listeners: list[dict],
                          container_pid_owner: dict[int, str] | None = None) -> list[dict]:
    if inspection.get("running"):
        return []
    expected = parse_expected_ports(inspection.get("port_bindings") or {})
    if not expected:
        return []
    container_pid_owner = container_pid_owner or {}
    out = []
    for port, proto in expected:
        owner = next((row for row in listeners if row.get("port") == port), None)
        out.append({
            "port": port, "protocol": proto, "conflict": bool(owner),
            "owner_pid": (owner or {}).get("pid", 0), "owner_process": (owner or {}).get("process", ""),
            "owner_container": container_pid_owner.get((owner or {}).get("pid", -1), ""),
        })
    return out


# ---------------------------------------------------------------------
# Safe start guard (task section 9)
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reason: str = ""


def can_start(service: dict, inspection: dict, dependency_states: dict[str, str],
              server_role: str, production_approved: bool,
              allow_production_test_start: bool = True) -> GuardResult:
    if inspection.get("running") and is_healthy(inspection):
        return GuardResult(False, "ALREADY_RUNNING")
    for dep_id in service.get("depends_on", []):
        if dependency_states.get(dep_id) != "healthy":
            return GuardResult(False, f"DEPENDENCY_NOT_READY:{dep_id}")
    role = (server_role or "DEV").strip().upper()
    if role == "PRODUCTION" and not production_approved:
        return GuardResult(False, "PRODUCTION_APPROVAL_REQUIRED")
    if role == "PRODUCTION_TEST" and not allow_production_test_start:
        return GuardResult(False, "PRODUCTION_TEST_START_DISABLED_BY_POLICY")
    return GuardResult(True, "")


# ---------------------------------------------------------------------
# Failure diagnosis for a start attempt (task section 4)
# ---------------------------------------------------------------------

def diagnose_start_failure(service: dict, inspection: dict, logs_tail: str,
                            port_conflicts: list[dict] | None = None) -> dict:
    label = service.get("label", service.get("id", ""))
    conflicting = [row for row in (port_conflicts or []) if row.get("conflict")]
    owner_line = ""
    if conflicting:
        row = conflicting[0]
        cause = f"Port {row['port']} đang được sử dụng."
        owner_line = str(row.get("owner_container") or row.get("owner_process") or f"PID {row.get('owner_pid')}")
    elif inspection.get("oom_killed"):
        cause = "Container bị hệ điều hành kill do hết bộ nhớ (OOMKilled)."
    elif inspection.get("exit_code") not in (None, 0):
        cause = f"Container thoát với exit code {inspection.get('exit_code')}."
    elif "port is already allocated" in (logs_tail or "").lower():
        cause = "Port đã được cấp phát cho container/tiến trình khác."
    else:
        cause = "Không xác định được nguyên nhân cụ thể từ log/health hiện tại."
    lines = [f"{label} không khởi động được.", "", "Nguyên nhân:", cause]
    if owner_line:
        lines += ["", "Owner:", owner_line]
    return {
        "message": "\n".join(lines), "probable_cause": cause, "owner": owner_line,
        "exit_code": inspection.get("exit_code"), "state": inspection.get("status_text"),
        "health": inspection.get("health"), "oom_killed": bool(inspection.get("oom_killed")),
        "port_conflicts": conflicting, "logs_tail": (logs_tail or "")[-4000:],
    }


# ---------------------------------------------------------------------
# Read-only diagnosis flows (task section 5): SUMMARY / EVIDENCE /
# PROBABLE CAUSE / RECOMMENDED ACTION. Pure functions over injected
# evidence -- no subprocess call happens inside this section.
# ---------------------------------------------------------------------

def _diag_mesflow_unavailable(mes_status=None, inspection=None, **_):
    mes_status = mes_status or {}
    inspection = inspection or {}
    evidence = [f"api_online={mes_status.get('api_online')}", f"docker_exists={inspection.get('exists')}",
                f"docker_running={inspection.get('running')}", f"health={inspection.get('health')}"]
    if not inspection.get("exists"):
        cause = "Container mesflow-app không tồn tại trên host này."
        action = "Kiểm tra lần deploy gần nhất; xác nhận release đã được cài trước khi Recover."
    elif not inspection.get("running"):
        cause = "Container mesflow-app đang dừng."
        action = "Khởi động MESFlow từ trang Dịch vụ (đảm bảo PostgreSQL khoẻ trước)."
    elif inspection.get("health") not in ("healthy", "none"):
        cause = "Container đang chạy nhưng healthcheck báo unhealthy."
        action = "Xem log mesflow-app để tìm lỗi khởi động (kết nối DB, migration, cấu hình)."
    else:
        cause = "Container chạy và healthy nhưng API không phản hồi từ bên ngoài."
        action = "Kiểm tra gateway/nginx và mạng nội bộ giữa nginx và mesflow-app."
    return {"summary": "MESFlow không truy cập được.", "evidence": evidence,
            "probable_cause": cause, "recommended_action": action}


def _diag_postgres_unavailable(inspection=None, **_):
    inspection = inspection or {}
    evidence = [f"docker_exists={inspection.get('exists')}", f"docker_running={inspection.get('running')}",
                f"health={inspection.get('health')}", f"exit_code={inspection.get('exit_code')}"]
    if not inspection.get("exists"):
        cause = "Container mesflow-postgres không tồn tại trên host này."
        action = "Kiểm tra cấu hình deploy; không tự tạo lại database container qua Recovery Console."
    elif not inspection.get("running"):
        cause = "Container mesflow-postgres đang dừng."
        action = "Khởi động PostgreSQL từ trang Dịch vụ trước khi khởi động MESFlow."
    elif inspection.get("health") not in ("healthy", "none"):
        cause = "PostgreSQL đang chạy nhưng healthcheck báo unhealthy."
        action = "Xem log mesflow-postgres; kiểm tra dung lượng đĩa và tiến trình khởi động DB."
    else:
        cause = "PostgreSQL container khoẻ mạnh -- nếu MESFlow vẫn không kết nối được, kiểm tra cấu hình kết nối/network."
        action = "Kiểm tra biến môi trường kết nối DB của MESFlow và Docker network."
    return {"summary": "PostgreSQL không truy cập được.", "evidence": evidence,
            "probable_cause": cause, "recommended_action": action}


def _diag_gateway_502(inspection=None, mesflow_running=None, **_):
    inspection = inspection or {}
    evidence = [f"nginx_running={inspection.get('running')}", f"nginx_health={inspection.get('health')}",
                f"mesflow_running={mesflow_running}"]
    if not mesflow_running:
        cause = "Nginx không có upstream khả dụng vì mesflow-app đang dừng."
        action = "Khởi động MESFlow trước; lỗi 502 sẽ tự hết khi upstream sẵn sàng."
    elif not inspection.get("running"):
        cause = "Container mesflow-nginx đang dừng."
        action = "Khởi động Gateway/nginx từ trang Dịch vụ."
    elif inspection.get("health") not in ("healthy", "none"):
        cause = "Gateway/nginx đang chạy nhưng unhealthy."
        action = "Xem log mesflow-nginx để tìm lỗi cấu hình proxy."
    else:
        cause = "Nginx và MESFlow đều đang chạy nhưng vẫn 502 -- có thể MESFlow chưa sẵn sàng nhận traffic hoặc cấu hình proxy sai."
        action = "Kiểm tra log nginx và log mesflow-app cùng thời điểm xảy ra 502."
    return {"summary": "Gateway/nginx trả về 502.", "evidence": evidence,
            "probable_cause": cause, "recommended_action": action}


def _diag_container_stopped(service=None, inspection=None, **_):
    service = service or {}
    inspection = inspection or {}
    label = service.get("label", service.get("id", "container"))
    evidence = [f"exists={inspection.get('exists')}", f"exit_code={inspection.get('exit_code')}",
                f"oom_killed={inspection.get('oom_killed')}"]
    if inspection.get("oom_killed"):
        cause = f"{label} bị hệ điều hành kill do hết bộ nhớ (OOMKilled)."
        action = "Kiểm tra RAM host và giới hạn bộ nhớ container trước khi khởi động lại."
    else:
        cause = f"{label} đang ở trạng thái dừng (exit code {inspection.get('exit_code')})."
        action = f"Xem log {service.get('container', '')} để xác định nguyên nhân dừng trước khi khởi động lại."
    return {"summary": f"{label} đang dừng.", "evidence": evidence,
            "probable_cause": cause, "recommended_action": action}


def _diag_container_unhealthy(service=None, inspection=None, **_):
    service = service or {}
    inspection = inspection or {}
    label = service.get("label", service.get("id", "container"))
    evidence = [f"running={inspection.get('running')}", f"health={inspection.get('health')}",
                f"restart_count={inspection.get('restart_count')}"]
    cause = f"{label} đang chạy nhưng Docker healthcheck báo '{inspection.get('health')}'."
    action = f"Xem log {service.get('container', '')} và readiness endpoint (nếu có) để tìm lý do healthcheck thất bại."
    return {"summary": f"{label} unhealthy.", "evidence": evidence,
            "probable_cause": cause, "recommended_action": action}


def _diag_restart_loop(service=None, inspection=None, **_):
    service = service or {}
    inspection = inspection or {}
    label = service.get("label", service.get("id", "container"))
    restarts = inspection.get("restart_count", 0) or 0
    evidence = [f"restart_count={restarts}", f"status={inspection.get('status_text')}",
                f"exit_code={inspection.get('exit_code')}"]
    if restarts >= 3:
        cause = f"{label} đã restart {restarts} lần -- dấu hiệu restart loop, thường do lỗi khởi động lặp lại (crash ngay sau start)."
        action = f"Xem log {service.get('container', '')} ngay sau lần restart gần nhất; không tiếp tục restart thủ công nhiều lần."
    else:
        cause = f"{label} chưa cho thấy dấu hiệu restart loop rõ ràng (restart_count={restarts})."
        action = "Theo dõi thêm; nếu restart_count tiếp tục tăng, xem log để tìm nguyên nhân crash."
    return {"summary": f"Kiểm tra restart loop cho {label}.", "evidence": evidence,
            "probable_cause": cause, "recommended_action": action}


def _diag_disk_high(percent=0, free="", **_):
    percent = float(percent or 0)
    evidence = [f"disk_percent={percent}", f"free={free}"]
    if percent >= 95:
        cause = "Đĩa gần đầy (>=95%) -- nguy cơ ghi dữ liệu/DB thất bại."
        action = "Dọn dẹp log/backup cũ theo chính sách retention hiện có; không xoá dữ liệu thủ công ngoài quy trình."
    elif percent >= 85:
        cause = "Đĩa sử dụng cao (>=85%)."
        action = "Theo dõi dự báo dung lượng đĩa; lên kế hoạch dọn dẹp trước khi chạm ngưỡng nguy hiểm."
    else:
        cause = "Dung lượng đĩa trong ngưỡng bình thường."
        action = "Không cần hành động."
    return {"summary": "Kiểm tra dung lượng đĩa.", "evidence": evidence,
            "probable_cause": cause, "recommended_action": action}


def _diag_ram_high(percent=0, **_):
    percent = float(percent or 0)
    evidence = [f"ram_percent={percent}"]
    if percent >= 90:
        cause = "RAM sử dụng rất cao (>=90%) -- nguy cơ OOM-kill container."
        action = "Kiểm tra tiến trình/container tiêu tốn RAM nhiều nhất; cân nhắc restart container có dấu hiệu memory leak."
    elif percent >= 80:
        cause = "RAM sử dụng cao (>=80%)."
        action = "Theo dõi thêm; xem alert/predictive insights để phát hiện xu hướng."
    else:
        cause = "RAM trong ngưỡng bình thường."
        action = "Không cần hành động."
    return {"summary": "Kiểm tra RAM.", "evidence": evidence,
            "probable_cause": cause, "recommended_action": action}


def _diag_docker_unavailable(docker_ok=None, **_):
    evidence = [f"docker_ok={docker_ok}"]
    if docker_ok:
        cause = "Docker daemon đang phản hồi bình thường."
        action = "Không cần hành động."
    else:
        cause = "Docker daemon không phản hồi hoặc chưa chạy -- mọi hành động khởi động container sẽ thất bại."
        action = "Kiểm tra 'systemctl status docker' trên host; khởi động lại Docker daemon yêu cầu quyền hệ thống, không tự động qua Recovery Console."
    return {"summary": "Kiểm tra Docker daemon.", "evidence": evidence,
            "probable_cause": cause, "recommended_action": action}


_DIAGNOSERS: dict[str, Callable[..., dict]] = {
    "mesflow_unavailable": _diag_mesflow_unavailable,
    "postgres_unavailable": _diag_postgres_unavailable,
    "gateway_502": _diag_gateway_502,
    "container_stopped": _diag_container_stopped,
    "container_unhealthy": _diag_container_unhealthy,
    "restart_loop": _diag_restart_loop,
    "disk_high": _diag_disk_high,
    "ram_high": _diag_ram_high,
    "docker_unavailable": _diag_docker_unavailable,
}
DIAGNOSIS_KINDS: tuple[str, ...] = tuple(_DIAGNOSERS.keys())


def diagnose(kind: str, **evidence) -> dict:
    fn = _DIAGNOSERS.get(kind)
    if not fn:
        raise ValueError(f"unknown diagnosis kind: {kind}")
    return fn(**evidence)


# ---------------------------------------------------------------------
# Incident -> recovery mapping (task section 6). Matches agent.py's
# _incident_conditions() fingerprints exactly.
# ---------------------------------------------------------------------

_CONTAINER_TO_SERVICE = {"mesflow-postgres": "postgres", "mesflow-app": "mesflow",
                         "mesflow-nginx": "nginx", "mesflow-qa-center": "qa"}


def service_id_for_incident(incident_type: str) -> str | None:
    incident_type = incident_type or ""
    if incident_type == "MESFLOW_DOWN":
        return "mesflow"
    if incident_type == "QA_DOWN":
        return "qa"
    if incident_type.startswith("CONTAINER_DOWN:") or incident_type.startswith("CONTAINER_UNHEALTHY:"):
        return _CONTAINER_TO_SERVICE.get(incident_type.split(":", 1)[1])
    return None


# ---------------------------------------------------------------------
# Service view (the 9-column row the "Dịch vụ" page renders)
# ---------------------------------------------------------------------

def build_service_view(service: dict, inspection: dict, dependency_states: dict[str, str],
                        server_role: str, production_approved: bool, version: str = "",
                        allow_production_test_start: bool = True) -> dict:
    state = _state_label(inspection)
    guard = can_start(service, inspection, dependency_states, server_role, production_approved,
                       allow_production_test_start)
    uptime = 0
    if inspection.get("running") and inspection.get("started_at"):
        try:
            started = datetime.fromisoformat(str(inspection["started_at"]).replace("Z", "+00:00"))
            uptime = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
        except ValueError:
            uptime = 0
    return {
        "id": service["id"], "label": service["label"], "container": service["container"],
        "exists": bool(inspection.get("exists")), "running": bool(inspection.get("running")),
        "health": inspection.get("health", "unknown"), "state": state, "version": version,
        "uptime_seconds": uptime, "dependencies": list(service.get("depends_on", [])),
        "dependency_states": dependency_states, "last_error": inspection.get("error_message") or "",
        "restart_count": inspection.get("restart_count", 0),
        "can_start": guard.allowed, "start_blocked_reason": guard.reason,
        "can_restart": state != "missing", "can_stop": bool(inspection.get("running")),
        "raw": inspection,
    }


# ---------------------------------------------------------------------
# Recovery run bookkeeping
# ---------------------------------------------------------------------

@dataclass
class RecoveryStep:
    service_id: str
    label: str
    action: str
    reason: str = ""
    detail: dict = field(default_factory=dict)


@dataclass
class RunResult:
    started_at: str
    finished_at: str = ""
    status: str = "RUNNING"
    steps: list[RecoveryStep] = field(default_factory=list)


# ---------------------------------------------------------------------
# Orchestrator -- the single object agent.py wires up and calls.
# ---------------------------------------------------------------------

class RecoveryOrchestrator:
    def __init__(self, services: list[dict], run_fn: RunFn, *,
                 mes_status_fn: Callable[[], dict] | None = None,
                 qa_status_fn: Callable[[], dict] | None = None,
                 server_role_provider: Callable[[], str] = lambda: "DEV",
                 hostname_provider: Callable[[], str] = socket.gethostname,
                 audit: Callable[[str, dict], None] | None = None,
                 listeners_fn: Callable[[], list[dict]] | None = None,
                 container_pid_lookup_fn: Callable[[], dict[int, str]] | None = None,
                 health_wait_seconds: float = 30, health_poll_interval: float = 1.5,
                 allow_production_test_start: bool = True):
        self.services = services
        self.run_fn = run_fn
        self.mes_status_fn = mes_status_fn or (lambda: {})
        self.qa_status_fn = qa_status_fn or (lambda: {})
        self.server_role_provider = server_role_provider
        self.hostname_provider = hostname_provider
        self.audit = audit or (lambda action, data: None)
        self.listeners_fn = listeners_fn or (lambda: [])
        self.container_pid_lookup_fn = container_pid_lookup_fn or (lambda: {})
        self.health_wait_seconds = health_wait_seconds
        self.health_poll_interval = health_poll_interval
        self.allow_production_test_start = allow_production_test_start
        self._stack_lock = threading.Lock()

    # -- lookups -------------------------------------------------------
    def _service(self, sid: str) -> dict:
        service = service_by_id(self.services, sid)
        if not service:
            raise NotManagedError(sid)
        return service

    def inspect(self, service: dict) -> dict:
        return inspect_container(service["container"], self.run_fn)

    def _health_label(self, sid: str) -> str:
        service = service_by_id(self.services, sid)
        if not service:
            return "unknown"
        insp = self.inspect(service)
        return "healthy" if (insp.get("running") and is_healthy(insp)) else "down"

    def _version_for(self, service: dict, insp: dict) -> str:
        sid = service["id"]
        if sid == "mesflow":
            try:
                return str(self.mes_status_fn().get("version") or "")
            except Exception:
                return ""
        if sid == "qa":
            try:
                return str(self.qa_status_fn().get("version") or "")
            except Exception:
                return ""
        image = insp.get("image") or ""
        return image.rsplit(":", 1)[-1] if ":" in image else image

    def _port_conflicts_for(self, inspection: dict) -> list[dict]:
        return check_port_conflicts(inspection, self.listeners_fn() or [], self.container_pid_lookup_fn() or {})

    def _recent_logs(self, container: str, lines: int = 80) -> str:
        result = self.run_fn(["docker", "logs", "--tail", str(lines), container], timeout=15)
        return ((result.get("stdout") or "") + "\n" + (result.get("stderr") or ""))[-4000:]

    def _audit_attempt(self, service: dict, actor: str, action: str, previous_state: str,
                        result: str, reason: str, incident_id: str) -> None:
        self.audit(f"RECOVERY_{action}", {
            "who": actor or "system", "when": now_iso(), "server": self.hostname_provider(),
            "service": service["id"], "container": service["container"], "action": action,
            "previous_state": previous_state, "result": result, "reason": (reason or "")[:500],
            "incident_id": incident_id or "",
        })

    # -- read-only views -------------------------------------------------
    def service_view(self, sid: str, production_approved: bool = False) -> dict:
        service = self._service(sid)
        insp = self.inspect(service)
        dep_states = {dep: self._health_label(dep) for dep in service.get("depends_on", [])}
        return build_service_view(service, insp, dep_states, self.server_role_provider(), production_approved,
                                   version=self._version_for(service, insp),
                                   allow_production_test_start=self.allow_production_test_start)

    def list_service_views(self, production_approved: bool = False) -> list[dict]:
        views = []
        health_cache: dict[str, str] = {}
        for service in self.services:
            insp = self.inspect(service)
            health_cache[service["id"]] = "healthy" if (insp.get("running") and is_healthy(insp)) else "down"
            dep_states = {dep: health_cache.get(dep, self._health_label(dep)) for dep in service.get("depends_on", [])}
            views.append(build_service_view(service, insp, dep_states, self.server_role_provider(),
                                             production_approved, version=self._version_for(service, insp),
                                             allow_production_test_start=self.allow_production_test_start))
        return views

    def ports(self, sid: str) -> list[dict]:
        service = self._service(sid)
        return self._port_conflicts_for(self.inspect(service))

    def logs(self, sid: str, lines: int = 200) -> dict:
        service = self._service(sid)
        return self.run_fn(["docker", "logs", "--tail", str(max(20, min(2000, lines))), service["container"]], timeout=15)

    def preflight(self) -> dict:
        docker_ok = bool(self.run_fn(["docker", "info"], timeout=10).get("ok"))
        return {"ok": docker_ok, "docker_available": docker_ok}

    # -- mutations ---------------------------------------------------
    def _wait_for_health(self, service: dict) -> bool:
        if self.health_wait_seconds <= 0:
            insp = self.inspect(service)
            return bool(insp.get("running")) and is_healthy(insp)
        deadline = time.monotonic() + self.health_wait_seconds
        while True:
            insp = self.inspect(service)
            if insp.get("running") and is_healthy(insp):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.health_poll_interval)

    def _attempt_start_one(self, service: dict, dependency_states: dict[str, str], actor: str,
                            production_approved: bool, incident_id: str) -> tuple[RecoveryStep, str]:
        sid = service["id"]
        inspection = self.inspect(service)
        previous_state = _state_label(inspection)
        if inspection.get("running") and is_healthy(inspection):
            return RecoveryStep(sid, service["label"], "SKIPPED_ALREADY_HEALTHY",
                                 detail={"state": previous_state}), "healthy"
        guard = can_start(service, inspection, dependency_states, self.server_role_provider(),
                           production_approved, self.allow_production_test_start)
        if not guard.allowed:
            self._audit_attempt(service, actor, "START", previous_state, "BLOCKED", guard.reason, incident_id)
            return RecoveryStep(sid, service["label"], "BLOCKED", reason=guard.reason,
                                 detail={"state": previous_state}), previous_state
        conflicts = self._port_conflicts_for(inspection)
        blocking = [row for row in conflicts if row.get("conflict")]
        if blocking:
            self._audit_attempt(service, actor, "START", previous_state, "FAILED_PORT_CONFLICT",
                                 json.dumps(blocking, ensure_ascii=False), incident_id)
            return RecoveryStep(sid, service["label"], "FAILED_PORT_CONFLICT", reason="PORT_CONFLICT",
                                 detail={"conflicts": blocking}), "down"
        result = self.run_fn(["docker", "start", service["container"]], timeout=60)
        if not result.get("ok"):
            reason = str(result.get("stderr") or "")[:500]
            self._audit_attempt(service, actor, "START", previous_state, "FAILED_TO_START", reason, incident_id)
            return RecoveryStep(sid, service["label"], "FAILED_TO_START", reason=reason), "down"
        healthy = self._wait_for_health(service)
        post_inspection = self.inspect(service)
        if healthy:
            self._audit_attempt(service, actor, "START", previous_state, "STARTED", "", incident_id)
            return RecoveryStep(sid, service["label"], "STARTED",
                                 detail={"state": _state_label(post_inspection)}), "healthy"
        diag = diagnose_start_failure(service, post_inspection, self._recent_logs(service["container"]), conflicts)
        self._audit_attempt(service, actor, "START", previous_state, "FAILED_UNHEALTHY_AFTER_START",
                             diag["probable_cause"], incident_id)
        return RecoveryStep(sid, service["label"], "FAILED_UNHEALTHY_AFTER_START",
                             reason=diag["probable_cause"], detail=diag), "down"

    def start_service(self, sid: str, actor: str = "", production_approved: bool = False,
                       incident_id: str = "") -> RecoveryStep:
        service = self._service(sid)
        dep_states = {dep: self._health_label(dep) for dep in service.get("depends_on", [])}
        step, _ = self._attempt_start_one(service, dep_states, actor, production_approved, incident_id)
        return step

    def _role_blocks_mutation(self, production_approved: bool) -> bool:
        return (self.server_role_provider() or "DEV").strip().upper() == "PRODUCTION" and not production_approved

    def restart_service(self, sid: str, actor: str = "", production_approved: bool = False,
                         incident_id: str = "") -> RecoveryStep:
        service = self._service(sid)
        previous_state = _state_label(self.inspect(service))
        if self._role_blocks_mutation(production_approved):
            self._audit_attempt(service, actor, "RESTART", previous_state, "BLOCKED",
                                 "PRODUCTION_APPROVAL_REQUIRED", incident_id)
            return RecoveryStep(sid, service["label"], "BLOCKED", reason="PRODUCTION_APPROVAL_REQUIRED")
        result = self.run_fn(["docker", "restart", service["container"]], timeout=90)
        if not result.get("ok"):
            reason = str(result.get("stderr") or "")[:500]
            self._audit_attempt(service, actor, "RESTART", previous_state, "FAILED_TO_RESTART", reason, incident_id)
            return RecoveryStep(sid, service["label"], "FAILED_TO_RESTART", reason=reason)
        healthy = self._wait_for_health(service)
        action = "RESTARTED" if healthy else "FAILED_UNHEALTHY_AFTER_START"
        self._audit_attempt(service, actor, "RESTART", previous_state, action, "", incident_id)
        return RecoveryStep(sid, service["label"], action)

    def stop_service(self, sid: str, actor: str = "", production_approved: bool = False,
                      incident_id: str = "") -> RecoveryStep:
        service = self._service(sid)
        previous_state = _state_label(self.inspect(service))
        if self._role_blocks_mutation(production_approved):
            self._audit_attempt(service, actor, "STOP", previous_state, "BLOCKED",
                                 "PRODUCTION_APPROVAL_REQUIRED", incident_id)
            return RecoveryStep(sid, service["label"], "BLOCKED", reason="PRODUCTION_APPROVAL_REQUIRED")
        result = self.run_fn(["docker", "stop", service["container"]], timeout=60)
        action = "STOPPED" if result.get("ok") else "FAILED_TO_STOP"
        reason = "" if result.get("ok") else str(result.get("stderr") or "")[:500]
        self._audit_attempt(service, actor, "STOP", previous_state, action, reason, incident_id)
        return RecoveryStep(sid, service["label"], action, reason=reason)

    def recover_stack(self, actor: str = "", production_approved: bool = False,
                       incident_id: str = "") -> RunResult:
        if not self._stack_lock.acquire(blocking=False):
            raise RecoveryAlreadyRunning()
        try:
            run = RunResult(started_at=now_iso())
            preflight = self.preflight()
            if not preflight.get("docker_available"):
                for service in self.services:
                    run.steps.append(RecoveryStep(service["id"], service["label"], "BLOCKED",
                                                   reason="DOCKER_UNAVAILABLE"))
                run.status = "BLOCKED"
                run.finished_at = now_iso()
                return run
            health_cache: dict[str, str] = {}
            for service in self.services:
                dep_states = {dep: health_cache.get(dep, self._health_label(dep))
                              for dep in service.get("depends_on", [])}
                step, new_state = self._attempt_start_one(service, dep_states, actor, production_approved, incident_id)
                health_cache[service["id"]] = new_state
                run.steps.append(step)
            run.status = "COMPLETED"
            run.finished_at = now_iso()
            return run
        finally:
            self._stack_lock.release()

    # -- incident linkage (task section 6) ----------------------------
    def recommend_for_incident(self, incident_type: str, production_approved: bool = False) -> dict | None:
        sid = service_id_for_incident(incident_type)
        if not sid:
            return None
        view = self.service_view(sid, production_approved=production_approved)
        actions = []
        if view["can_start"]:
            actions.append({"type": "start", "label": f"Khởi động {view['label']}"})
        actions.append({"type": "logs", "label": "Xem log"})
        actions.append({"type": "diagnose", "label": "Chẩn đoán"})
        text = f"{view['label']}: {view['state']}."
        if not view["can_start"] and view["state"] != "running":
            text += f" (Chưa thể khởi động: {view['start_blocked_reason']})"
        return {"service_id": sid, "service": view, "actions": actions, "recommendation_text": text}

    # -- diagnosis (task section 5) ------------------------------------
    def _safe(self, fn: Callable[[], dict]) -> dict:
        try:
            return fn() or {}
        except Exception:
            return {}

    def run_diagnosis(self, kind: str, service_id: str = "", host_summary: dict | None = None) -> dict:
        host_summary = host_summary or {}
        if kind == "docker_unavailable":
            return diagnose(kind, docker_ok=self.preflight().get("docker_available"))
        if kind == "disk_high":
            disk = host_summary.get("disk") or {}
            return diagnose(kind, percent=disk.get("percent", 0), free=disk.get("free", ""))
        if kind == "ram_high":
            ram = host_summary.get("ram") or {}
            return diagnose(kind, percent=ram.get("percent", 0))
        sid = service_id or {"mesflow_unavailable": "mesflow", "postgres_unavailable": "postgres",
                              "gateway_502": "nginx"}.get(kind, "")
        service = service_by_id(self.services, sid) if sid else None
        if not service:
            raise NotManagedError(sid or "(none)")
        inspection = self.inspect(service)
        if kind == "mesflow_unavailable":
            return diagnose(kind, mes_status=self._safe(self.mes_status_fn), inspection=inspection)
        if kind == "postgres_unavailable":
            return diagnose(kind, inspection=inspection)
        if kind == "gateway_502":
            mesflow_service = service_by_id(self.services, "mesflow")
            mesflow_insp = self.inspect(mesflow_service) if mesflow_service else {}
            return diagnose(kind, inspection=inspection, mesflow_running=mesflow_insp.get("running"))
        if kind in ("container_stopped", "container_unhealthy", "restart_loop"):
            return diagnose(kind, service=service, inspection=inspection)
        raise ValueError(f"unhandled diagnosis kind: {kind}")


# ---------------------------------------------------------------------
# System check (task section 8) -- HEALTHY / DEGRADED / CRITICAL
# ---------------------------------------------------------------------

def _band(critical: bool, warning: bool) -> str:
    if critical:
        return "CRITICAL"
    if warning:
        return "DEGRADED"
    return "HEALTHY"


def system_check(host_summary: dict, service_views: list[dict], docker_available: bool = True) -> dict:
    host_summary = host_summary or {}
    cpu = float(host_summary.get("cpu_percent", 0) or 0)
    ram = float((host_summary.get("ram") or {}).get("percent", 0) or 0)
    swap = float((host_summary.get("swap") or {}).get("percent", 0) or 0)
    disk = float((host_summary.get("disk") or {}).get("percent", 0) or 0)
    components = [
        {"name": "CPU", "status": _band(cpu >= 90, cpu >= 75), "detail": f"{cpu:.0f}%"},
        {"name": "RAM", "status": _band(ram >= 90, ram >= 80), "detail": f"{ram:.0f}%"},
        {"name": "Swap", "status": _band(swap >= 80, swap >= 50), "detail": f"{swap:.0f}%"},
        {"name": "Disk (/)", "status": _band(disk >= 95, disk >= 85), "detail": f"{disk:.0f}%"},
        {"name": "Docker daemon", "status": "HEALTHY" if docker_available else "CRITICAL",
         "detail": "available" if docker_available else "unreachable"},
    ]
    for view in service_views:
        critical_service = view["id"] in ("postgres", "mesflow")
        if not view.get("exists") or not view.get("running"):
            status = "CRITICAL" if critical_service else "DEGRADED"
            detail = "không tồn tại" if not view.get("exists") else "đang dừng"
        elif view.get("health") not in ("healthy", "none"):
            status, detail = "DEGRADED", f"health={view.get('health')}"
        else:
            status, detail = "HEALTHY", "OK"
        components.append({"name": view.get("label", view["id"]), "status": status, "detail": detail})
    order = {"CRITICAL": 2, "DEGRADED": 1, "HEALTHY": 0}
    overall = max((c["status"] for c in components), key=lambda s: order[s]) if components else "HEALTHY"
    return {"status": overall, "components": components, "score": host_summary.get("score")}
