"""Regression test for a real bug found while building P4 UI evidence:
/ops?view=backup silently fell back to view=overview because the 'backup'
tab was never added to ops_page()'s `allowed` whitelist -- Playwright
screenshots kept landing on the Overview tab instead of the new Backup /
DB / Storage tab even though the button and section both existed and
worked once actually reached. Never needs docker/Postgres -- this is a
pure template-rendering check.
"""
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def agent(tmp_path):
    import os
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["WORKSHOP_AGENT_HOME"] = str(tmp_path)
    os.environ.setdefault("MESFLOW_AGENT_RUNTIME", "linux")
    os.environ.setdefault("MESFLOW_AGENT_ADMIN_PASSWORD", "TestPw1234567")
    os.environ["SERVER_ROLE"] = "PRODUCTION_TEST"
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("agent_p4_ops_page", root / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_p4_ops_page"] = mod
    spec.loader.exec_module(mod)
    mod.app.testing = True
    return mod


def _client(agent):
    client = agent.app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "test-csrf"
    return client


def test_ops_view_backup_is_accepted_not_silently_downgraded(agent):
    client = _client(agent)
    r = client.get("/ops?view=backup")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # the JS bootstrap constant must actually say 'backup', not silently
    # fall back to 'overview' the way it did before this was fixed.
    assert "const initialView=\"backup\"" in html
    assert 'id="backup" class="tab' in html


def test_ops_view_unknown_falls_back_to_overview(agent):
    client = _client(agent)
    r = client.get("/ops?view=not-a-real-tab")
    assert r.status_code == 200
    assert "const initialView=\"overview\"" in r.get_data(as_text=True)


def test_ops_view_backup_requires_login(agent):
    client = agent.app.test_client()
    r = client.get("/ops?view=backup")
    assert r.status_code in (302, 401)
