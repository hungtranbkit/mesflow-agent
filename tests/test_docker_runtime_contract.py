import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_guard_is_present():
    text = (ROOT / "agent.py").read_text(encoding="utf-8")
    # The exact version changes every release; what must hold is that
    # AGENT_VERSION is tagged "-docker-runtime" (the marker this whole
    # test module is about), not any specific historical value.
    assert re.search(r'AGENT_VERSION="\d+\.\d+\.\d+-docker-runtime"', text), \
        "AGENT_VERSION must be a X.Y.Z-docker-runtime version string"
    assert "MESFLOW_AGENT_DEPLOY_ENABLED" in text
    assert "if not dry_run and not DEPLOY_ENABLED" in text
    assert "HOST_OPS_ENABLED" in text
    assert "SERVER_ROLE" in text
    assert "MESFLOW_BUILD_ENABLED" in text


def test_windows_container_is_safe_mode():
    text = (ROOT / "docker" / "compose.windows.yml").read_text(encoding="utf-8")
    assert 'MESFLOW_AGENT_DEPLOY_ENABLED: "0"' in text
    assert 'MESFLOW_AGENT_HOST_OPS_ENABLED: "0"' in text
    assert "host.docker.internal:8080" in text
    assert "/var/run/docker.sock:/var/run/docker.sock" in text


def test_linux_container_preserves_target_absolute_path():
    text = (ROOT / "docker" / "compose.linux.yml").read_text(encoding="utf-8")
    assert 'MESFLOW_AGENT_DEPLOY_ENABLED: "1"' in text
    assert "- /opt/mesflow:/opt/mesflow" in text
    assert "mesflow-edge" in text


def test_gateway_and_database_are_not_mounted_into_agent():
    combined = "\n".join(
        (ROOT / "docker" / f).read_text(encoding="utf-8")
        for f in ("compose.windows.yml", "compose.linux.yml")
    )
    assert "/opt/mesflow-gateway" not in combined
    assert "postgres-v65:" not in combined
    assert "/opt/mesflow/certs" not in combined
