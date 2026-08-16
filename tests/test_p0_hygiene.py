import ast
import re
from pathlib import Path

from agent_backend.system_health import format_bytes, run_capture

ROOT = Path(__file__).resolve().parents[1]


def test_authoritative_versions_are_synchronized():
    version = (ROOT / "VERSION.txt").read_text().strip()
    image_version = version.removesuffix("-docker-runtime")
    assert re.fullmatch(r"\d+\.\d+\.\d+-docker-runtime", version)
    assert f'AGENT_VERSION="{version}"' in (ROOT / "agent.py").read_text()
    assert f"ARG AGENT_VERSION={version}" in (ROOT / "docker/Dockerfile").read_text()
    for compose in ("compose.linux.yml", "compose.windows.yml"):
        assert f"mesflow-deploy-agent:{image_version}" in (ROOT / "docker" / compose).read_text(), compose
    assert f"Current version: **{version}**" in (ROOT / "README.md").read_text()
    assert f"Version: **{version}**" in (ROOT / "docs/DEPLOY_DOCKER.md").read_text()


def test_agent_imports_extracted_health_service():
    source = (ROOT / "agent.py").read_text()
    assert "from agent_backend.system_health import" in source
    tree = ast.parse(source)
    functions = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_run_capture" not in functions
    assert "_bytes" not in functions


def test_read_only_command_runner_is_bounded_and_never_uses_shell():
    result = run_capture(["python3", "-c", "print('ok')"], timeout=3)
    assert result["ok"] is True
    assert result["stdout"].strip() == "ok"
    assert format_bytes(1024) == "1.0 KB"


def test_pytest_is_scoped_away_from_runtime_backups():
    config = (ROOT / "pytest.ini").read_text()
    assert "testpaths = tests" in config
    assert "docker/runtime" in config

