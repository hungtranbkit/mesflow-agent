import json
from pathlib import Path
import pytest
from release_gate import CheckResult,ReleaseEvidenceStore,ReleaseGatePolicy,ReleaseIdentity,TestRun

DIGEST="sha256:"+"a"*64
def identity(digest=DIGEST): return ReleaseIdentity("70.0.0.1","deadbeef","mesflow-app:70.0.0.1",digest,"b"*64,"0034_v70_release_gate")
def run(env="LOCAL",status="PASS",digest=DIGEST,run_id="LOCAL-1",checks=None):
 return TestRun(run_id,env,f"release-{env.lower()}",status,digest,"2026-08-13T01:00:00Z","2026-08-13T01:01:00Z",tuple(checks or [CheckResult("required",status)]),1,int(status=="PASS"),int(status=="FAIL"))
def evaluate(**kw):
 base=dict(identity=identity(),build_status="PASS",local_run=run(),test_deployment={"status":"PASS","deployed_digest":DIGEST},test_run=run("TEST",run_id="TEST-1"),known_issues=[],evidence_complete=True);base.update(kw);return ReleaseGatePolicy.evaluate(**base)

def test_complete_evidence_is_ready(): assert evaluate()["decision"]=="READY_FOR_PRODUCTION_APPROVAL"
def test_local_pass_cannot_exist_without_build(): assert evaluate(build_status="FAIL")["decision"]=="BUILD_FAILED"
def test_wrong_local_digest_rejected(): assert "ARTIFACT_MISMATCH" in evaluate(local_run=run(digest="sha256:"+"c"*64))["reasons"]
def test_required_failure_blocks_promotion(): assert evaluate(local_run=run(status="PASS",checks=[CheckResult("migration","FAIL")]))["decision"]=="LOCAL_FAILED"
def test_test_deployment_digest_mismatch_blocks_test_pass(): assert evaluate(test_deployment={"status":"PASS","deployed_digest":"sha256:"+"d"*64})["decision"]=="TEST_DEPLOY_FAILED"
def test_stale_test_result_rejected(): assert "ARTIFACT_MISMATCH" in evaluate(test_run=run("TEST",digest="sha256:"+"e"*64))["reasons"]
def test_blocking_issue_blocks_readiness(): assert evaluate(known_issues=[{"blocking":True,"title":"P0"}])["ready"] is False
def test_nonblocking_issue_does_not_block(): assert evaluate(known_issues=[{"blocking":False,"title":"P3"}])["ready"] is True
def test_incomplete_evidence_blocks_readiness(): assert evaluate(evidence_complete=False)["decision"]=="EVIDENCE_INCOMPLETE"
def test_failed_runs_are_append_only_and_summary_hashed(tmp_path):
 store=ReleaseEvidenceStore(tmp_path);store.append_run(run(status="FAIL",run_id="LOCAL-1"));store.append_run(run(run_id="LOCAL-2"))
 with pytest.raises(FileExistsError):store.append_run(run(run_id="LOCAL-2"))
 assert len(store.list_runs())==2
 payload={"release":identity().__dict__,"build":{"status":"PASS"},"local":{"status":"PASS"},"test_deployment":{"status":"PASS"},"test":{"status":"PASS"},"known_blocking_issues":0,"decision":{"decision":"READY_FOR_PRODUCTION_APPROVAL"}}
 result=store.write_summary(payload);assert Path(result["json"]).exists() and len(result["sha256"])==64
 assert json.loads(Path(result["json"]).read_text())["release"]["version"]=="70.0.0.1"
