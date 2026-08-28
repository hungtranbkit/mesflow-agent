from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
S=(ROOT/'agent.py').read_text()
T=(ROOT/'templates/tutorial_monitor.html').read_text()
D=(ROOT/'templates/release/_deploy_tab.html').read_text()

def test_monitor_routes_exist_and_rebuild_redirects_to_monitor():
    assert '@app.get("/tutorial/monitor")' in S
    assert '@app.get("/tutorial/events")' in S
    block=S[S.index('@app.post("/tutorial/rebuild")'):S.index('ESP_TUTORIAL_STAGES=')]
    assert 'redirect(url_for("tutorial_monitor"))' in block

def test_monitor_uses_sse_not_client_polling():
    assert 'EventSource' in T
    assert 'setInterval(elapsed,1000)' in T  # local timer only
    assert 'fetch(' not in T
    assert 'text/event-stream' in S
    assert 'X-Accel-Buffering' in S

def test_release_page_only_links_monitor_and_does_not_add_tutorial_poller():
    assert "url_for('tutorial_monitor')" in D
    card=D[D.index('id="mesflowTutorialCard"'):D.index('{# ESP Firmware Builder')]
    assert 'setInterval' not in card and 'fetch(' not in card and 'EventSource' not in card

def test_monitor_log_path_is_confined_to_agent_log_dir():
    helper=S[S.index('def _tutorial_safe_log_path'):S.index('def _tutorial_update')]
    assert 'path.parent!=root' in helper
    assert 'path.name.startswith("tutorial_")' in helper
    assert 'path.suffix!=".log"' in helper

def test_code_125_failure_gets_log_tail_hint():
    worker=S[S.index('def _run_tutorial_job'):S.index('def start_tutorial_job')]
    assert '_tutorial_error_hint(log_path)' in worker
    assert 'Tutorial builder exited with code {r.returncode}{detail}' in worker
