import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_version_metadata_is_synchronized():
    # Assert cross-file consistency, not a frozen historical version number
    # (that goes stale on every release — see AGENTS.md: every modified
    # deployable must use a new version, synchronized across VERSION.txt /
    # __version__ / release.json / the compose.yml image tag).
    version = (ROOT / "VERSION.txt").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+-docker-runtime", version), version
    assert f'AGENT_VERSION="{version}"' in (ROOT / "agent.py").read_text()
    image_tag = version[: -len("-docker-runtime")]
    assert f"mesflow-deploy-agent:{image_tag}" in (ROOT / "docker/compose.linux.yml").read_text()


def test_qa_upgrade_preserves_runtime_state():
    source = (ROOT / "agent.py").read_text()
    block = source[source.index("def _preserve_qa_data"):source.index("def run_qa_job")]
    assert '("config.json","reports","runtime")' in block


def test_installer_package_excludes_local_runtime_and_secrets():
    source = (ROOT / "package_installer.sh").read_text()
    assert 'docker/runtime' in source
    assert "-name '.env'" in source
    assert "-name '.env.*'" in source
