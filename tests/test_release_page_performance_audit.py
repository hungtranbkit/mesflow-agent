"""Release Page Performance Audit (reports/RELEASE_PAGE_PERFORMANCE_AUDIT.md).

Root cause (measured live against the real running local Agent): opening
Release & Deploy took ~15.3s, ~99% of it inside mes_status() alone. It
makes 3 independent http_json() calls per page load (version/health/ready),
and each one re-walks the full MES URL candidate list from scratch. When
this Agent's container is not on the same Docker network as the MESFlow
container (a real, reproduced condition on this host: mesflow-deploy-agent
is on `mesflow-edge`, mesflow-app is on `mesflow_network`), resolving
MES_URL's hostname fails via glibc's resolver in ~5s (EAI_AGAIN) --
NOT bounded by urlopen's `timeout=` parameter, which only covers the
socket connect/read stage that never starts if DNS resolution itself
hangs. 3 calls x ~5s == the measured ~15.2s.

These tests cover the fix: a short-TTL cache of "this base URL failed at
the connection level" so a single page load's 3 http_json() calls -- and
rapid repeat page loads -- don't each re-pay the DNS cost.
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
    os.environ["WORKSHOP_MES_URL"] = "http://mesflow-app-does-not-resolve.invalid:8080"
    os.environ["MESFLOW_POSTGRES_CONTAINER"] = f"perf-audit-test-no-postgres-{uuid.uuid4().hex[:8]}"
    tag = uuid.uuid4().hex[:8]
    spec = importlib.util.spec_from_file_location(f"agent_perf_audit_{tag}", ROOT / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod.app.testing = True
    # Fresh module -> fresh cache each test.
    mod._MES_BASE_UNREACHABLE.clear()
    return mod


def test_http_json_caches_connection_level_failure_per_base(agent):
    calls = []

    def fake_http_json_base(base, path, timeout=4):
        calls.append(base)
        return {"ok": False, "error": "simulated DNS/connect failure"}, 0

    agent._http_json_base = fake_http_json_base
    for _ in range(3):
        agent.http_json("/api/system/version")
    # The exact request mes_status() makes 3x per page load -- the fix must
    # only actually attempt each unreachable base ONCE within the TTL, not
    # re-resolve it fresh on every one of the 3 calls.
    assert calls.count(agent.MES_URL) == 1


def test_http_json_stops_caching_once_a_base_answers(agent):
    responses = iter([
        ({"ok": False, "error": "still down"}, 0),
        ({"ok": True, "version": "1.2.3"}, 200),
    ])
    calls = []

    def fake_http_json_base(base, path, timeout=4):
        if base == agent.MES_URL:
            calls.append(base)
            return next(responses)
        return {"ok": False, "error": "connection refused"}, 0

    agent._http_json_base = fake_http_json_base
    payload1, code1 = agent.http_json("/api/system/version")
    assert code1 == 0  # cached-fast-fail path this call
    # Clear the cache the way a real recovery would surface (TTL elapsing) --
    # simulate directly rather than sleeping 20s in a unit test.
    agent._MES_BASE_UNREACHABLE.pop(agent.MES_URL, None)
    payload2, code2 = agent.http_json("/api/system/version")
    assert code2 == 200 and payload2["version"] == "1.2.3"
    # A real HTTP response (even from a prior failure state) must clear the
    # cache immediately -- the next call must not still skip this base.
    assert not agent._mes_base_recently_unreachable(agent.MES_URL)


def test_mes_status_bounded_when_mes_url_unreachable(agent, monkeypatch):
    # mes_status() must not block the whole request for multiples of the
    # DNS-failure cost -- simulate the real ~5s-per-DNS-attempt cost and
    # assert the fix keeps mes_status() under roughly one such cost, not
    # three of them.
    monkeypatch.setattr(agent, "_docker_mes_state", lambda: {"available": True, "running": True})

    def slow_fake(base, path, timeout=4):
        if base == agent.MES_URL:
            time.sleep(0.3)  # stand-in for the real ~5s DNS stall, scaled down for test speed
            return {"ok": False, "error": "simulated DNS failure"}, 0
        return {"ok": False, "error": "connection refused"}, 0

    agent._http_json_base = slow_fake
    start = time.perf_counter()
    agent.mes_status()
    elapsed = time.perf_counter() - start
    # Without the fix this would cost ~3x the simulated DNS stall (~0.9s);
    # with the cache it should cost at most ~1x plus a small margin.
    assert elapsed < 0.6, f"mes_status() took {elapsed:.3f}s -- cache did not prevent repeated DNS-cost attempts"


def test_release_deploy_context_does_not_recompute_expensive_functions_twice(agent, monkeypatch):
    # Same guarantee as the earlier performance fix (reports/
    # DEPLOY_AGENT_REFRESH_PERFORMANCE_FIX.md), re-verified here: the Release
    # Deploy tab context builder must still pass its already-computed
    # mes/qa/release_summary/qa_release_summary straight into
    # _deployment_platform_summary() rather than letting it recompute them.
    calls = {"mes_status": 0, "qa_status": 0, "_release_summary": 0, "_qa_release_summary": 0}
    orig = {name: getattr(agent, name) for name in calls}

    def wrap(name):
        def inner(*a, **k):
            calls[name] += 1
            return orig[name](*a, **k)
        return inner

    for name in calls:
        monkeypatch.setattr(agent, name, wrap(name))
    with agent.app.test_request_context("/releases?tab=deploy"):
        agent._release_deploy_tab_context()
    for name, count in calls.items():
        assert count == 1, f"{name} called {count} times, expected exactly 1"
