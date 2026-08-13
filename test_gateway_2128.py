from pathlib import Path
R=Path(__file__).resolve().parents[2]
A=R/"payload/mesflow-deploy-agent"

def test_version_sync():
    assert (A/"VERSION.txt").read_text().strip()=="2.12.8-docker-runtime"
    assert 'AGENT_VERSION="2.12.8-docker-runtime"' in (A/"agent.py").read_text()
    assert 'image: mesflow-deploy-agent:2.12.8' in (A/"docker/compose.linux.yml").read_text()

def test_updater_uses_sni_resolve():
    s=(R/"update-gateway.sh").read_text()
    assert '--resolve "deploy.mesflow.net:443:127.0.0.1"' in s
    assert 'https://deploy.mesflow.net/agent/health' in s
    assert "-H 'Host: deploy.mesflow.net' https://127.0.0.1/agent/" not in s

def test_updater_checks_json_health_and_redirect():
    s=(R/"update-gateway.sh").read_text()
    assert 'if not data.get("ok")' in s
    assert 'Root deploy.mesflow.net không redirect' in s

def test_installer_uses_same_gateway_health():
    s=(R/"install.sh").read_text()
    assert '--resolve "deploy.mesflow.net:443:127.0.0.1"' in s
    assert 'https://deploy.mesflow.net/agent/health' in s

def test_diagnostics_script_exists():
    s=(R/"diagnose-gateway.sh").read_text()
    assert "--resolve deploy.mesflow.net:443:127.0.0.1" in s
    assert "nginx -T" in s
