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
    # The legacy full-source QA deploy path (and its old_current/new_current
    # "_preserve_qa_data" copy step) was removed alongside the legacy
    # source-ZIP UI -- see reports/RELEASE_DEPLOY_UX_CLEANUP.md. The current
    # Docker-image-based QA deploy keeps runtime/config/reports via a
    # persistent bind mount (QA_HOME/runtime) plus a one-time migration
    # helper for pre-existing installs, not a copy-on-upgrade step.
    source = (ROOT / "agent.py").read_text()
    assert "def _preserve_qa_data" not in source
    block = source[source.index("def _migrate_qa_runtime_data_if_needed"):source.index("def qa_status")]
    assert 'QA_HOME/"runtime"' in block or "QA_HOME/'runtime'" in block


def test_installer_package_excludes_local_runtime_and_secrets():
    installer = (ROOT / "package_installer.sh").read_text()
    packager = (ROOT / "tools/build_source_package.py").read_text()
    # The installer delegates to the verified source packager rather than
    # duplicating a fragile list of post-copy rm/find commands.
    assert "tools/build_source_package.py" in installer
    assert '"runtime"' in packager
    assert '".env"' in packager
    assert 'path.name.startswith(".env.")' in packager
