"""Deploy Agent Package Build + Push workflow (task: AGENT_PACKAGE_BUILD_PUSH).

Covers: building the immutable AGENT_UPDATE_<version>.zip artifact from the
Agent Update page (source preflight -> version validation -> tests ->
package build -> SHA256 -> manifest -> immutable artifact), the download
route, and the push-to-Bootstrap job's state reporting -- without
reimplementing or touching Bootstrap's own updater.py (verify/install/
restart/health-check stay entirely inside its single synchronous
/updater/update call; this Agent's job here is build + orchestrate the
push, per bootstrap/AGENTS.md: "Bootstrap must remain the installer/update
authority").

Real `docker build` is NOT exercised in this suite (too slow for every
test run, and would recurse: build-agent-release.sh's own "tests" stage
runs this exact suite). The script's fast, Docker-free guards (bad
version, duplicate version) ARE exercised for real, against an isolated
copy of the repo -- same convention as
tests/test_package_installer_bump_integration.py.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
IGNORE = shutil.ignore_patterns(".venv", "__pycache__", ".git", "*.pyc", "node_modules", "runtime", "artifacts")


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WORKSHOP_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("MESFLOW_AGENT_RUNTIME", "linux")
    monkeypatch.setenv("MESFLOW_AGENT_ADMIN_PASSWORD", "TestPw1234567")
    monkeypatch.setenv("SERVER_ROLE", "DEV")
    monkeypatch.setenv("MESFLOW_BUILD_ENABLED", "1")
    monkeypatch.setenv("MESFLOW_QA_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("WORKSHOP_MES_URL", "http://127.0.0.1:1")
    # Isolated agent-releases root -- never the real artifacts/deploy-agent/
    # releases/ tree, which has real frozen versions. monkeypatch.setenv
    # (not a plain os.environ[...] = ...) so this reverts automatically at
    # the end of the test -- real bug found+fixed here: a plain assignment
    # leaked MESFLOW_SOURCE_DIR into the rest of the pytest session and
    # broke an unrelated MESFlow-release test that runs later and expects
    # the real workspace's MESFLOW_SOURCE_DIR.
    monkeypatch.setenv("MESFLOW_SOURCE_DIR", str(tmp_path / "mesflow-src"))
    deploy_agent_src = tmp_path / "deploy-agent-src"
    (deploy_agent_src / "scripts").mkdir(parents=True, exist_ok=True)
    (deploy_agent_src / "scripts" / "build-agent-release.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("DEPLOY_AGENT_SOURCE_DIR", str(deploy_agent_src))
    monkeypatch.setenv("MESFLOW_POSTGRES_CONTAINER", f"apbp-test-no-postgres-{uuid.uuid4().hex[:8]}")
    tag = uuid.uuid4().hex[:8]
    spec = importlib.util.spec_from_file_location(f"agent_apbp_{tag}", ROOT / "agent.py")
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


def _write_fake_release(agent, version="9.9.9-docker-runtime", built_at="2026-08-16T00:00:00+00:00"):
    root = agent._agent_releases_root() / version
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"type": "mesflow-agent-release", "version": version, "image": f"mesflow-deploy-agent:9.9.9",
                "image_digest": "sha256:" + "a" * 64, "image_id": "sha256:" + "a" * 64,
                "source_commit": "deadbeef1234", "built_at": built_at, "api_protocol_version": 1,
                "capabilities": [], "bundle": f"mesflow-deploy-agent_9.9.9.tar"}
    (root / "agent-release.json").write_text(json.dumps(manifest), encoding="utf-8")
    package = root / f"AGENT_UPDATE_{version}.zip"
    package.write_bytes(b"fake zip bytes")
    (root / f"AGENT_UPDATE_{version}.zip.sha256").write_text("deadbeef00  " + package.name + "\n", encoding="utf-8")
    return root, package


# --- _latest_agent_release() ------------------------------------------------

def test_latest_agent_release_empty_when_nothing_built(agent):
    assert agent._latest_agent_release() == {}


def test_latest_agent_release_reads_manifest_checksum_and_size(agent):
    _write_fake_release(agent)
    release = agent._latest_agent_release()
    assert release["version"] == "9.9.9-docker-runtime"
    assert release["package_exists"] is True
    assert release["package_sha256"] == "deadbeef00"
    assert release["package_size"] == len(b"fake zip bytes")


# --- real script guards (fast, no Docker) -----------------------------------

@pytest.fixture()
def repo(tmp_path):
    dest = tmp_path / "deploy-agent"
    shutil.copytree(ROOT, dest, ignore=IGNORE)
    (tmp_path / "artifacts" / "deploy-agent" / "releases").mkdir(parents=True)
    return dest


def _run_script(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "scripts/build-agent-release.sh"], cwd=repo,
                           capture_output=True, text=True, timeout=30)


def test_build_script_refuses_bad_version(repo):
    (repo / "VERSION.txt").write_text("not-a-valid-version\n", encoding="utf-8")
    result = _run_script(repo)
    assert result.returncode != 0
    assert "VERSION_INVALID" in result.stderr


def test_build_script_refuses_duplicate_version(repo):
    version = (repo / "VERSION.txt").read_text().strip()
    frozen = repo.parent / "artifacts" / "deploy-agent" / "releases" / version
    frozen.mkdir(parents=True)
    (frozen / "agent-release.json").write_text("{}", encoding="utf-8")
    result = _run_script(repo)
    assert result.returncode != 0
    assert "VERSION_ALREADY_RELEASED" in result.stderr


def test_build_script_refuses_missing_source_file(repo):
    (repo / "docker" / "Dockerfile").unlink()
    result = _run_script(repo)
    assert result.returncode != 0
    assert "SOURCE_PREFLIGHT_FAILED" in result.stderr


# --- job state machine (mocked subprocess -- no real Docker build) ---------

def test_run_agent_package_build_success(agent, tmp_path, monkeypatch):
    _write_fake_release(agent)  # what _latest_agent_release() should see after a "successful" run

    def fake_run(cmd, cwd=None, stdout=None, stderr=None, text=None, timeout=None):
        if stdout:
            stdout.write("AGENT RELEASE PASS\nVersion: 9.9.9-docker-runtime\n")
        class R: returncode = 0
        return R()

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    agent._run_agent_package_build()
    job = agent.load_state().get("agent_package_build_job")
    assert job["status"] == "SUCCESS"
    assert job["release"]["version"] == "9.9.9-docker-runtime"
    assert not agent.AGENT_PACKAGE_BUILD_LOCK.locked()


def test_run_agent_package_build_tests_failed(agent, monkeypatch):
    def fake_run(cmd, cwd=None, stdout=None, stderr=None, text=None, timeout=None):
        if stdout:
            stdout.write("running tests...\nERROR: TESTS_FAILED: deploy-agent test suite did not pass\n")
        class R: returncode = 1
        return R()

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    agent._run_agent_package_build()
    job = agent.load_state().get("agent_package_build_job")
    assert job["status"] == "FAILED"
    assert job["error_code"] == "TESTS_FAILED"
    assert not agent.AGENT_PACKAGE_BUILD_LOCK.locked()


def test_run_agent_package_build_duplicate_version_reason(agent, monkeypatch):
    def fake_run(cmd, cwd=None, stdout=None, stderr=None, text=None, timeout=None):
        if stdout:
            stdout.write("ERROR: VERSION_ALREADY_RELEASED: already frozen\n")
        class R: returncode = 1
        return R()

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    agent._run_agent_package_build()
    job = agent.load_state().get("agent_package_build_job")
    assert job["error_code"] == "VERSION_ALREADY_RELEASED"


def test_run_agent_package_build_timeout(agent, monkeypatch):
    def fake_run(cmd, cwd=None, stdout=None, stderr=None, text=None, timeout=None):
        raise agent.subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    agent._run_agent_package_build()
    job = agent.load_state().get("agent_package_build_job")
    assert job["status"] == "FAILED" and job["error_code"] == "BUILD_TIMEOUT"


def test_start_agent_package_build_refuses_concurrent_run(agent):
    assert agent.AGENT_PACKAGE_BUILD_LOCK.acquire(False)
    try:
        with pytest.raises(RuntimeError):
            agent.start_agent_package_build()
    finally:
        agent.AGENT_PACKAGE_BUILD_LOCK.release()


# --- routes ------------------------------------------------------------------

def test_build_route_requires_login(agent):
    # Not in protect()'s login-exempt set (same convention as the existing
    # /api/release-manager/update-agent) -- protect() redirects to /login
    # before this route's own AUTH_REQUIRED check ever runs.
    r = agent.app.test_client().post("/api/release-manager/build-agent-package")
    assert r.status_code == 302


def test_build_route_requires_csrf(agent):
    client = agent.app.test_client()
    with client.session_transaction() as s:
        s["authenticated"] = True
        s["csrf"] = "real-csrf"
    r = client.post("/api/release-manager/build-agent-package", headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403


def test_build_route_starts_job_and_returns_202(agent, monkeypatch):
    monkeypatch.setattr(agent, "start_agent_package_build", lambda: None)
    client = _client(agent)
    r = client.post("/api/release-manager/build-agent-package", headers={"X-CSRF-Token": "test-csrf"})
    assert r.status_code == 202
    assert r.get_json()["ok"] is True


def test_build_route_409_when_already_running(agent, monkeypatch):
    def boom():
        raise RuntimeError("AGENT_PACKAGE_BUILD_ALREADY_RUNNING")
    monkeypatch.setattr(agent, "start_agent_package_build", boom)
    client = _client(agent)
    r = client.post("/api/release-manager/build-agent-package", headers={"X-CSRF-Token": "test-csrf"})
    assert r.status_code == 409


def test_build_route_403_when_build_disabled(agent, monkeypatch):
    monkeypatch.setattr(agent, "MESFLOW_BUILD_ENABLED", False)
    client = _client(agent)
    r = client.post("/api/release-manager/build-agent-package", headers={"X-CSRF-Token": "test-csrf"})
    assert r.status_code == 403


def test_download_route_requires_login(agent):
    r = agent.app.test_client().get("/api/release-manager/agent-package/download")
    assert r.status_code == 302


def test_download_route_404_when_nothing_built(agent):
    client = _client(agent)
    r = client.get("/api/release-manager/agent-package/download")
    assert r.status_code == 404
    assert r.get_json()["error"] == "AGENT_ARTIFACT_NOT_FOUND"


def test_download_route_serves_existing_package(agent):
    _root, package = _write_fake_release(agent)
    client = _client(agent)
    r = client.get("/api/release-manager/agent-package/download")
    assert r.status_code == 200
    assert r.data == b"fake zip bytes"


# --- push (existing route, re-verified with the new build UI alongside) ----

def test_push_to_production_test_still_requires_no_extra_confirm(agent, monkeypatch):
    monkeypatch.setattr(agent, "start_agent_update", lambda role: None)
    client = _client(agent)
    r = client.post("/api/release-manager/update-agent", json={"role": "PRODUCTION_TEST"},
                     headers={"X-CSRF-Token": "test-csrf"})
    assert r.status_code == 202


def test_push_to_production_requires_confirm(agent):
    client = _client(agent)
    r = client.post("/api/release-manager/update-agent", json={"role": "PRODUCTION"},
                     headers={"X-CSRF-Token": "test-csrf"})
    assert r.status_code == 403
    assert r.get_json()["error"] == "PRODUCTION_AGENT_UPDATE_REQUIRES_CONFIRM"


def test_push_reports_rolled_back_status(agent, monkeypatch):
    _write_fake_release(agent)
    monkeypatch.setattr(agent, "_agent_updater_target", lambda role: ("https://bootstrap.example/agent", "tok"))
    monkeypatch.setattr(agent, "_push_agent_update",
                         lambda url, token, zip_path, timeout=300: {"status": "ROLLED_BACK", "reason": "HEALTH_CHECK_FAILED"})
    agent._run_agent_update("PRODUCTION_TEST")
    job = agent.load_state().get("agent_update_job")
    assert job["status"] == "ROLLED_BACK"


def test_push_without_built_package_fails_clearly(agent):
    # No _write_fake_release() call -- nothing built yet.
    agent._run_agent_update("PRODUCTION_TEST")
    job = agent.load_state().get("agent_update_job")
    assert job["status"] == "FAILED"
    assert "AGENT_ARTIFACT_NOT_FOUND" in job["message"]


# --- UI: identity display, empty state, no auto-trigger on refresh ---------

def test_agent_update_tab_shows_latest_package_identity(agent):
    _write_fake_release(agent)
    with patch.object(agent, "_release_contamination", return_value={"contaminated": False}):
        body = _client(agent).get("/releases?tab=agent-update").get_data(as_text=True)
    assert "9.9.9-docker-runtime" in body
    assert "deadbeef00" in body
    assert 'id="buildAgentPackageButton"' in body
    assert "/api/release-manager/agent-package/download" in body


def test_agent_update_tab_shows_empty_state_when_nothing_built(agent):
    with patch.object(agent, "_release_contamination", return_value={"contaminated": False}):
        body = _client(agent).get("/releases?tab=agent-update").get_data(as_text=True)
    assert "Chưa build package nào" in body


def test_agent_update_tab_normal_refresh_never_starts_a_build_or_push(agent, monkeypatch):
    started = {"build": False, "push": False}
    monkeypatch.setattr(agent, "start_agent_package_build", lambda: started.__setitem__("build", True))
    monkeypatch.setattr(agent, "start_agent_update", lambda role: started.__setitem__("push", True))
    client = _client(agent)
    with patch.object(agent, "_release_contamination", return_value={"contaminated": False}):
        client.get("/releases?tab=agent-update")
        client.get("/releases?tab=agent-update")
        client.get("/releases?tab=agent-update")
    assert started == {"build": False, "push": False}


def test_live_status_tracks_agent_package_build_job(agent):
    payload = agent._release_live_status_payload()
    assert "agent_package_build_job" in payload["jobs"]


def test_orphaned_build_job_reconciled_on_startup(agent):
    def mutate(s):
        s["agent_package_build_job"] = {"status": "BUILDING", "message": "..."}
    agent.update_state(mutate)
    agent.reconcile_orphaned_jobs_on_startup()
    job = agent.load_state()["agent_package_build_job"]
    assert job["status"] == "INTERRUPTED"
