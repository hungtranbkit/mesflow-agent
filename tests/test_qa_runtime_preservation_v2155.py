from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_version_metadata_is_synchronized():
    assert (ROOT / "VERSION.txt").read_text().strip() == "2.15.5-docker-runtime"
    assert 'AGENT_VERSION="2.15.5-docker-runtime"' in (ROOT / "agent.py").read_text()
    assert "mesflow-deploy-agent:2.15.5" in (ROOT / "docker/compose.linux.yml").read_text()


def test_qa_upgrade_preserves_runtime_state():
    source = (ROOT / "agent.py").read_text()
    block = source[source.index("def _preserve_qa_data"):source.index("def run_qa_job")]
    assert '("config.json","reports","runtime")' in block


def test_installer_package_excludes_local_runtime_and_secrets():
    source = (ROOT / "package_installer.sh").read_text()
    assert 'docker/runtime' in source
    assert "-name '.env'" in source
    assert "-name '.env.*'" in source
