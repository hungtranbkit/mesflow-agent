"""Server Incident Recovery Console -- Flask-route-level wiring, mirroring
tests/test_p5_fleet_routes.py's lightweight importlib-loaded `agent`
fixture (no subprocess needed for these checks).

SAFETY: every managed container name is overridden to a name that
provably does not exist on this host (mirrors the existing
MESFLOW_POSTGRES_CONTAINER safety default other tests already rely on --
see AGENTS.md and agent.py's MANAGED_SERVICES wiring). This test file
must NEVER be able to reach a real 'mesflow-postgres'/'mesflow-app'/etc
container, even read-only 'docker inspect' -- see the module docstring
in agent_backend/incident_recovery.py for why that boundary matters.
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def agent(tmp_path):
    import os
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["WORKSHOP_AGENT_HOME"] = str(tmp_path)
    os.environ.setdefault("MESFLOW_AGENT_RUNTIME", "linux")
    os.environ.setdefault("MESFLOW_AGENT_ADMIN_PASSWORD", "TestPw1234567")
    os.environ["SERVER_ROLE"] = "DEV"
    os.environ["MESFLOW_QA_URL"] = "http://127.0.0.1:1"
    os.environ["WORKSHOP_MES_URL"] = "http://127.0.0.1:1"
    tag = uuid.uuid4().hex[:8]
    os.environ["MESFLOW_POSTGRES_CONTAINER"] = f"recovery-test-no-postgres-{tag}"
    os.environ["MESFLOW_APP_CONTAINER"] = f"recovery-test-no-app-{tag}"
    os.environ["MESFLOW_NGINX_CONTAINER"] = f"recovery-test-no-nginx-{tag}"
    os.environ["MESFLOW_QA_CONTAINER"] = f"recovery-test-no-qa-{tag}"
    spec = importlib.util.spec_from_file_location(f"agent_recovery_routes_{tag}", ROOT / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod.app.testing = True
    return mod


def _client(agent):
    client = agent.app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "test-csrf"
        sess["username"] = "test-admin"
    return client


CSRF = {"X-CSRF-Token": "test-csrf"}

GET_ROUTES = [
    "/api/ops/recovery/services",
    "/api/ops/recovery/services/postgres",
    "/api/ops/recovery/services/postgres/logs",
    "/api/ops/recovery/services/postgres/ports",
    "/api/ops/recovery/preflight",
    "/api/ops/recovery/system-check",
    "/api/ops/recovery/diagnose/docker_unavailable",
    "/api/ops/recovery/audit",
]

POST_ROUTES = [
    "/api/ops/recovery/services/postgres/start",
    "/api/ops/recovery/services/postgres/restart",
    "/api/ops/recovery/services/postgres/stop",
    "/api/ops/recovery/stack",
]


def test_get_routes_require_login(agent):
    client = agent.app.test_client()
    for path in GET_ROUTES:
        resp = client.get(path)
        assert resp.status_code == 401, path


def test_post_routes_require_login(agent):
    # /api/ops/recovery/stack (bulk "restart everything") deliberately
    # stays OUT of protect()'s exempt set -- Deploy Agent's own browser
    # session only, no internal-token path, no MESFlow SUPER_ADMIN
    # exposure for this one (task section: avoid exposing destructive
    # actions unless there is a real operational need). before_request
    # redirects an unauthenticated request before the handler even runs.
    client = agent.app.test_client()
    resp = client.post("/api/ops/recovery/stack", json={})
    assert resp.status_code == 302

    # start/restart/stop ARE now exempt from the blanket redirect (same as
    # the read-only recovery GET routes) so MESFlow's SUPER_ADMIN System
    # Console can reach them with the internal token instead of a Deploy
    # Agent browser session -- the route body itself still requires
    # logged_in() OR _internal_allowed(), so a bare unauthenticated request
    # gets a real 401 JSON response, not a login redirect.
    for path in ("/api/ops/recovery/services/postgres/start",
                 "/api/ops/recovery/services/postgres/restart",
                 "/api/ops/recovery/services/postgres/stop"):
        resp = client.post(path, json={})
        assert resp.status_code == 401, path
        assert resp.get_json()["error"] == "AUTH_REQUIRED"


def test_post_routes_require_csrf(agent):
    client = _client(agent)
    for path in POST_ROUTES:
        resp = client.post(path, json={})  # no X-CSRF-Token header
        assert resp.status_code == 403, path


def test_get_routes_work_when_logged_in(agent):
    client = _client(agent)
    for path in GET_ROUTES:
        resp = client.get(path)
        assert resp.status_code == 200, (path, resp.get_json())


def test_start_restart_stop_accept_the_internal_token_no_session_needed(agent, monkeypatch):
    # MESFlow's SUPER_ADMIN System Console calls these three with
    # X-MESFlow-Internal-Token instead of a Deploy Agent browser session
    # (same shared-secret pattern api_ops_summary already used). No CSRF
    # token is required on this path -- verify_csrf() only runs when
    # logged_in() is true (a real session with a forgeable ambient cookie);
    # a bearer secret in a custom header isn't subject to that threat model.
    monkeypatch.setenv("MESFLOW_INTERNAL_API_TOKEN", "shared-test-token")
    client = agent.app.test_client()
    headers = {"X-MESFlow-Internal-Token": "shared-test-token"}
    for path in ("/api/ops/recovery/services/postgres/start",
                 "/api/ops/recovery/services/postgres/restart",
                 "/api/ops/recovery/services/postgres/stop"):
        resp = client.post(path, json={"actor": "mesflow-super-admin", "reason": "test"}, headers=headers)
        assert resp.status_code == 200, (path, resp.get_json())
        # the container genuinely doesn't exist (test-safe naming) -- the
        # call must still be REAL (reach the orchestrator, get a real
        # FAILED_TO_* outcome), not silently no-op into a fake success.
        assert resp.get_json()["ok"] is False

    audit_items = client.get("/api/ops/recovery/audit", headers=headers).get_json()["items"]
    assert any(a["who"] == "mesflow-super-admin" for a in audit_items), audit_items


def test_wrong_internal_token_is_rejected(agent, monkeypatch):
    monkeypatch.setenv("MESFLOW_INTERNAL_API_TOKEN", "shared-test-token")
    client = agent.app.test_client()
    resp = client.post("/api/ops/recovery/services/postgres/restart", json={},
                        headers={"X-MESFlow-Internal-Token": "wrong-token"})
    assert resp.status_code == 401


def test_stack_route_never_accepts_the_internal_token(agent, monkeypatch):
    # Deliberate scope limit: the bulk "restart everything" action stays
    # Deploy Agent console-only, unlike the three individual service
    # actions above.
    monkeypatch.setenv("MESFLOW_INTERNAL_API_TOKEN", "shared-test-token")
    client = agent.app.test_client()
    resp = client.post("/api/ops/recovery/stack", json={},
                        headers={"X-MESFlow-Internal-Token": "shared-test-token"})
    assert resp.status_code == 302  # still redirected by protect(), never reaches the handler


def test_services_list_is_exactly_the_four_allowlisted_rows_in_order(agent):
    client = _client(agent)
    items = client.get("/api/ops/recovery/services").get_json()["items"]
    assert [row["id"] for row in items] == ["postgres", "mesflow", "nginx", "qa"]
    containers = {row["container"] for row in items}
    # every configured container name really is the test-safe nonexistent one
    assert all("recovery-test-no-" in c for c in containers)


def test_unmanaged_service_id_is_rejected_everywhere(agent):
    client = _client(agent)
    resp = client.get("/api/ops/recovery/services/some-unrelated-container")
    assert resp.status_code == 404 and resp.get_json()["error"] == "NOT_MANAGED"
    resp = client.get("/api/ops/recovery/services/some-unrelated-container/logs")
    assert resp.status_code == 404
    resp = client.get("/api/ops/recovery/services/some-unrelated-container/ports")
    assert resp.status_code == 404
    resp = client.post("/api/ops/recovery/services/some-unrelated-container/start", json={}, headers=CSRF)
    assert resp.status_code == 404 and resp.get_json()["error"] == "NOT_MANAGED"
    resp = client.post("/api/ops/recovery/services/some-unrelated-container/restart", json={}, headers=CSRF)
    assert resp.status_code == 404
    resp = client.post("/api/ops/recovery/services/some-unrelated-container/stop", json={}, headers=CSRF)
    assert resp.status_code == 404


def test_start_on_missing_test_container_fails_safely_not_silently(agent):
    # The container genuinely does not exist (that's the whole point of the
    # test-safe naming) -- start must report a clean FAILED_TO_START, never
    # raise/crash, and every attempt must land exactly one audit entry.
    client = _client(agent)
    resp = client.post("/api/ops/recovery/services/postgres/start", json={}, headers=CSRF)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["item"]["action"] == "FAILED_TO_START"
    audit_items = client.get("/api/ops/recovery/audit").get_json()["items"]
    assert len(audit_items) == 1
    assert audit_items[0]["service"] == "postgres" and audit_items[0]["result"] == "FAILED_TO_START"


def test_diagnose_unknown_kind_is_404(agent):
    client = _client(agent)
    resp = client.get("/api/ops/recovery/diagnose/not_a_real_kind")
    assert resp.status_code == 404


def test_recover_stack_returns_completed_run_with_four_steps(agent):
    client = _client(agent)
    resp = client.post("/api/ops/recovery/stack", json={}, headers=CSRF)
    assert resp.status_code == 200
    steps = resp.get_json()["item"]["steps"]
    assert [s["service_id"] for s in steps] == ["postgres", "mesflow", "nginx", "qa"]


def test_incident_detail_carries_recovery_recommendation_or_none(agent):
    client = _client(agent)
    # Synthesize a MESFLOW_DOWN incident directly through the store the
    # same way the real monitor loop would, without needing the poll loop
    # to actually fire during the test.
    store = agent._load_incidents()
    store.setdefault("items", {})["MESFLOW_DOWN"] = {
        "incident_id": "MESFLOW_DOWN", "incident_type": "MESFLOW_DOWN", "status": "ACTIVE",
        "severity": "CRITICAL", "summary": "MESFlow unreachable", "service": "mesflow",
        "started_at": "2026-01-01 00:00:00", "last_seen_at": "2026-01-01 00:00:00", "occurrence_count": 1,
    }
    agent._save_incidents(store)
    resp = client.get("/api/incidents/MESFLOW_DOWN")
    assert resp.status_code == 200
    item = resp.get_json()["item"]
    assert item["recovery"]["service_id"] == "mesflow"
    assert any(a["type"] == "logs" for a in item["recovery"]["actions"])
