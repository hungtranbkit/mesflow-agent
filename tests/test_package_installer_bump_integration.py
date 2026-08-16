"""package_installer.sh now bumps the Deploy Agent's own version by
default before packaging (scripts/bump-version.sh integration), so a
release build is a single command instead of two manual steps. Runs
against an isolated copy of the real repo tree in tmp_path -- never
against the real workspace VERSION.txt.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IGNORE = shutil.ignore_patterns(".venv", "__pycache__", ".git", "*.pyc", "node_modules", "runtime", "artifacts")


@pytest.fixture()
def repo(tmp_path):
    dest = tmp_path / "deploy-agent"
    # "runtime" is excluded above: docker/runtime/ can hold root-owned
    # bind-mount data from a real local Docker Postgres volume on this
    # host, which a plain user-owned copytree can't read -- and it has
    # no business in a source copy anyway (AGENTS.md RULE 8).
    shutil.copytree(ROOT, dest, ignore=IGNORE)
    (tmp_path / "artifacts" / "deploy-agent" / "releases").mkdir(parents=True)
    return dest


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "package_installer.sh", *args], cwd=repo,
                           capture_output=True, text=True, timeout=60)


def test_default_bumps_version_then_packages(repo):
    before = (repo / "VERSION.txt").read_text().strip()
    result = _run(repo)
    assert result.returncode == 0, result.stderr
    after = (repo / "VERSION.txt").read_text().strip()
    assert after != before
    assert "VERSION BUMP PASS" in result.stdout
    out_path = Path(result.stdout.strip().splitlines()[-1])
    assert out_path.is_file()
    assert after.removesuffix("-docker-runtime") in out_path.name


def test_no_bump_packages_current_version_unchanged(repo):
    before = (repo / "VERSION.txt").read_text().strip()
    result = _run(repo, "--no-bump")
    assert result.returncode == 0, result.stderr
    assert (repo / "VERSION.txt").read_text().strip() == before
    assert "VERSION BUMP PASS" not in result.stdout
    out_path = Path(result.stdout.strip().splitlines()[-1])
    assert before.removesuffix("-docker-runtime") in out_path.name


def test_to_explicit_version_then_packages(repo):
    result = _run(repo, "--to", "9.9.9")
    assert result.returncode == 0, result.stderr
    assert (repo / "VERSION.txt").read_text().strip() == "9.9.9-docker-runtime"


def test_out_flag_controls_output_path(repo, tmp_path):
    custom = tmp_path / "custom.zip"
    result = _run(repo, "--no-bump", "--out", str(custom))
    assert result.returncode == 0, result.stderr
    assert custom.is_file()
    assert result.stdout.strip().splitlines()[-1] == str(custom)


def test_no_bump_and_to_are_mutually_exclusive(repo):
    result = _run(repo, "--no-bump", "--to", "1.2.3")
    assert result.returncode != 0
    assert "mutually exclusive" in result.stderr


def test_unknown_flag_rejected(repo):
    result = _run(repo, "--bogus")
    assert result.returncode != 0
    assert "Unknown argument" in result.stderr
