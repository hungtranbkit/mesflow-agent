from pathlib import Path
import ast
ROOT=Path(__file__).resolve().parents[1]
AGENT=ROOT/"agent.py"

def test_agent_parses():
    ast.parse(AGENT.read_text(encoding="utf-8"))

def test_docker_start_does_not_return_host_pid():
    t=AGENT.read_text(encoding="utf-8")
    block=t[t.index("def _qa_docker_start"):t.index("def _stop_qa")]
    assert "return 0" in block
    assert '".State.Pid"' not in block

def test_wait_qa_uses_container_state_for_docker():
    t=AGENT.read_text(encoding="utf-8")
    block=t[t.index("def _wait_qa"):t.index("def _password_valid")]
    assert 'RUNTIME_MODE=="docker-linux"' in block
    assert "_qa_service_state()" in block
    assert "QA Center container exited" in block
    assert "_qa_docker_log_tail" in block
