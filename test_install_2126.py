from pathlib import Path
R=Path(__file__).resolve().parents[2]
A=R/"payload/mesflow-deploy-agent"

def test_versions_synced():
    assert (A/"VERSION.txt").read_text().strip()=="2.12.6-docker-runtime"
    assert 'AGENT_VERSION="2.12.6-docker-runtime"' in (A/"agent.py").read_text()
    assert 'image: mesflow-deploy-agent:2.12.6' in (A/"docker/compose.linux.yml").read_text()
    assert 'image: mesflow-deploy-agent:2.12.6' in (A/"docker/compose.windows.yml").read_text()

def test_linux_port_single_source():
    assert '127.0.0.1:8090:8090' not in (A/"docker/compose.linux.yml").read_text()
    i=(R/"install.sh").read_text()
    assert '${AGENT_BIND_IP}:${AGENT_PORT}:8090' in i

def test_no_old_image_hardcode():
    i=(R/"install.sh").read_text()
    assert 'AGENT_IMAGE="mesflow-deploy-agent:${AGENT_IMAGE_TAG}"' in i
    assert 'mesflow-deploy-agent:2.12.4' not in i
    assert '"$AGENT_IMAGE"' in i

def test_legacy_and_port_preflight():
    i=(R/"install.sh").read_text()
    assert 'systemctl stop mesflow-deploy-agent.service' in i
    assert 'docker rm -f mesflow-deploy-agent' in i
    assert 'ss -lntp "sport = :${AGENT_PORT}"' in i

def test_recreate_and_binding_verification():
    i=(R/"install.sh").read_text()
    assert '--force-recreate' in i
    assert '.HostConfig.PortBindings' in i
    assert 'Container Agent không publish' in i
