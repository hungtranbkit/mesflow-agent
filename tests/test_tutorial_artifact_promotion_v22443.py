import hashlib, json
from pathlib import Path

import agent


def tutorial_source(root: Path):
    (root/'videos').mkdir(parents=True)
    (root/'videos/01-login.mp4').write_bytes(b'fake-mp4-data')
    (root/'manifest.json').write_text(json.dumps({'items':[{'id':'login','title':'Login','description':'Sign in','file':'videos/01-login.mp4','order':1}]}),encoding='utf-8')
    return root


def client():
    value=agent.app.test_client()
    with value.session_transaction() as session: session['authenticated']=True;session['csrf']='csrf'
    return value


def test_manifest_and_immutable_artifact_sha(monkeypatch,tmp_path):
    source=tutorial_source(tmp_path/'source'); publish=tmp_path/'publish'; artifacts=tmp_path/'artifacts';artifacts.mkdir()
    monkeypatch.setattr(agent,'TUTORIAL_ARTIFACT_DIR',artifacts)
    manifest=agent._tutorial_build_publish_tree(source,publish,'tutorial-job-1')
    assert manifest['schema_version']==1 and manifest['source_job_id']=='tutorial-job-1'
    assert manifest['video_count']==1 and manifest['videos'][0]['file']=='videos/01-login.mp4'
    artifact=agent._tutorial_create_artifact(publish,'tutorial-job-1'); path=Path(artifact['artifact_path'])
    assert artifact['sha256']==hashlib.sha256(path.read_bytes()).hexdigest()


def test_completed_artifact_remains_addressable_after_agent_upgrade(monkeypatch):
    job={'job_id':'tutorial-old','status':'completed','agent_version':'older-agent'}
    monkeypatch.setattr(agent,'load_state',lambda:{'tutorial_job':job,'tutorial_history':[]})
    assert agent._tutorial_find_job('tutorial-old')==job
    assert agent._tutorial_find_job('current')=={}


def test_path_traversal_manifest_rejected(tmp_path):
    root=tmp_path/'bad';root.mkdir();(root/'manifest.json').write_text(json.dumps({'videos':[{'file':'../../secret.mp4'}]}))
    try: agent._tutorial_validate_tree(root)
    except ValueError as exc: assert 'PATH_TRAVERSAL' in str(exc)
    else: raise AssertionError('traversal accepted')


def test_atomic_publish_keeps_previous_on_invalid_new(monkeypatch,tmp_path):
    previous=tutorial_source(tmp_path/'previous'); target=tmp_path/'runtime/tutorials'
    good=tmp_path/'good';agent._tutorial_build_publish_tree(previous,good,'old');agent._tutorial_atomic_publish(good,target)
    bad=tmp_path/'bad';bad.mkdir();(bad/'manifest.json').write_text('{}')
    try: agent._tutorial_atomic_publish(bad,target)
    except ValueError: pass
    assert json.loads((target/'manifest.json').read_text())['source_job_id']=='old'


def test_capability_missing_and_production_role_blocked(monkeypatch,tmp_path):
    result=agent._agent_compatibility({'agent_version':'x','api_protocol_version':2,'capabilities':[]},agent.TUTORIAL_REQUIRED_CAPABILITIES)
    assert result['status']=='INCOMPATIBLE' and 'tutorial_deploy' in result['missing']
    monkeypatch.setattr(agent,'SERVER_ROLE','PRODUCTION')
    response=client().post('/tutorial-release/chunk/start',json={'filename':'x.tar.zst','total_size':1,'chunk_size':1,'sha256':'a'*64},headers={'X-CSRF-Token':'csrf'})
    assert response.status_code==403 and response.get_json()['error']=='TUTORIAL_PRODUCTION_DEPLOY_BLOCKED'


def test_chunk_missing_and_digest_mismatch(monkeypatch,tmp_path):
    monkeypatch.setattr(agent,'SERVER_ROLE','PRODUCTION_TEST');monkeypatch.setattr(agent,'TUTORIAL_CHUNK_DIR',tmp_path/'chunks');agent.TUTORIAL_CHUNK_DIR.mkdir()
    c=client(); payload={'filename':'x.tar.zst','total_size':8,'chunk_size':8,'sha256':hashlib.sha256(b'12345678').hexdigest()}
    upload=c.post('/tutorial-release/chunk/start',json=payload,headers={'X-CSRF-Token':'csrf'}).get_json()['upload_id']
    missing=c.post(f'/tutorial-release/chunk/{upload}/complete',json={'chunks':1},headers={'X-CSRF-Token':'csrf'})
    assert missing.status_code==400 and 'MISSING_CHUNK' in missing.get_json()['error']
    corrupt=c.post(f'/tutorial-release/chunk/{upload}/0',data=b'12345678',headers={'X-CSRF-Token':'csrf','X-Chunk-SHA256':'0'*64})
    assert corrupt.status_code==400 and 'SHA256_MISMATCH' in corrupt.get_json()['error']


def test_chunk_upload_happy_path(monkeypatch,tmp_path):
    monkeypatch.setattr(agent,'SERVER_ROLE','PRODUCTION_TEST');monkeypatch.setattr(agent,'TUTORIAL_CHUNK_DIR',tmp_path/'chunks');agent.TUTORIAL_CHUNK_DIR.mkdir()
    monkeypatch.setattr(agent,'_tutorial_unpack_publish',lambda path,digest:{'video_count':14})
    data=b'abcdefgh'; digest=hashlib.sha256(data).hexdigest();c=client()
    upload=c.post('/tutorial-release/chunk/start',json={'filename':'tutorial.tar.zst','total_size':8,'chunk_size':4,'sha256':digest},headers={'X-CSRF-Token':'csrf'}).get_json()['upload_id']
    for index,chunk in enumerate((data[:4],data[4:])):
        response=c.post(f'/tutorial-release/chunk/{upload}/{index}',data=chunk,headers={'X-CSRF-Token':'csrf','X-Chunk-SHA256':hashlib.sha256(chunk).hexdigest()})
        assert response.status_code==200
    final=c.post(f'/tutorial-release/chunk/{upload}/complete',json={'chunks':2},headers={'X-CSRF-Token':'csrf'})
    assert final.status_code==200 and final.get_json()['sha256']==digest
