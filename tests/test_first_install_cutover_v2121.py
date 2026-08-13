import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent.py"

def test_agent_parses():
    ast.parse(AGENT.read_text(encoding="utf-8"))

def test_first_install_skips_stop_without_compose():
    text = AGENT.read_text(encoding="utf-8")
    assert 'if (TARGET_HOME/"compose.yml").is_file():' in text
    assert 'FIRST INSTALL: no existing application compose.yml; skip application stop' in text

def test_first_install_rollback_skips_missing_compose_warning():
    text = AGENT.read_text(encoding="utf-8")
    assert 'FIRST INSTALL ROLLBACK: no application compose.yml to stop' in text
