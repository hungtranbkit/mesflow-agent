import io
import json
from pathlib import Path

import agent


ROOT=Path(__file__).resolve().parents[1]
DEPLOY_TEMPLATE=(ROOT/'templates/release/_deploy_tab.html').read_text(encoding='utf-8')
MONITOR_TEMPLATE=(ROOT/'templates/tutorial_monitor.html').read_text(encoding='utf-8')


def authenticated_client():
    client=agent.app.test_client()
    with client.session_transaction() as state:
        state['authenticated']=True
        state['csrf']='tutorial-csrf'
    return client


HEADERS={'X-CSRF-Token':'tutorial-csrf'}


def test_tutorial_check_login_success(monkeypatch):
    monkeypatch.setattr(agent,'MESFLOW_BUILD_ENABLED',True)
    monkeypatch.setattr(agent,'_tutorial_login_preflight',lambda username,password,log: True)
    response=authenticated_client().post('/api/tutorial/check-login',json={'username':'operator','password':'valid-secret'},headers=HEADERS)
    assert response.status_code==200
    assert response.get_json()=={'ok':True,'authenticated':True,'username':'operator','status':200,'message':'Đăng nhập MESFlow thành công'}


def test_tutorial_check_login_401(monkeypatch):
    monkeypatch.setattr(agent,'MESFLOW_BUILD_ENABLED',True)
    def denied(username,password,log):
        raise agent.TutorialAuthError(401,'Sai username/password')
    monkeypatch.setattr(agent,'_tutorial_login_preflight',denied)
    response=authenticated_client().post('/api/tutorial/check-login',json={'username':'operator','password':'wrong-secret'},headers=HEADERS)
    assert response.status_code==401
    payload=response.get_json()
    assert payload['authenticated'] is False
    assert payload['code']=='TUTORIAL_AUTH_FAILED'
    assert payload['status']==401


def test_tutorial_check_login_never_logs_password(monkeypatch,tmp_path):
    monkeypatch.setattr(agent,'MESFLOW_BUILD_ENABLED',True)
    monkeypatch.setattr(agent,'LOG_DIR',tmp_path)
    seen={}
    def accepted(username,password,log):
        seen['memory_only']=isinstance(log,io.StringIO)
        log.write('[AUTH] valid\n')
    monkeypatch.setattr(agent,'_tutorial_login_preflight',accepted)
    password='never-persist-this-password'
    response=authenticated_client().post('/api/tutorial/check-login',json={'username':'operator','password':password},headers=HEADERS)
    assert response.status_code==200 and seen['memory_only']
    assert not list(tmp_path.iterdir())
    assert password not in response.get_data(as_text=True)


def test_tutorial_start_returns_202_job_id(monkeypatch):
    monkeypatch.setattr(agent,'MESFLOW_BUILD_ENABLED',True)
    monkeypatch.setattr(agent,'tutorial_job_running',lambda:False)
    job={'job_id':'tutorial-test-1','status':'queued','stage':'queued','progress':0,'agent_version':agent.AGENT_VERSION}
    monkeypatch.setattr(agent,'start_tutorial_job',lambda username,password,seed_data:job)
    response=authenticated_client().post('/api/tutorial/export',json={'username':'operator','password':'secret','seed_data':True},headers=HEADERS)
    assert response.status_code==202
    assert response.get_json()['job_id']=='tutorial-test-1'
    assert response.get_json()['status']=='queued'


def test_tutorial_duplicate_job_returns_409(monkeypatch):
    monkeypatch.setattr(agent,'MESFLOW_BUILD_ENABLED',True)
    monkeypatch.setattr(agent,'tutorial_job_running',lambda:True)
    existing={'job_id':'tutorial-running','status':'running','stage':'recording','agent_version':agent.AGENT_VERSION}
    monkeypatch.setattr(agent,'_tutorial_find_job',lambda job_id:existing)
    response=authenticated_client().post('/api/tutorial/export',json={'username':'operator','password':'secret'},headers=HEADERS)
    assert response.status_code==409
    payload=response.get_json()
    assert payload['code']=='TUTORIAL_JOB_ALREADY_RUNNING'
    assert payload['existing_job_id']=='tutorial-running'


def _assert_status(monkeypatch,status,stage):
    job={'job_id':'tutorial-state','status':status,'stage':stage,'progress':42,'current':5,'total':14,
         'message':'state','agent_version':agent.AGENT_VERSION}
    monkeypatch.setattr(agent,'_tutorial_find_job',lambda job_id:job)
    response=authenticated_client().get('/api/tutorial/job/tutorial-state')
    assert response.status_code==200
    payload=response.get_json()['job']
    assert payload['status']==status and payload['stage']==stage and payload['progress']==42


def test_tutorial_job_status_running(monkeypatch):
    _assert_status(monkeypatch,'running','recording')


def test_tutorial_job_status_failed(monkeypatch):
    _assert_status(monkeypatch,'failed','recording')


def test_tutorial_job_status_completed(monkeypatch):
    _assert_status(monkeypatch,'completed','completed')


def test_tutorial_ui_buttons_have_type_button():
    assert 'id="tutorialCheckLoginButton" class="secondary btn" type="button"' in DEPLOY_TEMPLATE
    assert 'id="tutorialExportButton" class="btn btn-primary" type="button"' in DEPLOY_TEMPLATE
    assert 'id="tutorialRefresh" class="secondary btn" type="button"' in MONITOR_TEMPLATE
    assert 'id="tutorialCopyError" class="secondary btn" type="button"' in MONITOR_TEMPLATE


def test_tutorial_ui_check_login_is_ajax():
    assert "fetch(url" in (ROOT/'templates/release/index.html').read_text(encoding='utf-8')
    assert "apiFetch(CHECK_URL" in DEPLOY_TEMPLATE
    assert "addEventListener('click'" in DEPLOY_TEMPLATE


def test_tutorial_ui_export_is_ajax():
    assert "apiFetch(START_URL" in DEPLOY_TEMPLATE
    assert "data._status===202" in DEPLOY_TEMPLATE
    assert "TUTORIAL_JOB_ALREADY_RUNNING" in DEPLOY_TEMPLATE


def test_tutorial_monitor_polls_status():
    assert "fetch(STATUS_URL" in MONITOR_TEMPLATE
    assert "setTimeout(poll,1500)" in MONITOR_TEMPLATE
    assert "tutorialProgress" in MONITOR_TEMPLATE


def test_historical_tutorial_failure_not_current_system_error():
    historical={'tutorial_job':{'status':'failed','message':'exit 127','agent_version':'2.24.1-docker-runtime'}}
    assert not any(x['component']=='Tutorial video' for x in agent._release_system_issues(historical))
    auth={'tutorial_job':{'status':'failed','message':'HTTP 401','error_code':'TUTORIAL_AUTH_FAILED','agent_version':agent.AGENT_VERSION}}
    assert not any(x['component']=='Tutorial video' for x in agent._release_system_issues(auth))


def test_progress_parser_updates_structured_recording_state(monkeypatch,tmp_path):
    log=tmp_path/'tutorial_test.log'
    log.write_text('TUTORIAL_PROGRESS stage=recording current=5 total=14 item=04_material_flow\n',encoding='utf-8')
    monkeypatch.setattr(agent,'LOG_DIR',tmp_path)
    captured={}
    monkeypatch.setattr(agent,'_tutorial_update',lambda job_id=None,**values:captured.update(values))
    agent._tutorial_refresh_progress_from_log(log,'tutorial-test')
    assert captured['stage']=='recording'
    assert captured['current']==5 and captured['total']==14
    assert captured['current_item']=='04_material_flow'


def test_progress_parser_exposes_current_tutorial_step(monkeypatch,tmp_path):
    log=tmp_path/'tutorial_step.log'
    log.write_text('TUTORIAL_PROGRESS stage=recording current=2 total=14 item=01_dashboard\nTUTORIAL_STEP step_id=dashboard-04 number=4 selector=%23dailyKpis action=%C4%90%E1%BB%8Dc%20KPI expected=Hi%E1%BB%83u%20s%E1%BB%91%20li%E1%BB%87u\n',encoding='utf-8')
    monkeypatch.setattr(agent,'LOG_DIR',tmp_path)
    captured={}
    monkeypatch.setattr(agent,'_tutorial_update',lambda job_id=None,**values:captured.update(values))
    agent._tutorial_refresh_progress_from_log(log,'tutorial-test')
    assert captured['step_id']=='dashboard-04'
    assert captured['step_number']==4
    assert captured['focus_selector']=='#dailyKpis'
    assert captured['current_action']=='Đọc KPI'
    assert captured['expected_result']=='Hiểu số liệu'


def test_progress_parser_exposes_coverage_gate(monkeypatch,tmp_path):
    from urllib.parse import quote
    payload={'status':'passed','summary':{'feature_count':14,'covered':14,'overall_percent':91},
             'gate':{'selectors':True},'report_path':'/work/tutorial-coverage-report.json'}
    log=tmp_path/'tutorial_coverage.log'
    log.write_text('TUTORIAL_PROGRESS stage=recording\nTUTORIAL_COVERAGE payload='+quote(json.dumps(payload))+'\n',encoding='utf-8')
    monkeypatch.setattr(agent,'LOG_DIR',tmp_path)
    captured={};monkeypatch.setattr(agent,'_tutorial_update',lambda job_id=None,**values:captured.update(values))
    agent._tutorial_refresh_progress_from_log(log,'tutorial-test')
    assert captured['coverage_status']=='passed'
    assert captured['coverage']['covered']==14
    assert captured['selector_preflight'] is True


def test_tutorial_monitor_renders_coverage_metrics():
    for element_id in ('coverageFeatures','coverageHappy','coverageException','coverageRecovery','coverageOverall','coverageGate'):
        assert f'id="{element_id}"' in MONITOR_TEMPLATE


def test_tutorial_source_bind_uses_docker_host_mount(monkeypatch):
    monkeypatch.setenv('HOSTNAME','agent-container-id')
    mounts=[{'Type':'bind','Source':'/home/dell/workspace/mesflow','Destination':'/workspace/mesflow'}]
    class Result:
        returncode=0
        stdout=json.dumps(mounts)
    monkeypatch.setattr(agent.subprocess,'run',lambda *args,**kwargs:Result())
    resolved=agent._docker_host_bind_path('/workspace/mesflow/mesflow')
    assert resolved==Path('/home/dell/workspace/mesflow/mesflow')


def test_tutorial_source_bind_falls_back_outside_container(monkeypatch):
    monkeypatch.delenv('HOSTNAME',raising=False)
    source=Path('/workspace/mesflow/mesflow')
    assert agent._docker_host_bind_path(source)==source


def test_tutorial_monitor_shows_step_action_selector_and_expected_result():
    for marker in ('tutorialStepId','tutorialStepNumber','tutorialFocusSelector','tutorialCurrentAction','tutorialExpectedResult'):
        assert marker in MONITOR_TEMPLATE
    assert 'bug_report_path' in MONITOR_TEMPLATE
