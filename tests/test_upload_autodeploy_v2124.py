from pathlib import Path
import ast
import re
ROOT=Path(__file__).resolve().parents[1]

def test_version_sync():
    version=(ROOT/'VERSION.txt').read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+-docker-runtime", version), version
    s=(ROOT/'agent.py').read_text()
    assert f'AGENT_VERSION="{version}"' in s

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

def test_file_picker_has_auto_submit_and_a_visible_fallback_button():
    # AGENTS.md: "Upload UI must have a visible fallback action; never rely
    # on only one fragile JavaScript auto-submit path." The upload flow may
    # still auto-submit on file selection as a convenience, but a real,
    # always-visible <button type="submit"> must exist so a user can send
    # the upload manually if auto-submit doesn't fire.
    h=(ROOT/'templates/index.html').read_text()
    assert 'requestSubmit' in h or 'form.submit()' in h  # auto-submit convenience path exists
    assert re.search(r'id="mesUploadButton"[^>]*type="submit"', h), "MESFlow upload needs a visible submit button"
    assert re.search(r'id="qaUploadButton"[^>]*type="submit"', h), "QA upload needs a visible submit button"
    assert 'Upload & Deploy' in h
    assert 'Upload & Deploy QA' in h

def test_agent_parses():
    ast.parse((ROOT/'agent.py').read_text())
