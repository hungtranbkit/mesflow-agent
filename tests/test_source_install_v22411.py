import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_source_installer_targets_workspace_deploy_agent_only():
    s = (ROOT / 'install.sh').read_text()
    assert 'workspace/mesflow' in s
    assert 'WORKSPACE_ROOT/deploy-agent' in s
    assert 'REFUSE unsafe target' in s
    assert 'CHỈ COPY SOURCE' in s
    assert 'docker compose up/restart' in s


def test_source_installer_preserves_local_config_and_runtime():
    s = (ROOT / 'install.sh').read_text()
    for token in ["--exclude '.env'", "--exclude 'docker/runtime/'", "--exclude 'runtime/'", "--exclude 'artifacts/'"]:
        assert token in s


def test_version_bumped_for_source_installer_release():
    v=(ROOT/"VERSION.txt").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+-docker-runtime", v)
    assert f'AGENT_VERSION="{v}"' in (ROOT / "agent.py").read_text()
