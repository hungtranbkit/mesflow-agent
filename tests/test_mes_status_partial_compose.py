"""Production false negative (deploy.mesflow.net /agent/health, 2026-11-12):

  mes.docker = {available: true, services: ["mesflow"],
                service_health: {mesflow: healthy, postgres: stopped}, running: false}
  mes.api_online = false, health_payload = MESFLOW_CONTAINER_NOT_RUNNING

while https://mesflow.net/api/system/ready answered 200 with a real
migration_head (the DB is up -- it is simply not the `postgres` service of the
/opt/mesflow Compose project). mes_status() skipped every HTTP probe because
"running" requires BOTH postgres and mesflow in that project, so the Agent
reported MESFlow down without ever asking it. The skip must only apply when
the mesflow service itself is confirmed not running.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROD_DOCKER = {"available": True, "running": False, "healthy": False, "health": "stopped", "services": ["mesflow"],
               "service_health": {"mesflow": "healthy", "postgres": "stopped"},
               "container_id": "ba3290d07b59", "container_ids": {"mesflow": "ba3290d07b59"}, "stderr": ""}


@pytest.fixture()
def agent(tmp_path):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["WORKSHOP_AGENT_HOME"] = str(tmp_path)
    os.environ.setdefault("MESFLOW_AGENT_RUNTIME", "linux")
    os.environ.setdefault("MESFLOW_AGENT_ADMIN_PASSWORD", "TestPw1234567")
    os.environ["SERVER_ROLE"] = "DEV"
    os.environ["MESFLOW_QA_URL"] = "http://127.0.0.1:1"
    os.environ["WORKSHOP_MES_URL"] = "http://127.0.0.1:1"
    os.environ["MESFLOW_POSTGRES_CONTAINER"] = f"partial-compose-test-no-postgres-{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(f"agent_partial_{uuid.uuid4().hex[:8]}", ROOT / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _live_mes(path, timeout=4):
    return ({"/api/system/version": {"version": "71.0.0.390"},
             "/api/system/health": {"ok": True, "critical_ok": True},
             "/api/system/ready": {"ok": True, "migration_head": "0054_multi_open_session_per_employee"}}[path], 200)


def test_mesflow_service_running_but_db_outside_project_is_probed_and_online(agent, monkeypatch):
    monkeypatch.setattr(agent, "_docker_mes_state", lambda: dict(PROD_DOCKER))
    calls = []
    monkeypatch.setattr(agent, "http_json", lambda p, timeout=4: (calls.append(p), _live_mes(p))[1])
    st = agent.mes_status()
    assert calls == ["/api/system/version", "/api/system/health", "/api/system/ready"]
    assert st["api_online"] is True and st["online"] is True and st["healthy"] is True
    assert st["version"] == "71.0.0.390" and st["migration_head"].startswith("0054")
    assert st["health_payload"].get("error") != "MESFLOW_CONTAINER_NOT_RUNNING"


def test_mesflow_service_itself_stopped_still_skips_probe(agent, monkeypatch):
    stopped = dict(PROD_DOCKER, services=["postgres"], service_health={"mesflow": "stopped", "postgres": "healthy"})
    monkeypatch.setattr(agent, "_docker_mes_state", lambda: stopped)
    monkeypatch.setattr(agent, "http_json", lambda *a, **k: pytest.fail("probe must be skipped"))
    st = agent.mes_status()
    assert st["api_online"] is False and st["health_payload"]["error"] == "MESFLOW_CONTAINER_NOT_RUNNING"


def test_mesflow_running_but_http_unreachable_is_reported_not_masked(agent, monkeypatch):
    monkeypatch.setattr(agent, "_docker_mes_state", lambda: dict(PROD_DOCKER))
    monkeypatch.setattr(agent, "http_json", lambda p, timeout=4: ({"ok": False, "error": "connection refused"}, 0))
    st = agent.mes_status()
    assert st["api_online"] is False
    assert st["health_payload"]["error"] == "connection refused"   # real probe result, not a guess


def test_legacy_ota_routes_do_not_depend_on_mes_status(agent):
    src = (ROOT / "agent.py").read_text(encoding="utf-8")
    names = [n for n in ("_mesflow_readiness", "_mesflow_inventory", "_mesflow_json") if f"def {n}(" in src]
    assert "_mesflow_readiness" in names
    bodies = {n: src.split(f"def {n}(", 1)[1].split("\ndef ", 1)[0] for n in names}
    for name, body in bodies.items():
        assert "mes_status(" not in body and "_docker_mes_state(" not in body, name
    assert any("MES_URL" in b for b in bodies.values())
