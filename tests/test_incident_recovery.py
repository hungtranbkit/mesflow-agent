"""Server Incident Recovery Console -- pure unit tests (no Flask, no real
Docker/subprocess), mirroring tests/test_p5_fleet_unit.py's shape. A fake
`run_fn` stands in for `docker ...` so scenarios A-H from
AGENTS.md-adjacent task section 12 can be asserted against the exact
commands issued -- in particular that a port conflict or an unrelated
container NEVER results in a stop/kill/start being issued against
anything real.
"""
from __future__ import annotations

import json

import pytest

from agent_backend.incident_recovery import (
    DEFAULT_MANAGED_SERVICES,
    GuardResult,
    NotManagedError,
    RecoveryAlreadyRunning,
    RecoveryOrchestrator,
    can_start,
    diagnose,
    inspect_container,
    parse_expected_ports,
    service_by_id,
    service_id_for_incident,
    system_check,
)


class FakeDocker:
    """Minimal `docker ...` double. Records every command issued so tests
    can assert exactly what was (and was NOT) run."""

    def __init__(self, containers: dict[str, dict]):
        self.containers = containers
        self.commands: list[list[str]] = []

    def run_fn(self, cmd, timeout=12, **_):
        cmd = list(cmd)
        self.commands.append(cmd)
        if cmd[:2] == ["docker", "info"]:
            return {"ok": True, "stdout": "", "stderr": ""}
        if cmd[:2] == ["docker", "inspect"]:
            name = cmd[2]
            c = self.containers.get(name)
            if not c or not c.get("exists", True):
                return {"ok": False, "exit_code": 1, "stdout": "", "stderr": f"Error: No such object: {name}"}
            row = {
                "Id": f"cid-{name}",
                "State": {
                    "Running": c.get("running", False),
                    "Status": "running" if c.get("running") else "exited",
                    "Health": {"Status": c["health"]} if c.get("health") else None,
                    "ExitCode": c.get("exit_code", 0), "OOMKilled": c.get("oom_killed", False),
                    "StartedAt": c.get("started_at", ""), "FinishedAt": "",
                    "Pid": c.get("pid", 0), "Error": c.get("error", ""),
                },
                "RestartCount": c.get("restart_count", 0),
                "HostConfig": {"PortBindings": c.get("port_bindings", {})},
                "Config": {"Image": c.get("image", "mesflow:1.0")},
            }
            return {"ok": True, "stdout": json.dumps([row]), "stderr": ""}
        if cmd[:2] == ["docker", "start"]:
            name = cmd[2]
            c = self.containers.setdefault(name, {})
            if c.get("start_fails"):
                return {"ok": False, "exit_code": 1, "stdout": "", "stderr": c.get("start_fail_msg", "failed")}
            c["exists"] = True
            c["running"] = True
            if "post_start_health" in c:
                c["health"] = c["post_start_health"]
            return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}
        if cmd[:2] == ["docker", "stop"]:
            self.containers.setdefault(cmd[2], {})["running"] = False
            return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}
        if cmd[:2] == ["docker", "restart"]:
            self.containers.setdefault(cmd[2], {})["running"] = True
            return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}
        if cmd[:2] == ["docker", "logs"]:
            return {"ok": True, "stdout": "log line\n", "stderr": ""}
        return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "unknown fake command"}


def make_orchestrator(docker: FakeDocker, **kw) -> RecoveryOrchestrator:
    kw.setdefault("listeners_fn", lambda: [])
    kw.setdefault("container_pid_lookup_fn", lambda: {})
    return RecoveryOrchestrator(DEFAULT_MANAGED_SERVICES, docker.run_fn, health_wait_seconds=0, **kw)


def _start_commands(docker):
    return [c for c in docker.commands if c[:2] == ["docker", "start"]]


def _mutating_commands(docker):
    return [c for c in docker.commands if c[:2] in (["docker", "start"], ["docker", "stop"],
                                                      ["docker", "restart"], ["docker", "kill"], ["docker", "rm"])]


# -- Scenario A: MESFlow stopped, Postgres healthy -> recover starts MESFlow only ---

def test_scenario_a_recover_starts_mesflow_only():
    docker = FakeDocker({
        "mesflow-postgres": {"exists": True, "running": True, "health": "healthy"},
        "mesflow-app": {"exists": True, "running": False, "health": "none"},
        "mesflow-nginx": {"exists": True, "running": True, "health": "healthy"},
        "mesflow-qa-center": {"exists": True, "running": True, "health": "healthy"},
    })
    run = make_orchestrator(docker).recover_stack(actor="tester")
    actions = {s.service_id: s.action for s in run.steps}
    assert actions["postgres"] == "SKIPPED_ALREADY_HEALTHY"
    assert actions["mesflow"] == "STARTED"
    assert actions["nginx"] == "SKIPPED_ALREADY_HEALTHY"
    assert actions["qa"] == "SKIPPED_ALREADY_HEALTHY"
    assert _start_commands(docker) == [["docker", "start", "mesflow-app"]]


# -- Scenario B: Postgres stopped -> Postgres first, then MESFlow ---

def test_scenario_b_postgres_started_before_mesflow():
    docker = FakeDocker({
        "mesflow-postgres": {"exists": True, "running": False, "health": "none"},
        "mesflow-app": {"exists": True, "running": False, "health": "none"},
        "mesflow-nginx": {"exists": True, "running": True, "health": "healthy"},
        "mesflow-qa-center": {"exists": True, "running": True, "health": "healthy"},
    })
    run = make_orchestrator(docker).recover_stack(actor="tester")
    actions = {s.service_id: s.action for s in run.steps}
    assert actions["postgres"] == "STARTED"
    assert actions["mesflow"] == "STARTED"
    assert [c[2] for c in _start_commands(docker)] == ["mesflow-postgres", "mesflow-app"]


# -- Scenario C: MESFlow port conflict -> fails safely, owner shown, nothing killed --

def test_scenario_c_port_conflict_fails_safely_without_killing_owner():
    docker = FakeDocker({
        "mesflow-postgres": {"exists": True, "running": True, "health": "healthy"},
        "mesflow-app": {"exists": True, "running": False, "health": "none",
                         "port_bindings": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]}},
        "mesflow-nginx": {"exists": True, "running": True, "health": "healthy"},
        "mesflow-qa-center": {"exists": True, "running": True, "health": "healthy"},
    })
    listeners = [{"ip": "0.0.0.0", "port": 8080, "pid": 9999, "process": "some-other-proc"}]
    orch = make_orchestrator(docker, listeners_fn=lambda: listeners,
                              container_pid_lookup_fn=lambda: {9999: "unrelated-container"})
    run = orch.recover_stack(actor="tester")
    mesflow_step = next(s for s in run.steps if s.service_id == "mesflow")
    assert mesflow_step.action == "FAILED_PORT_CONFLICT"
    conflict = mesflow_step.detail["conflicts"][0]
    assert conflict["port"] == 8080 and conflict["owner_pid"] == 9999
    assert conflict["owner_container"] == "unrelated-container"
    assert _start_commands(docker) == []  # `docker start` was never even attempted
    assert all(c[:2] != ["docker", "stop"] and c[:2] != ["docker", "kill"] for c in docker.commands)


# -- Scenario D: MESFlow unhealthy -> diagnosis is non-empty and actionable --

def test_scenario_d_diagnose_container_unhealthy():
    service = service_by_id(DEFAULT_MANAGED_SERVICES, "mesflow")
    inspection = {"running": True, "health": "unhealthy", "restart_count": 1, "exists": True}
    result = diagnose("container_unhealthy", service=service, inspection=inspection)
    assert result["summary"] and result["evidence"] and result["probable_cause"] and result["recommended_action"]


# -- Scenario E: QA stopped -> QA can start independently --

def test_scenario_e_qa_starts_independently():
    docker = FakeDocker({
        "mesflow-postgres": {"exists": True, "running": True, "health": "healthy"},
        "mesflow-app": {"exists": True, "running": True, "health": "healthy"},
        "mesflow-nginx": {"exists": True, "running": False, "health": "none"},
        "mesflow-qa-center": {"exists": True, "running": False, "health": "none"},
    })
    step = make_orchestrator(docker).start_service("qa", actor="tester")
    assert step.action == "STARTED"
    assert _start_commands(docker) == [["docker", "start", "mesflow-qa-center"]]


# -- Scenario F: unrelated container -> never offered a managed Start --

def test_scenario_f_unrelated_container_is_rejected():
    docker = FakeDocker({})
    with pytest.raises(NotManagedError):
        make_orchestrator(docker).start_service("some-other-container", actor="tester")
    assert docker.commands == []  # rejected before any docker call


# -- Scenario G: repeated recovery click -> idempotent / safe --

def test_scenario_g_repeated_recovery_is_idempotent():
    docker = FakeDocker({
        "mesflow-postgres": {"exists": True, "running": True, "health": "healthy"},
        "mesflow-app": {"exists": True, "running": False, "health": "none"},
        "mesflow-nginx": {"exists": True, "running": True, "health": "healthy"},
        "mesflow-qa-center": {"exists": True, "running": True, "health": "healthy"},
    })
    orch = make_orchestrator(docker)
    orch.recover_stack(actor="tester")
    assert len(_start_commands(docker)) == 1
    run2 = orch.recover_stack(actor="tester")
    assert len(_start_commands(docker)) == 1  # no NEW start issued
    assert all(s.action == "SKIPPED_ALREADY_HEALTHY" for s in run2.steps)


# -- Scenario H: Postgres already healthy -> never restarted unnecessarily --

def test_scenario_h_healthy_postgres_never_restarted():
    docker = FakeDocker({
        "mesflow-postgres": {"exists": True, "running": True, "health": "healthy"},
        "mesflow-app": {"exists": True, "running": True, "health": "healthy"},
        "mesflow-nginx": {"exists": True, "running": True, "health": "healthy"},
        "mesflow-qa-center": {"exists": True, "running": True, "health": "healthy"},
    })
    make_orchestrator(docker).recover_stack(actor="tester")
    assert _mutating_commands(docker) == []


# -- Concurrency: a second overlapping recover_stack() call is refused --

def test_recover_stack_refuses_concurrent_run():
    docker = FakeDocker({s["container"]: {"exists": True, "running": True, "health": "healthy"}
                          for s in DEFAULT_MANAGED_SERVICES})
    orch = make_orchestrator(docker)
    orch._stack_lock.acquire()
    try:
        with pytest.raises(RecoveryAlreadyRunning):
            orch.recover_stack(actor="tester")
    finally:
        orch._stack_lock.release()


# -- PRODUCTION gating --

def test_production_start_requires_approval():
    docker = FakeDocker({"mesflow-postgres": {"exists": True, "running": False, "health": "none"}})
    orch = make_orchestrator(docker, server_role_provider=lambda: "PRODUCTION")
    step = orch.start_service("postgres", actor="tester")
    assert step.action == "BLOCKED" and step.reason == "PRODUCTION_APPROVAL_REQUIRED"
    assert _start_commands(docker) == []


def test_production_start_allowed_with_explicit_approval():
    docker = FakeDocker({"mesflow-postgres": {"exists": True, "running": False, "health": "none"}})
    orch = make_orchestrator(docker, server_role_provider=lambda: "PRODUCTION")
    step = orch.start_service("postgres", actor="tester", production_approved=True)
    assert step.action == "STARTED"


# -- Audit --

def test_audit_recorded_for_every_mutation_attempt():
    events = []
    docker = FakeDocker({"mesflow-postgres": {"exists": True, "running": False, "health": "none"}})
    orch = make_orchestrator(docker, audit=lambda action, data: events.append((action, data)))
    orch.start_service("postgres", actor="tester", incident_id="inc-1")
    assert len(events) == 1
    action, data = events[0]
    assert action == "RECOVERY_START"
    for key in ("who", "when", "server", "service", "action", "previous_state", "result", "reason", "incident_id"):
        assert key in data
    assert data["incident_id"] == "inc-1" and data["result"] == "STARTED"


# -- Guard / pure-function coverage --

def test_can_start_refuses_already_running():
    service = service_by_id(DEFAULT_MANAGED_SERVICES, "postgres")
    result = can_start(service, {"exists": True, "running": True, "health": "healthy"}, {}, "DEV", False)
    assert result == GuardResult(False, "ALREADY_RUNNING")


def test_can_start_blocks_on_unhealthy_dependency():
    service = service_by_id(DEFAULT_MANAGED_SERVICES, "mesflow")
    result = can_start(service, {"running": False, "health": "none"}, {"postgres": "down"}, "DEV", False)
    assert not result.allowed and result.reason == "DEPENDENCY_NOT_READY:postgres"


def test_inspect_container_missing_is_distinct_from_stopped():
    docker = FakeDocker({})
    result = inspect_container("does-not-exist", docker.run_fn)
    assert result["exists"] is False and result["running"] is False


def test_parse_expected_ports():
    bindings = {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}], "5432/tcp": [], "9/udp": [{"HostPort": "9"}]}
    assert sorted(parse_expected_ports(bindings)) == [(9, "udp"), (8080, "tcp")]


def test_service_id_for_incident_mapping():
    assert service_id_for_incident("MESFLOW_DOWN") == "mesflow"
    assert service_id_for_incident("QA_DOWN") == "qa"
    assert service_id_for_incident("CONTAINER_DOWN:mesflow-postgres") == "postgres"
    assert service_id_for_incident("CONTAINER_UNHEALTHY:mesflow-nginx") == "nginx"
    assert service_id_for_incident("DISK_CRITICAL") is None
    assert service_id_for_incident("CONTAINER_DOWN:some-unrelated-thing") is None


def test_diagnose_unknown_kind_raises():
    with pytest.raises(ValueError):
        diagnose("not_a_real_kind")


def test_system_check_bands_and_overall_rollup():
    host_summary = {"cpu_percent": 20, "ram": {"percent": 30}, "swap": {"percent": 0}, "disk": {"percent": 40}}
    healthy_views = [{"id": "postgres", "label": "PostgreSQL", "exists": True, "running": True, "health": "healthy"}]
    result = system_check(host_summary, healthy_views, docker_available=True)
    assert result["status"] == "HEALTHY"

    down_views = [{"id": "postgres", "label": "PostgreSQL", "exists": True, "running": False, "health": "none"}]
    result = system_check(host_summary, down_views, docker_available=True)
    assert result["status"] == "CRITICAL"  # postgres is a critical_service

    degraded_views = [{"id": "qa", "label": "QA Center", "exists": True, "running": False, "health": "none"}]
    result = system_check(host_summary, degraded_views, docker_available=True)
    assert result["status"] == "DEGRADED"  # qa is not critical

    result = system_check(host_summary, [], docker_available=False)
    assert result["status"] == "CRITICAL"
