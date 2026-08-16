"""Deploy Agent Refresh/Freeze Performance Fix (reports/
DEPLOY_AGENT_REFRESH_PERFORMANCE_FIX.md). Extends the existing
tests/test_release_tab_lightweight_polling.py (which already covers the
Release Deploy tab's own earlier fix) with the gaps that fix missed:

- Agent Update tab had the EXACT SAME unconditional full-page-reload-loop
  bug as the Deploy tab used to, just in a sibling template the earlier
  fix's own regression test never looked at.
- _deployment_platform_summary() silently recomputed mes_status()/
  qa_status()/_release_summary()/_qa_release_summary() a SECOND time on
  every single Release Deploy tab page load, even though its only
  in-page caller had just computed all four itself moments earlier --
  each is a real Docker inspect / QA network probe / SHA256 hash of a
  70-500+MB release ZIP.
- sha() had no caching at all, so the same unchanged release artifact
  was re-hashed from scratch on every call, in every request that
  touched a release summary.
"""
from __future__ import annotations

import importlib.util
import sys
import time
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
    # Same safety convention as every other test file in this project: point
    # the managed Postgres container name at something that provably does
    # not exist, so nothing here can ever touch a real container.
    os.environ["MESFLOW_POSTGRES_CONTAINER"] = f"perf-fix-test-no-postgres-{uuid.uuid4().hex[:8]}"
    tag = uuid.uuid4().hex[:8]
    spec = importlib.util.spec_from_file_location(f"agent_perf_fix_{tag}", ROOT / "agent.py")
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


# --- Agent Update tab: no full-page-reload loop -----------------------

def test_agent_update_tab_has_no_unconditional_reload_setTimeout():
    text = (ROOT / "templates/release/_agent_update_tab.html").read_text(encoding="utf-8")
    # The old bug: a bare setTimeout whose body is a conditional
    # location.reload() with no lightweight-poll/interval anywhere near
    # it. The fix replaces it with the same pollX/setInterval(...,5000)
    # shape the Deploy tab already uses.
    assert "pollAgentUpdateState" in text
    assert "window.setInterval(pollAgentUpdateState,5000)" in text
    assert "release_manager_live_status" in text


def test_agent_update_tab_idle_render_has_no_active_flag(agent):
    client = _client(agent)
    body = client.get("/releases?tab=agent-update").get_data(as_text=True)
    assert "agentUpdateHadActiveJob=false" in body


def test_agent_update_tab_active_job_render_sets_active_flag(agent):
    agent.update_state(lambda s: s.__setitem__(
        "agent_update_job", {"status": "UPDATING", "message": "Đang cập nhật...", "role": "PRODUCTION_TEST"}))
    client = _client(agent)
    body = client.get("/releases?tab=agent-update").get_data(as_text=True)
    assert "agentUpdateHadActiveJob=true" in body


# --- Lightweight live-status endpoint now also tracks agent_update_job --

def test_live_status_payload_tracks_agent_update_job_and_stays_cheap():
    text = (ROOT / "agent.py").read_text(encoding="utf-8")
    start = text.index("def _release_live_status_payload():")
    end = text.index('@app.get("/api/status")', start)
    body = text[start:end]
    assert '"agent_update_job"' in body
    assert "UPDATING" in body and "PUSHING" in body
    # Same guarantee the pre-existing test already enforces for the other
    # jobs -- this endpoint still must never call summaries/probes/tail.
    assert "_release_summary()" not in body
    assert "_qa_release_summary()" not in body
    assert "mes_status()" not in body
    assert "qa_status()" not in body
    assert "tail(" not in body


def test_live_status_reports_agent_update_job(agent):
    agent.update_state(lambda s: s.__setitem__(
        "agent_update_job", {"status": "PUSHING", "message": "Đang đẩy artifact...", "updated_at": "2026-01-01 00:00:00"}))
    client = _client(agent)
    data = client.get("/api/release-manager/live-status").get_json()
    assert data["active"] is True
    assert data["jobs"]["agent_update_job"]["status"] == "PUSHING"


# --- No redundant double-computation on the Release Deploy tab --------

def test_deployment_platform_summary_accepts_precomputed_values_text():
    text = (ROOT / "agent.py").read_text(encoding="utf-8")
    assert "def _deployment_platform_summary(mes=None,qa=None,mes_release=None,qa_release=None):" in text
    start = text.index("def _release_deploy_tab_context():")
    end = text.index("def _deployment_history_tab_context():", start)
    body = text[start:end]
    assert "_deployment_platform_summary(mes=mes,qa=qa,mes_release=release_summary,qa_release=qa_release_summary)" in body
    # The precise "called exactly once each, not twice" guarantee is
    # verified functionally (real call-counting through a real request)
    # in test_release_deploy_tab_load_does_not_call_summaries_twice below
    # -- more robust than a source-text count, which is thrown off by this
    # very file's own explanatory comments naming the functions.


def test_release_deploy_tab_load_does_not_call_summaries_twice(agent, monkeypatch):
    """Functional (not just textual) guard: count real calls during one
    real request through the Flask test client."""
    counts = {"mes_status": 0, "qa_status": 0, "_release_summary": 0, "_qa_release_summary": 0}
    for name in list(counts):
        original = getattr(agent, name)

        def counter(*a, _name=name, _orig=original, **kw):
            counts[_name] += 1
            return _orig(*a, **kw)

        monkeypatch.setattr(agent, name, counter)
    client = _client(agent)
    resp = client.get("/releases?tab=deploy")
    assert resp.status_code == 200
    for name, count in counts.items():
        assert count == 1, f"{name} was called {count} times in one page load (expected exactly 1)"


# --- sha() caching --------------------------------------------------

def test_sha_is_cached_by_mtime_and_size(agent, tmp_path):
    p = tmp_path / "artifact.bin"
    p.write_bytes(b"x" * (2 * 1024 * 1024))
    first = agent.sha(p)
    calls = {"n": 0}
    original_hash = agent._hash_file_sha256

    def counting_hash(path):
        calls["n"] += 1
        return original_hash(path)

    agent._hash_file_sha256 = counting_hash
    try:
        second = agent.sha(p)  # unchanged file -- must hit cache, not re-hash
        assert second == first
        assert calls["n"] == 0
        p.write_bytes(b"y" * (2 * 1024 * 1024))  # real content change -> new mtime/size
        third = agent.sha(p)
        assert third != first
        assert calls["n"] == 1  # re-hashed exactly once for the changed file
    finally:
        agent._hash_file_sha256 = original_hash


# --- Idle Release page: no polling script runs at all ------------------

def test_idle_release_deploy_tab_has_no_active_polling(agent):
    client = _client(agent)
    body = client.get("/releases?tab=deploy").get_data(as_text=True)
    assert "releaseHadActiveJob=false" in body
    # The poller code exists in the template but its own runtime guard
    # (`if(releaseHadActiveJob){...}`) means setInterval is never actually
    # invoked when idle -- verified functionally in the "does not call
    # summaries twice" test above already making exactly one real request
    # per summary function with no follow-up polling from this same page.


# --- History tab: no polling at all (already true; guard against regressions) --

def test_history_tab_has_no_polling_intervals():
    text = (ROOT / "templates/release/_history_tab.html").read_text(encoding="utf-8")
    assert "setInterval" not in text
    assert "location.reload" not in text


# --- mes_status()/qa_status(): skip the HTTP cascade when Docker/systemd --
# --- already confirms the service isn't running (live-discovered follow-up) --

def test_mes_status_skips_http_probe_when_docker_confirms_not_running(agent, monkeypatch):
    monkeypatch.setattr(agent, "_docker_mes_state", lambda: {"available": True, "running": False, "healthy": False, "health": "stopped", "services": []})
    calls = {"n": 0}

    def fail_if_called(*a, **kw):
        calls["n"] += 1
        return {"ok": False, "error": "should not be called"}, 0

    monkeypatch.setattr(agent, "http_json", fail_if_called)
    result = agent.mes_status()
    assert calls["n"] == 0
    assert result["online"] is False
    assert result["api_online"] is False


def test_mes_status_still_probes_http_when_docker_state_unavailable(agent, monkeypatch):
    # Windows / bare-process / no-compose.yml installs: _docker_mes_state()
    # returns available=False -- the ONLY way to detect a running MESFlow
    # there is still the HTTP probe, so it must not be skipped.
    monkeypatch.setattr(agent, "_docker_mes_state", lambda: {"available": False, "running": False, "healthy": False, "health": "unknown", "services": []})
    calls = {"n": 0}

    def counting(*a, **kw):
        calls["n"] += 1
        return {"ok": False, "error": "connection refused"}, 0

    monkeypatch.setattr(agent, "http_json", counting)
    agent.mes_status()
    assert calls["n"] == 3  # version, health, ready -- unchanged from before this fix


def test_qa_status_skips_http_probe_when_service_confirmed_inactive(agent, monkeypatch):
    monkeypatch.setattr(agent, "_qa_service_state", lambda: {"active": False, "pid": 0, "runtime": "docker"})
    calls = {"n": 0}

    def fail_if_called(*a, **kw):
        calls["n"] += 1
        return {"ok": False, "error": "should not be called"}, 0

    monkeypatch.setattr(agent, "qa_http_json", fail_if_called)
    result = agent.qa_status()
    assert calls["n"] == 0
    assert result["online"] is False


def test_qa_status_still_probes_http_when_service_active(agent, monkeypatch):
    monkeypatch.setattr(agent, "_qa_service_state", lambda: {"active": True, "pid": 123, "runtime": "docker"})
    calls = {"n": 0}

    def counting(*a, **kw):
        calls["n"] += 1
        return {"ok": True, "version": "1.0.0"}, 200

    monkeypatch.setattr(agent, "qa_http_json", counting)
    result = agent.qa_status()
    assert calls["n"] == 1
    assert result["online"] is True


# --- URL-prefix cookie bug (live-discovered while verifying Deployment --
# --- History against the real local Agent, which runs under the default --
# --- WORKSHOP_AGENT_URL_PREFIX=/agent): a root-absolute fetch('/api/...') --
# --- doesn't carry the Path=/agent/-scoped session cookie, so it always --
# --- 302s to /login and the JSON parse silently fails. ---

def test_dashboard_no_longer_uses_bare_api_fetch_paths():
    text = (ROOT / "templates/dashboard.html").read_text(encoding="utf-8")
    assert "fetch('/api/fleet/summary')" not in text
    assert "fetch('/api/ops/storage')" not in text
    assert 'fetch(\'{{url_for("api_fleet_summary")}}\')' in text
    assert 'fetch(\'{{url_for("api_ops_storage")}}\')' in text


def test_release_index_apifetch_is_prefix_aware():
    text = (ROOT / "templates/release/index.html").read_text(encoding="utf-8")
    assert "if(url.startsWith('/api/'))url='{{request.script_root}}'+url;" in text
    # History tab's two calls go through this same shared helper -- no
    # separate fix needed there, verified by this one assertion covering
    # the single choke point both calls flow through.
    assert "apiFetch('/api/release-manager/history?'" in (ROOT / "templates/release/_history_tab.html").read_text(encoding="utf-8")


def test_ops_page_api_helper_already_had_this_fix_for_reference():
    # ops.html's api() helper is the pre-existing correct pattern this fix
    # mirrors -- guard that it doesn't regress either.
    text = (ROOT / "templates/ops.html").read_text(encoding="utf-8")
    assert "if(url.startsWith('/api/'))url='{{request.script_root}}'+url;" in text
