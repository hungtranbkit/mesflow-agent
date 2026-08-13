import importlib.util
import json
import os
import sys
import types
import zipfile
from pathlib import Path


def load_agent(tmp_path):
    sys.modules.setdefault('paramiko',types.SimpleNamespace())
    os.environ['WORKSHOP_AGENT_HOME']=str(tmp_path/'agent')
    os.environ['WORKSHOP_TARGET_HOME']=str(tmp_path/'mesflow')
    os.environ['MESFLOW_ESP_TUTORIAL_HOME']=str(tmp_path/'mesflow/runtime/tutorials/esp-kiosk')
    spec=importlib.util.spec_from_file_location('agent_esp_test',Path(__file__).parents[1]/'agent.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def package(tmp_path,version='5.1.9.1',payload=b'video',unsafe=False):
    path=tmp_path/f'tutorial-{version}.zip'
    manifest={'type':'esp-kiosk-tutorial','tutorial_version':version,'firmware_version':'ESP32-KIOSK-5.1.9-WORKER-QTY-FLOW','audio':False,
              'videos':[{'id':'overview','filename':'00.mp4','title':'Tổng quan','description':'Mô tả','order':0,'cases':['A'],'duration_seconds':4}]}
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('esp-kiosk-tutorial/VERSION.txt',version+'\n')
        z.writestr('esp-kiosk-tutorial/manifest.json',json.dumps(manifest))
        z.writestr('esp-kiosk-tutorial/videos/00.mp4',payload)
        if unsafe:z.writestr('../escape.txt','bad')
    return path


def test_atomic_publish_same_version_and_backup(tmp_path):
    agent=load_agent(tmp_path)
    first=package(tmp_path,'5.1.9.1',b'first')
    assert agent.publish_esp_tutorial(first,first.name)=='5.1.9.1'
    home=agent.ESP_TUTORIAL_HOME
    assert (home/'videos/00.mp4').read_bytes()==b'first'
    assert agent.publish_esp_tutorial(first,first.name)=='5.1.9.1'
    changed=package(tmp_path,'5.1.9.1',b'changed')
    try: agent.publish_esp_tutorial(changed,changed.name)
    except ValueError as ex: assert 'tăng version' in str(ex)
    else: raise AssertionError('changed same version must fail')
    assert (home/'videos/00.mp4').read_bytes()==b'first'
    second=package(tmp_path,'5.1.9.2',b'second')
    assert agent.publish_esp_tutorial(second,second.name)=='5.1.9.2'
    assert (home/'videos/00.mp4').read_bytes()==b'second'
    backups=list((home.parent/'backups').glob('esp-kiosk-*'))
    assert len(backups)==1 and (backups[0]/'videos/00.mp4').read_bytes()==b'first'


def test_zip_slip_rejected(tmp_path):
    agent=load_agent(tmp_path);archive=package(tmp_path,unsafe=True);extract=tmp_path/'extract';extract.mkdir()
    try: agent.validate_esp_tutorial_zip(archive,extract)
    except ValueError as ex: assert 'an toàn' in str(ex) or 'traversal' in str(ex)
    else: raise AssertionError('zip-slip must fail')


def test_agent_card_current_info_progress_and_view_link(tmp_path,monkeypatch):
    agent=load_agent(tmp_path)
    first=package(tmp_path,'5.1.9.1',b'first');second=package(tmp_path,'5.1.9.2',b'second')
    agent.publish_esp_tutorial(first,first.name);agent.publish_esp_tutorial(second,second.name)
    monkeypatch.setattr(agent,'mes_status',lambda:{'online':True,'health_state':'healthy','version':'test','deployment_id':''})
    monkeypatch.setattr(agent,'qa_status',lambda:{'online':False,'version':'test','port':8095,'payload':{}})
    with agent.app.test_client() as client:
        with client.session_transaction() as session:
            session['authenticated']=True;session['csrf']='test-csrf'
        response=client.get('/')
    html=response.get_data(as_text=True)
    assert response.status_code==200
    for text in ('Video hướng dẫn ESP Kiosk','5.1.9.2','5.1.9.1','Upload &amp; Publish','Đang kiểm tra ZIP','Đang kiểm tra manifest','Đang backup bản cũ','Mở hướng dẫn ESP Kiosk'):
        assert text in html
    assert 'href="/app?guide=esp-kiosk"' in html
    assert 'id="espTutorialFile"' in html and 'id="espTutorialState"' in html
