import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_agent_version_and_promotion_fields_are_visible():
    # Assert internal consistency (VERSION.txt matches agent.py's
    # AGENT_VERSION), not one frozen historical version — this must keep
    # passing across every release, not just the one it was written against.
    version = (ROOT / "VERSION.txt").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+-docker-runtime", version), version
    source = (ROOT / "agent.py").read_text()
    # templates/index.html was removed and replaced by the tabbed
    # templates/release/*.html views -- see reports/RELEASE_DEPLOY_UX_CLEANUP.md.
    # The Release Deploy tab is where artifact/evidence metadata now lives.
    template = (ROOT / "templates/release/_deploy_tab.html").read_text()
    assert f'AGENT_VERSION="{version}"' in source
    assert 'promotion=load_json(x/"PROMOTION.json",{})' in source
    assert "ZIP SHA" in template
    assert "Local QA" in template
    assert "Source commit" in template


def test_docker_liveness_does_not_call_dependency_health():
    source=(ROOT / "agent.py").read_text()
    dockerfile=(ROOT / "docker/Dockerfile").read_text()
    assert '@app.get("/live")' in source
    assert "/agent/live" in dockerfile
    assert "/agent/health" not in dockerfile


def test_promotion_template_contains_no_credential_and_starts_unverified():
    data=json.loads((ROOT.parent / "artifacts/releases/TEMPLATE/PROMOTION.json").read_text())
    assert data["local_verification"] == "NOT VERIFIED"
    assert data["production_test_status"] == "NOT VERIFIED"
    assert not any("password" in key.lower() or "secret" in key.lower() for key in data)
