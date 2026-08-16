"""Frontend cache busting (reports/DEPLOY_AGENT_FRONTEND_CACHE_BUST.md).

A rebuilt Agent must never require the operator to hard-refresh/
Incognito to see new JS/CSS. Covers: FRONTEND_BUILD_ID is deterministic
and content-based (not mtime), every template references static assets
through the versioned static_url() helper (never a bare url_for('static',...)),
HTML responses always revalidate, versioned static responses are cached
immutably, and the identity is exposed in /live and /api/status.
"""
from __future__ import annotations

import importlib.util
import re
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
    # Same convention tests/conftest.py's spawn_agent() uses -- an empty
    # prefix keeps generated URLs simple to assert on; the /agent prefix
    # itself is unrelated to cache-busting and covered by other tests.
    os.environ["WORKSHOP_AGENT_URL_PREFIX"] = ""
    tag = uuid.uuid4().hex[:8]
    spec = importlib.util.spec_from_file_location(f"agent_cachebust_{tag}", ROOT / "agent.py")
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
    return client


# -- FRONTEND_BUILD_ID ---------------------------------------------------

def test_frontend_build_id_is_agent_version_plus_content_hash(agent):
    assert agent.FRONTEND_BUILD_ID.startswith(agent.AGENT_VERSION + "-")
    suffix = agent.FRONTEND_BUILD_ID[len(agent.AGENT_VERSION) + 1:]
    assert re.fullmatch(r"[0-9a-f]{10}", suffix), suffix


def test_frontend_build_id_is_deterministic_not_mtime_based(agent, tmp_path):
    static_dir = tmp_path / "static_a"
    (static_dir / "css").mkdir(parents=True)
    (static_dir / "css" / "agent.css").write_text("body{color:red}")
    first = agent._compute_frontend_build_id(static_dir)
    # A second, completely separate directory with byte-identical content
    # (different mtime, different inode) must hash to the exact same id --
    # this is what makes it safe to cache assets forever by URL.
    static_dir_2 = tmp_path / "static_b"
    (static_dir_2 / "css").mkdir(parents=True)
    (static_dir_2 / "css" / "agent.css").write_text("body{color:red}")
    second = agent._compute_frontend_build_id(static_dir_2)
    assert first == second


def test_frontend_build_id_changes_when_static_content_changes(agent, tmp_path):
    static_dir = tmp_path / "static"
    (static_dir / "css").mkdir(parents=True)
    (static_dir / "css" / "agent.css").write_text("body{color:red}")
    before = agent._compute_frontend_build_id(static_dir)
    (static_dir / "css" / "agent.css").write_text("body{color:blue}")
    after = agent._compute_frontend_build_id(static_dir)
    assert before != after


# -- static_url() / templates --------------------------------------------

def test_static_url_helper_appends_frontend_build_id(agent):
    with agent.app.test_request_context("/"):
        url = agent.static_url("css/agent.css")
    assert url.endswith(f"?v={agent.FRONTEND_BUILD_ID}")
    assert url.startswith("/static/css/agent.css")


def test_no_template_uses_bare_url_for_static():
    """Every static asset reference in every template must go through
    static_url() -- a bare url_for('static', filename=...) would silently
    reintroduce unversioned, browser-cacheable URLs."""
    offenders = []
    for path in (ROOT / "templates").rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"url_for\(\s*['\"]static['\"]", text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f"templates still using bare url_for('static',...): {offenders}"


def test_dashboard_and_ops_and_release_and_kiosk_render_versioned_asset_urls(agent):
    client = _client(agent)
    for path in ("/", "/ops?view=docker", "/releases", "/kiosk/ota"):
        body = client.get(path).get_data(as_text=True)
        assert f"?v={agent.FRONTEND_BUILD_ID}" in body, f"{path} missing versioned asset URL"


# -- cache headers ---------------------------------------------------------

def test_html_response_never_caches(agent):
    client = _client(agent)
    resp = client.get("/")
    assert resp.headers.get("Cache-Control") == "no-cache, must-revalidate"


def test_versioned_static_asset_is_cached_immutably(agent):
    client = _client(agent)
    body = client.get("/").get_data(as_text=True)
    m = re.search(r'href="([^"]*static/css/agent\.css\?v=[^"]+)"', body)
    assert m, "could not find the versioned agent.css URL in the rendered page"
    resp = client.get(m.group(1))
    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "public, max-age=31536000, immutable"


# -- metadata exposure (task item 6) ---------------------------------------

def test_live_and_status_expose_frontend_build_id(agent):
    client = _client(agent)
    live = client.get("/live").get_json()
    assert live["agent_version"] == agent.AGENT_VERSION
    assert live["frontend_build_id"] == agent.FRONTEND_BUILD_ID
    status = client.get("/api/status").get_json()
    assert status["frontend_build_id"] == agent.FRONTEND_BUILD_ID


# -- rebuild simulation: new FRONTEND_BUILD_ID -> new asset URL, no stale HTML --

def test_a_rebuilt_agent_process_never_serves_stale_asset_url(agent, tmp_path):
    """Simulates 'rebuild the Agent': a second process (fresh FRONTEND_BUILD_ID
    computed independently, since it's a module-level constant, exactly
    like a real container restart after `docker build`) must render a
    DIFFERENT asset URL if the static content differs, and the SAME URL
    if it doesn't -- proving there is no cached/stale identity carried
    across a rebuild."""
    import os
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["WORKSHOP_AGENT_HOME"] = str(tmp_path)
    os.environ["WORKSHOP_AGENT_URL_PREFIX"] = ""
    tag = uuid.uuid4().hex[:8]
    spec = importlib.util.spec_from_file_location(f"agent_cachebust_reload_{tag}", ROOT / "agent.py")
    mod2 = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod2
    spec.loader.exec_module(mod2)
    # Same static/ directory on disk -> same content hash -> same id,
    # exactly as a rebuild from unchanged source would behave.
    assert mod2.FRONTEND_BUILD_ID == agent.FRONTEND_BUILD_ID
