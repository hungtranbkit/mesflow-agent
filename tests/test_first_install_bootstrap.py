import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent.py"

def test_first_install_backup_contract_present():
    text = AGENT.read_text(encoding="utf-8")
    assert 'FIRST_INSTALL_BACKUP_MARKER=".mesflow_first_install_baseline"' in text
    assert "application compose.yml not present yet" in text
    assert '(backup/FIRST_INSTALL_BACKUP_MARKER).is_file()' in text
    assert '"compose.bootstrap.yml"' in text

def test_agent_python_parses():
    ast.parse(AGENT.read_text(encoding="utf-8"))
