from pathlib import Path
R=Path(__file__).resolve().parents[2]
A=R/"payload/mesflow-deploy-agent"
S=(A/"agent.py").read_text()
T=(A/"templates/index.html").read_text()

def test_version():
    assert (A/"VERSION.txt").read_text().strip()=="2.12.9-docker-runtime"
    assert 'AGENT_VERSION="2.12.9-docker-runtime"' in S
    assert 'image: mesflow-deploy-agent:2.12.9' in (A/"docker/compose.linux.yml").read_text()

def test_button_and_route():
    assert '@app.post("/tutorial/rebuild")' in S
    assert "Xuất lại video hướng dẫn" in T
    assert "tutorial_password" in T and "tutorial_seed_data" in T

def test_background_job_and_state():
    assert "_tutorial_job_lock" in S
    assert "tutorial_history" in S
    assert "tutorial_job_running" in S
    assert "target=_run_tutorial_job" in S

def test_builder_is_isolated_and_networked():
    assert "mcr.microsoft.com/playwright:v1.62.1-noble" in S
    assert '"--network","mesflow-edge"' in S
    assert ':/opt/mesflow:ro"' in S
    assert "mesflow-tutorial-builder" in S

def test_seed_cleanup_and_publish():
    assert '_tutorial_seed(log,"seed")' in S
    assert '_tutorial_seed(log,"cleanup")' in S
    assert 'runtime/tutorials' in S and 'manifest.json' in S

def test_password_is_not_persisted_or_logged():
    worker=S[S.index("def _run_tutorial_job"):S.index("def start_tutorial_job")]
    assert 'child_env["MESFLOW_TUTORIAL_PASSWORD"]=password' in worker
    assert "password hidden" in worker
    assert '"password":password' not in worker
    assert 'password={password}' not in worker
