from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MESFLOW = ROOT.parent / "mesflow"


def test_boot_recovery_owns_only_application_services():
    installer = (ROOT / "install_agent.sh").read_text(encoding="utf-8")
    command = 'up -d --no-build postgres mesflow'
    assert command in installer
    assert 'compose.yml" up -d;' not in installer


def test_application_scripts_never_manage_gateway():
    for name in ("deploy.sh", "reset-admin-password.sh", "restore.sh"):
        text = (MESFLOW / "scripts" / name).read_text(encoding="utf-8")
        lifecycle = [line for line in text.splitlines() if "docker compose" in line and any(word in line for word in (" up ", " stop ", " down ", " rm "))]
        assert all("nginx" not in line for line in lifecycle), (name, lifecycle)
    assert "--remove-orphans" not in (MESFLOW / "scripts" / "deploy.sh").read_text(encoding="utf-8")


def test_application_compose_excludes_gateway_and_preserves_postgres_path():
    compose = (MESFLOW / "compose.yml").read_text(encoding="utf-8")
    assert "\n  nginx:" not in compose
    assert "./runtime/postgres-v65:/var/lib/postgresql/data" in compose
    assert "external: true" in compose


def test_production_mutation_guard_remains_active():
    agent = (ROOT / "agent.py").read_text(encoding="utf-8")
    assert "_dangerous_command" in agent
    assert r"docker\s+volume\s+(rm|prune)" in agent
    assert r"\b(drop\s+database|drop\s+table|truncate\s+table)\b" in agent
