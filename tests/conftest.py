"""Shared test infrastructure for P5 multi-server tests: spawns a REAL
second `agent.py` process (real HTTP, real waitress, real login/CSRF) to
act as a Remote Agent -- Flask's in-process test client cannot be reached
by `agent.py`'s own urllib-based `_remote_agent_client`, so genuine
multi-server tests need a genuine second listening process.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PASSWORD = "TestPw1234567"


def _wait_ready(base_url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/login", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.3)
    return False


def spawn_agent(tmp_path: Path, port: int, server_role: str = "TEST", extra_env: dict | None = None,
                 admin_password: str = ADMIN_PASSWORD):
    """Starts a real `python agent.py` process bound to 127.0.0.1:<port>,
    isolated WORKSHOP_AGENT_HOME. Returns (process, base_url). Caller must
    call stop_agent() in a finally block."""
    home = tmp_path / f"agent_home_{port}"
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "data" / "logs").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "WORKSHOP_AGENT_HOME": str(home),
        "WORKSHOP_AGENT_HOST": "127.0.0.1",
        "WORKSHOP_AGENT_PORT": str(port),
        "WORKSHOP_AGENT_URL_PREFIX": "",
        "MESFLOW_AGENT_RUNTIME": "linux",
        "MESFLOW_AGENT_ADMIN_PASSWORD": admin_password,
        "SERVER_ROLE": server_role,
        "MESFLOW_BUILD_ENABLED": "0",
        "ESP_BUILD_ENABLED": "0",
        # QA_URL defaults to the docker-internal hostname "mesflow-qa-center",
        # which does not resolve on a bare test host -- qa_status()'s 3s
        # timeout then taxes EVERY dashboard render and /api/status call.
        # Point both MES/QA at a local port nothing listens on: ECONNREFUSED
        # is near-instant (no DNS lookup), unlike an unresolvable hostname.
        "MESFLOW_QA_URL": "http://127.0.0.1:1",
        "WORKSHOP_MES_URL": "http://127.0.0.1:1",
        # CRITICAL SAFETY DEFAULT: agent.py's PostgresTarget defaults its
        # container name to "mesflow-postgres" -- the REAL production
        # container's name -- whenever MESFLOW_POSTGRES_CONTAINER is unset.
        # A spawned test process that inherits this host's environment
        # (which has real docker access) would then run a REAL pg_dump
        # against REAL production data on any backup-creating call. This
        # was caught live during P5 test development (see P5 report,
        # "Safety incident" section) -- a test's own POST to
        # /api/servers/<id>/backups produced a real 1.3MB dump of the real
        # database before this default was added. Every spawned test agent
        # MUST point at a name that provably does not exist unless a test
        # explicitly opts into a real throwaway container via extra_env.
        "MESFLOW_POSTGRES_CONTAINER": "p5-test-no-postgres-configured",
        # A module-scoped test remote serves MANY sequential requests across
        # a whole test file; observed waitress's default 8-thread pool
        # getting exhausted by earlier timed-out/abandoned client
        # connections, wedging later requests indefinitely. More headroom
        # here only affects spawned test processes (see agent.py's
        # WORKSHOP_AGENT_THREADS, default unchanged at 8).
        "WORKSHOP_AGENT_THREADS": "32",
    })
    env.pop("MESFLOW_TELEGRAM_BOT_TOKEN", None)
    env.pop("MESFLOW_SMTP_HOST", None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen([sys.executable, "agent.py"], cwd=str(ROOT), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base_url = f"http://127.0.0.1:{port}"
    if not _wait_ready(base_url):
        out = ""
        try:
            proc.terminate()
            out = proc.communicate(timeout=5)[0] or ""
        except Exception:
            pass
        pytest.fail(f"remote agent process on port {port} never became ready.\n{out[-3000:]}")
    return proc, base_url


def stop_agent(proc: subprocess.Popen):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
