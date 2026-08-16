from pathlib import Path


def test_release_tab_does_not_full_reload_every_three_seconds():
    text=(Path(__file__).parents[1]/"templates/release/_deploy_tab.html").read_text(encoding="utf-8")
    assert "release_manager_live_status" in text
    assert "setInterval(pollReleaseState,5000)" in text
    assert "if(wasActive&&!data.active)" in text
    assert "if({{ 'true' if" not in text or "location.reload()},3000)" not in text


def test_release_live_status_is_state_only():
    text=(Path(__file__).parents[1]/"agent.py").read_text(encoding="utf-8")
    start=text.index("def _release_live_status_payload():")
    end=text.index('@app.get("/api/status")', start)
    body=text[start:end]
    assert "load_state()" in body
    assert "_release_summary()" not in body
    assert "_qa_release_summary()" not in body
    assert "mes_status()" not in body
    assert "qa_status()" not in body
    assert "tail(" not in body


def test_release_page_context_does_not_tail_runtime_logs():
    text=(Path(__file__).parents[1]/"agent.py").read_text(encoding="utf-8")
    start=text.index("def _release_deploy_tab_context():")
    end=text.index("def _deployment_history_tab_context():", start)
    body=text[start:end]
    assert 'j["log_tail"]=tail' not in body
    assert "qa_log=tail(" not in body
