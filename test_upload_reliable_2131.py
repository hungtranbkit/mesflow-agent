from pathlib import Path
R=Path(__file__).resolve().parents[2]
A=R/"payload/mesflow-deploy-agent"
S=(A/"agent.py").read_text()
T=(A/"templates/index.html").read_text()

def test_version():
    assert (A/"VERSION.txt").read_text().strip()=="2.13.1-docker-runtime"
    assert 'AGENT_VERSION="2.13.1-docker-runtime"' in S
    assert 'image: mesflow-deploy-agent:2.13.1' in (A/"docker/compose.linux.yml").read_text()

def test_mes_upload_has_real_button():
    assert 'id="mesUploadForm"' in T
    assert 'id="mesUploadButton"' in T
    assert '>Upload & Deploy</button>' in T

def test_no_inline_requestsubmit_dependency():
    assert 'onchange="if(this.files.length' not in T
    assert "typeof form.requestSubmit==='function'" in T
    assert "else form.submit()" in T

def test_visible_upload_state():
    assert "Đang upload file lên Deploy Agent..." in T
    assert "Đã chọn:" in T

def test_backend_upload_diagnostics():
    assert "MES upload request received" in S
    assert "MES upload saved: bytes=" in S
    assert "MES upload validated: version=" in S
    assert "MES upload/deploy failed before redirect" in S
