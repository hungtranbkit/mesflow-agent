from pathlib import Path
R=Path(__file__).resolve().parent

def test_version():
    assert (R/'VERSION.txt').read_text().strip()=='2.12.5-docker-runtime'
    assert 'AGENT_VERSION="2.12.5-docker-runtime"' in (R/'agent.py').read_text()

def test_file_input_not_disabled_before_submit():
    s=(R/'templates/index.html').read_text()
    assert 'this.disabled=true' not in s
    assert 'requestSubmit()' in s
    assert "dataset.submitting" in s

def test_both_uploads_are_auto_submit():
    s=(R/'templates/index.html').read_text()
    assert s.count('requestSubmit()')==2
    assert 'name="release_zip"' in s
    assert 'name="qa_release_zip"' in s
