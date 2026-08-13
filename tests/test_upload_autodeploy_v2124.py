from pathlib import Path
import ast
ROOT=Path(__file__).resolve().parents[1]

def test_version_sync():
    assert (ROOT/'VERSION.txt').read_text().strip()=='2.12.4-docker-runtime'
    s=(ROOT/'agent.py').read_text()
    assert 'AGENT_VERSION="2.12.4-docker-runtime"' in s

def test_mes_upload_auto_deploys():
    s=(ROOT/'agent.py').read_text()
    block=s[s.index('@app.post("/upload")'):s.index('@app.post("/deploy/<version>")')]
    assert 'start_deploy(v)' in block
    assert 'Đã upload và bắt đầu deploy MESFlow' in block

def test_qa_upload_auto_deploys():
    s=(ROOT/'agent.py').read_text()
    block=s[s.index('@app.post("/qa/upload")'):s.index('@app.post("/qa/deploy/<version>")')]
    assert 'start_qa_deploy(v)' in block
    assert 'Đã upload và bắt đầu deploy QA Center' in block

def test_file_picker_auto_submits_without_upload_button():
    h=(ROOT/'templates/index.html').read_text()
    assert 'this.form.submit()' in h
    assert '>Upload</button>' not in h
    assert '>Upload QA</button>' not in h
    assert 'Upload & Deploy MESFlow' in h
    assert 'Upload & Deploy QA Center' in h

def test_agent_parses():
    ast.parse((ROOT/'agent.py').read_text())
