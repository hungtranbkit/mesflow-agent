import json,tempfile
from pathlib import Path
import agent

class TestAgentEvidence:
 def setup_method(self):
  self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name);self.src=root/'mesflow';self.src.mkdir()
  release=root/'artifacts'/'releases'/'70.0.0.1';release.mkdir(parents=True)
  meta={'type':'mesflow-image-release','version':'70.0.0.1','image':'mesflow-app:70.0.0.1','image_digest':'sha256:'+'a'*64,'image_id':'sha256:'+'a'*64,'source_commit':'deadbeef','schema_revision':'0034_v70_release_gate'}
  (release/'release.json').write_text(json.dumps(meta));(release/'image-info.json').write_text(json.dumps(meta));package=release/'MESFlow_70.0.0.1.deploy.zip';package.write_bytes(b'frozen')
  self.old=(agent.MESFLOW_SOURCE_DIR,agent.PROMOTION_STATE_FILE);agent.MESFLOW_SOURCE_DIR=self.src;agent.PROMOTION_STATE_FILE=root/'promotion.json'
  agent.save_json(agent.PROMOTION_STATE_FILE,{'version':'70.0.0.1','test_deployment_verified':True,'test_deployed_digest':'sha256:'+'a'*64,'test_target':'TEST'})
 def teardown_method(self):agent.MESFLOW_SOURCE_DIR,agent.PROMOTION_STATE_FILE=self.old;self.tmp.cleanup()
 def payload(self,env,run_id,digest='sha256:'+'a'*64):
  return {'run_id':run_id,'environment':env,'profile':'release-'+env.lower(),'status':'PASS','artifact_digest':digest,'started_at':'x','finished_at':'y','checks':[{'name':'required','status':'PASS','required':True}]}
 def test_local_then_test_evidence_reaches_ready(self):
  first=agent._record_gate_run('70.0.0.1',self.payload('LOCAL','LOCAL-1'));assert not first['decision']['ready']
  final=agent._record_gate_run('70.0.0.1',self.payload('TEST','TEST-1'));assert final['decision']['ready']
  assert (agent._release_dir_for('70.0.0.1')/'evidence'/'release-evidence.json').is_file()
 def test_late_wrong_digest_is_rejected(self):
  try:agent._record_gate_run('70.0.0.1',self.payload('TEST','TEST-old','sha256:'+'c'*64))
  except ValueError as exc:assert str(exc)=='ARTIFACT_MISMATCH'
  else:raise AssertionError('stale result accepted')
 def test_v70_deploy_preflight_is_not_local_pass(self,monkeypatch):
  monkeypatch.setattr(agent,'_actual_running_mes_image_id',lambda:'sha256:'+'a'*64);monkeypatch.setattr(agent,'_actual_schema_revision',lambda:'0034_v70_release_gate');monkeypatch.setattr(agent,'_http_smoke_ok',lambda:True);monkeypatch.setattr(agent,'_release_contamination',lambda v:{'contaminated':False})
  monkeypatch.setattr(agent.threading,'Thread',lambda **kw:type('T',(),{'start':lambda self:None})())
  agent._record_local_deploy_result('70.0.0.1',True);state=agent.load_json(agent.PROMOTION_STATE_FILE,{})
  assert state['local_status']=='AWAITING_QA' and state['local_pass'] is False
