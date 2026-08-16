"""scripts/bump-version.sh -- Deploy Agent's OWN version bump, mirroring
mesflow/scripts/bump-version.sh's pattern (see tests/test_version_bump.py
for that one, which bumps the MESFlow *application* version through
agent._run_version_bump and is a separate, pre-existing feature).

This script is a standalone CLI tool, not wired into any Deploy Agent
route/UI button -- invoked directly via subprocess against a seeded copy
of the 7 declared-version files, matching tests/test_version_bump.py's
own isolation style (never runs against the real repo's VERSION.txt).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REAL_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bump-version.sh"
FILES = {
    "VERSION.txt": "{v}",
    "agent.py": 'AGENT_VERSION="{v}"\nOTHER_LINE=1\n',
    "docker/Dockerfile": "FROM python:3.13-alpine3.22\n\nARG AGENT_VERSION={v}\nLABEL x=y\n",
    "docker/compose.linux.yml": "services:\n  mesflow-deploy-agent:\n    image: ${{AGENT_IMAGE:-mesflow-deploy-agent:{core}}}\n",
    "docker/compose.windows.yml": "services:\n  mesflow-deploy-agent:\n    image: mesflow-deploy-agent:{core}\n",
    "README.md": "# MESFlow Deploy Agent\n\nCurrent version: **{v}**\n",
    "docs/DEPLOY_DOCKER.md": "# Docker\n\nVersion: **{v}**\n",
}


def _seed(root: Path, version: str) -> None:
    core = version.removesuffix("-docker-runtime")
    (root / "docker").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    for rel, template in FILES.items():
        (root / rel).write_text(template.format(v=version, core=core))
    script = root / "scripts" / "bump-version.sh"
    script.write_bytes(REAL_SCRIPT.read_bytes())
    script.chmod(0o755)


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "scripts/bump-version.sh", *args], cwd=root,
                           capture_output=True, text=True, timeout=15)


@pytest.fixture()
def repo(tmp_path):
    assert REAL_SCRIPT.is_file(), f"real script missing: {REAL_SCRIPT}"
    root = tmp_path / "deploy-agent"
    _seed(root, "1.0.0-docker-runtime")
    (tmp_path / "artifacts" / "deploy-agent" / "releases").mkdir(parents=True)
    return root


def test_default_bump_increments_last_segment_everywhere(repo):
    result = _run(repo, )
    assert result.returncode == 0, result.stderr
    assert "New version: 1.0.1-docker-runtime" in result.stdout
    assert (repo / "VERSION.txt").read_text().strip() == "1.0.1-docker-runtime"
    assert 'AGENT_VERSION="1.0.1-docker-runtime"' in (repo / "agent.py").read_text()
    assert "ARG AGENT_VERSION=1.0.1-docker-runtime" in (repo / "docker/Dockerfile").read_text()
    assert "mesflow-deploy-agent:1.0.1" in (repo / "docker/compose.linux.yml").read_text()
    assert "mesflow-deploy-agent:1.0.1" in (repo / "docker/compose.windows.yml").read_text()
    assert "Current version: **1.0.1-docker-runtime**" in (repo / "README.md").read_text()
    assert "Version: **1.0.1-docker-runtime**" in (repo / "docs/DEPLOY_DOCKER.md").read_text()
    # Untouched lines in agent.py must survive the sed untouched.
    assert "OTHER_LINE=1" in (repo / "agent.py").read_text()


def test_explicit_target_without_suffix(repo):
    result = _run(repo, "1.5.0")
    assert result.returncode == 0, result.stderr
    assert (repo / "VERSION.txt").read_text().strip() == "1.5.0-docker-runtime"


def test_explicit_target_with_to_flag_and_suffix(repo):
    result = _run(repo, "--to", "1.5.0-docker-runtime")
    assert result.returncode == 0, result.stderr
    assert (repo / "VERSION.txt").read_text().strip() == "1.5.0-docker-runtime"


def test_rejects_same_version(repo):
    result = _run(repo, "1.0.0")
    assert result.returncode != 0
    assert "VERSION_UNCHANGED" in result.stderr
    assert (repo / "VERSION.txt").read_text().strip() == "1.0.0-docker-runtime"


def test_rejects_non_newer_version(repo):
    result = _run(repo, "0.9.0")
    assert result.returncode != 0
    assert "VERSION_NOT_NEWER" in result.stderr
    assert (repo / "VERSION.txt").read_text().strip() == "1.0.0-docker-runtime"


def test_rejects_already_released_target(repo):
    frozen = repo.parent / "artifacts" / "deploy-agent" / "releases" / "1.2.0-docker-runtime"
    frozen.mkdir(parents=True)
    (frozen / "agent-release.json").write_text("{}")
    result = _run(repo, "1.2.0")
    assert result.returncode != 0
    assert "VERSION_ALREADY_RELEASED" in result.stderr
    assert (repo / "VERSION.txt").read_text().strip() == "1.0.0-docker-runtime"


def test_rejects_invalid_current_version_format(repo):
    (repo / "VERSION.txt").write_text("not-a-version")
    result = _run(repo)
    assert result.returncode != 0
    assert "VERSION_INVALID" in result.stderr


def test_missing_file_fails_before_writing_anything(repo):
    (repo / "README.md").unlink()
    result = _run(repo)
    assert result.returncode != 0
    assert "Expected file missing: README.md" in result.stderr
    assert (repo / "VERSION.txt").read_text().strip() == "1.0.0-docker-runtime"
