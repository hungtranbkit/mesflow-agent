import ast
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
AGENT=ROOT/"agent.py"
COMPOSE=ROOT/"docker"/"compose.linux.yml"

def test_agent_parses():
    ast.parse(AGENT.read_text(encoding="utf-8"))

def test_qa_docker_contract():
    t=AGENT.read_text(encoding="utf-8")
    assert 'RUNTIME_MODE=="docker-linux"' in t
    assert 'mesflow-qa-center' in t
    assert 'docker","compose' in t
    c=COMPOSE.read_text(encoding="utf-8")
    assert '/opt/mesflow-qa-center:/opt/mesflow-qa-center' in c
    assert 'MESFLOW_QA_URL: http://mesflow-qa-center:8095' in c
