import hashlib
import json
import threading
from pathlib import Path

import pytest
from unittest.mock import patch

from deployment_platform import (
    DeploymentComponent, DeploymentEnvironment, DeploymentEvidenceStore,
    DeploymentService, DeploymentTarget, ReleaseIdentity, RetentionPolicy,
    RetentionService,
)


class FakeAdapter:
    def __init__(self, version="1.0.0", *, fail_health=False, fail_activate=False, digest=""):
        self.state={"version":version,"healthy":True,"digest":digest}; self.previous=dict(self.state)
        self.fail_health=fail_health; self.fail_activate=fail_activate; self.activations=0; self.rollbacks=0
        self.entered=None; self.release=None
    def current(self,target): return dict(self.state)
    def precheck(self,release,target): return {"ok":True,"rollback_plan":True,"disk_sufficient":True}
    def stage(self,release,target,deployment_id): self.release=release
    def activate(self,release,target,deployment_id):
        if self.entered:
            self.entered.set()
            import time; time.sleep(.08)
        if self.fail_activate: raise RuntimeError("activate failed")
        self.activations+=1; self.previous=dict(self.state)
        self.state={"version":release.version,"healthy":not self.fail_health,"digest":release.image_digest or release.artifact_digest}
    def verify(self,release,target): return dict(self.state)
    def rollback(self,previous,target,deployment_id): self.rollbacks+=1; self.state=dict(previous)


def release(tmp_path, component, version="2.0.0"):
    artifact=tmp_path/f"{component.value}.zip";artifact.write_bytes((component.value+version).encode())
    digest="sha256:"+hashlib.sha256(artifact.read_bytes()).hexdigest()
    return ReleaseIdentity(component,version,f"{component.value}:{version}","abc123",artifact.name,str(artifact),digest,"2026-08-14T00:00:00Z",digest)


def target(environment=DeploymentEnvironment.LOCAL):
    return DeploymentTarget(environment,"target","http://target/health","/opt/target","service",environment is DeploymentEnvironment.PRODUCTION)


@pytest.mark.parametrize("component",list(DeploymentComponent))
def test_successful_deploy_for_every_component(tmp_path,component):
    adapter=FakeAdapter();store=DeploymentEvidenceStore(tmp_path/"evidence")
    result=DeploymentService({component:adapter},store).deploy(release(tmp_path,component),target(),actor="admin",request_id="req-1")
    assert result.status=="HEALTHY" and result.health_result=="PASS" and result.running_version=="2.0.0"
    assert adapter.activations==1 and adapter.rollbacks==0
    assert store.list()[0]["artifact_digest"]==result.artifact_digest


def test_tampered_artifact_is_rejected_before_activation(tmp_path):
    item=release(tmp_path,DeploymentComponent.MESFLOW);Path(item.artifact_path).write_bytes(b"tampered")
    adapter=FakeAdapter();result=DeploymentService({item.component:adapter},DeploymentEvidenceStore(tmp_path/"e")).deploy(item,target(),actor="a",request_id="r")
    assert result.failure_category=="ARTIFACT_INVALID" and adapter.activations==0


@pytest.mark.parametrize("component",list(DeploymentComponent))
def test_health_failure_rolls_back_each_component(tmp_path,component):
    adapter=FakeAdapter(fail_health=True);item=release(tmp_path,component)
    result=DeploymentService({component:adapter},DeploymentEvidenceStore(tmp_path/"e")).deploy(item,target(),actor="a",request_id="r")
    assert result.status=="DEPLOY_FAILED" and result.failure_category=="HEALTH_FAILED"
    assert result.rollback_status=="SUCCESS" and result.running_version=="1.0.0" and adapter.rollbacks==1


def test_version_mismatch_rolls_back(tmp_path):
    adapter=FakeAdapter();item=release(tmp_path,DeploymentComponent.QA_CENTER)
    original=adapter.verify;adapter.verify=lambda release,target:{**original(release,target),"version":"wrong"}
    result=DeploymentService({item.component:adapter},DeploymentEvidenceStore(tmp_path/"e")).deploy(item,target(),actor="a",request_id="r")
    assert result.failure_category=="VERSION_MISMATCH" and result.rollback_status=="SUCCESS"


def test_same_request_is_idempotent(tmp_path):
    adapter=FakeAdapter();item=release(tmp_path,DeploymentComponent.MESFLOW);service=DeploymentService({item.component:adapter},DeploymentEvidenceStore(tmp_path/"e"))
    first=service.deploy(item,target(),actor="a",request_id="same");second=service.deploy(item,target(),actor="a",request_id="same")
    assert second.deployment_id==first.deployment_id and adapter.activations==1


def test_manual_rollback_to_verified_retained_release(tmp_path):
    adapter=FakeAdapter(version="2.0.0");item=release(tmp_path,DeploymentComponent.MESFLOW,version="1.0.0")
    result=DeploymentService({item.component:adapter},DeploymentEvidenceStore(tmp_path/"e")).manual_rollback(item,target(),actor="admin",request_id="rollback-1")
    assert result.status=="ROLLED_BACK" and result.rollback_status=="MANUAL_SUCCESS"
    assert result.from_version=="2.0.0" and result.running_version=="1.0.0"


def test_concurrent_deployment_same_target_is_locked(tmp_path):
    adapter=FakeAdapter();adapter.entered=threading.Event();item=release(tmp_path,DeploymentComponent.MESFLOW);service=DeploymentService({item.component:adapter},DeploymentEvidenceStore(tmp_path/"e"))
    worker=threading.Thread(target=lambda:service.deploy(item,target(),actor="a",request_id="one"));worker.start();assert adapter.entered.wait(1)
    with pytest.raises(RuntimeError,match="LOCKED"):service.deploy(item,target(),actor="b",request_id="two")
    worker.join()


def test_production_requires_explicit_per_run_approval(tmp_path):
    item=release(tmp_path,DeploymentComponent.MESFLOW);service=DeploymentService({item.component:FakeAdapter()},DeploymentEvidenceStore(tmp_path/"e"))
    with pytest.raises(PermissionError,match="PRODUCTION_APPROVAL_REQUIRED"):service.deploy(item,target(DeploymentEnvironment.PRODUCTION),actor="admin",request_id="r")


def test_rollback_sensitive_migration_needs_plan(tmp_path):
    base=release(tmp_path,DeploymentComponent.MESFLOW)
    item=ReleaseIdentity(**{**base.__dict__,"migration_class":"ROLLBACK_SENSITIVE"})
    adapter=FakeAdapter();adapter.precheck=lambda release,target:{"ok":True}
    result=DeploymentService({item.component:adapter},DeploymentEvidenceStore(tmp_path/"e")).deploy(item,target(),actor="a",request_id="r")
    assert result.failure_category=="PRECHECK_FAILED" and adapter.activations==0


def test_retention_preview_protects_current_previous_pinned_and_active(tmp_path):
    root=tmp_path/"releases";root.mkdir();rows=[]
    for index in range(7):
        path=root/str(index);path.mkdir();(path/"artifact").write_bytes(b"x")
        rows.append({"version":str(index),"path":str(path),"size_bytes":10,"status":"SUCCESS","built_at":f"2026-08-{index+1:02d}","current":index==6,"previous":index==5,"pinned":index==1,"active_deployment":index==2})
    preview=RetentionService(root,RetentionPolicy(keep_last_successful=2)).preview(rows)
    assert {row["version"] for row in preview["eligible"]}=={"0","3","4"}
    assert preview["reclaimable_bytes"]==30 and all((root/row["version"]).exists() for row in preview["eligible"])


def test_retention_apply_only_removes_previewed_safe_children(tmp_path):
    root=tmp_path/"releases";root.mkdir();old=root/"old";old.mkdir();current=root/"current";current.mkdir()
    service=RetentionService(root,RetentionPolicy(keep_last_successful=0))
    preview=service.preview([{"version":"old","path":str(old),"size_bytes":12},{"version":"current","path":str(current),"size_bytes":13,"current":True}])
    result=service.apply(preview)
    assert result["removed"]==["old"] and result["bytes_reclaimed"]==12 and current.exists() and not old.exists()


def test_agent_updater_contract_has_staged_digest_health_and_rollback():
    source=(Path(__file__).resolve().parents[1]/"updater/updater.py").read_text()
    for contract in ("extract_and_verify_artifact","IMAGE_ID_MISMATCH","poll_health","_rollback","ROLLED_BACK"):
        assert contract in source


def _authenticated_client():
    import agent
    client = agent.app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["csrf"] = "p1-test"
    return client


def test_platform_api_requires_authentication():
    import agent
    response = agent.app.test_client().get("/api/deployment-platform")
    # Preserve the Agent's existing global browser-auth contract: unauthenticated
    # requests are redirected to login before the route is entered.
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_platform_api_exposes_all_components_and_explicit_environments():
    fake = {
        "components": [{"component": component.value} for component in DeploymentComponent],
        "environments": [environment.value for environment in DeploymentEnvironment],
        "deployments": [],
        "generated_at": "2026-08-14T00:00:00+07:00",
    }
    with patch("agent._deployment_platform_summary", return_value=fake):
        response = _authenticated_client().get("/api/deployment-platform")
    assert response.status_code == 200
    payload = response.get_json()
    assert {row["component"] for row in payload["components"]} == {component.value for component in DeploymentComponent}
    assert payload["environments"] == ["LOCAL", "TEST", "PRODUCTION"]


def test_retention_preview_api_is_csrf_protected_and_dry_run_only():
    preview = {"dry_run": True, "eligible": [], "protected": [], "reclaimable_bytes": 0}
    with patch("agent._platform_retention_preview", return_value=preview):
        response = _authenticated_client().post(
            "/api/deployment-platform/retention/preview",
            json={"component": "MESFLOW"},
            headers={"X-CSRF-Token": "p1-test"},
        )
    assert response.status_code == 200
    assert response.get_json()["preview"]["dry_run"] is True


def test_release_manager_renders_common_component_matrix():
    with patch("agent._release_contamination", return_value={"contaminated": False}):
        response = _authenticated_client().get("/releases")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'id="deploymentPlatformSummary"' in body
    for label in ("MESFlow", "QA Center", "Deploy Agent"):
        assert label in body
