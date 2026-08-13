from pathlib import Path
R=Path(__file__).resolve().parents[2]
A=R/"payload/mesflow-deploy-agent"
S=(A/"agent.py").read_text()
T=(A/"templates/index.html").read_text()

def test_version():
    assert (A/"VERSION.txt").read_text().strip()=="2.13.0-docker-runtime"
    assert 'AGENT_VERSION="2.13.0-docker-runtime"' in S
    assert 'image: mesflow-deploy-agent:2.13.0' in (A/"docker/compose.linux.yml").read_text()

def test_auth_preflight_before_seed():
    assert "def _tutorial_login_preflight" in S
    worker=S[S.index("def _run_tutorial_job"):S.index("def start_tutorial_job")]
    assert worker.index("_tutorial_login_preflight") < worker.index('_tutorial_seed(log,"seed")')

def test_login_check_route_and_button():
    assert '@app.post("/tutorial/check-login")' in S
    assert "Kiểm tra đăng nhập" in T
    assert "tutorial_check_login" in T

def test_429_and_bad_password_handling():
    helper=S[S.index("def _tutorial_login_preflight"):S.index("def _tutorial_seed")]
    assert "ex.code==429" in helper
    assert "Sai tài khoản hoặc mật khẩu MESFlow" in helper
    assert "time.sleep(wait)" in helper

def test_password_not_logged():
    helper=S[S.index("def _tutorial_login_preflight"):S.index("def _tutorial_seed")]
    # Password is only encoded into the request payload; no log/write/format contains it.
    assert 'log.write' in helper
    assert 'password)' not in "\n".join(
        line for line in helper.splitlines() if "log.write" in line or "RuntimeError" in line
    )
