import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_agent_version_and_promotion_fields_are_visible():
    assert (ROOT / "VERSION.txt").read_text().strip() == "2.15.5-docker-runtime"
    source = (ROOT / "agent.py").read_text()
    template = (ROOT / "templates/index.html").read_text()
    assert 'AGENT_VERSION="2.15.5-docker-runtime"' in source
    assert 'promotion=load_json(x/"PROMOTION.json",{})' in source
    assert "SHA256:" in template
    assert "Local verification:" in template
    assert "Source commit:" in template


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
