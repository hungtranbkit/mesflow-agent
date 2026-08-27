import re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
S=(ROOT/'agent.py').read_text()
T=(ROOT/'templates/release/_deploy_tab.html').read_text()

def test_version_bumped():
    v=(ROOT/"VERSION.txt").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+-docker-runtime", v)
    assert f'AGENT_VERSION="{v}"' in S

def test_server_generation_routes_are_enabled_on_dev():
    check=S[S.index('@app.post("/api/tutorial/check-login")'):S.index('@app.post("/tutorial/check-login")')]
    rebuild=S[S.index('@app.post("/tutorial/rebuild")'):S.index('ESP_TUTORIAL_STAGES=')]
    assert 'SERVER_VIDEO_GENERATION_DISABLED' not in check
    assert 'SERVER_VIDEO_GENERATION_DISABLED' not in rebuild
    assert 'SERVER_VIDEO_GENERATION_DEV_ONLY' in check
    assert 'SERVER_VIDEO_GENERATION_DEV_ONLY' in rebuild
    assert 'start_tutorial_job(username,password,seed_data)' in rebuild

def test_builder_uses_workspace_source_not_opt_runtime_source():
    worker=S[S.index('def _run_tutorial_job'):S.index('def start_tutorial_job')]
    assert 'tutorial_source=MESFLOW_SOURCE_DIR' in worker
    assert 'tutorial_host_source=_docker_host_bind_path(tutorial_source)' in worker
    assert 'f"{tutorial_host_source}:/src/mesflow:ro"' in worker
    assert '"-w","/src/mesflow"' in worker
    assert 'f"{builder_publish}:/publish"' in worker
    assert '_tutorial_atomic_publish(normalized_publish,publish_dir)' in worker
    assert 'MESFLOW_TUTORIAL_PUBLISH_DIR=/publish' in worker
    assert 'TUTORIAL_SOURCE_SCRIPT_NOT_FOUND' in worker

def test_release_page_has_tutorial_controls():
    assert 'Video hướng dẫn MESFlow' in T
    assert 'Xuất lại video hướng dẫn' in T
    assert "url_for('api_tutorial_check_login')" in T
    assert "url_for('api_tutorial_export')" in T
    assert 'tutorial_password' in T
    assert 'tutorialSeedData' in T
