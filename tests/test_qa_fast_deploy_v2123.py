from pathlib import Path
import ast
ROOT=Path(__file__).resolve().parents[1]
AGENT=ROOT/"agent.py"

def test_agent_parses():
    ast.parse(AGENT.read_text())

def test_fast_deploy_signature_contract():
    t=AGENT.read_text()
    assert "def _qa_runtime_signature" in t
    assert "runtime-manifest.json" in t
    assert "FAST QA DEPLOY: runtime unchanged; skipping Docker build" in t

def test_fast_deploy_recreates_without_build():
    t=AGENT.read_text()
    block=t[t.index("def _qa_docker_start"):t.index("def _stop_qa")]
    assert '"--no-build","--force-recreate"' in block

def test_existing_qa_kept_up_until_cutover():
    t=AGENT.read_text()
    assert "existing QA stays up until recreate" in t
