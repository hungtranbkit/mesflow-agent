"""V70 machine-readable release gate and append-only evidence packs.

This module intentionally has no Flask or shell-command dependency.  Deploy Agent
adapters persist facts here; the policy computes readiness from those facts.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import re
import threading
from typing import Any


class RunStatus(str, Enum):
    RUNNING="RUNNING"; PASS="PASS"; FAIL="FAIL"; BLOCKED="BLOCKED"; ERROR="ERROR"; CANCELLED="CANCELLED"


class GateDecision(str, Enum):
    BUILT="BUILT"; LOCAL_FAILED="LOCAL_FAILED"; LOCAL_PASS="LOCAL_PASS"
    TEST_DEPLOY_FAILED="TEST_DEPLOY_FAILED"; TEST_FAILED="TEST_FAILED"; TEST_PASS="TEST_PASS"
    EVIDENCE_INCOMPLETE="EVIDENCE_INCOMPLETE"; READY_FOR_PRODUCTION_APPROVAL="READY_FOR_PRODUCTION_APPROVAL"


@dataclass(frozen=True)
class ReleaseIdentity:
    version: str
    source_commit: str
    artifact: str
    artifact_digest: str
    package_sha256: str
    schema_revision: str

    def validate(self) -> None:
        if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}", self.version): raise ValueError("INVALID_RELEASE_VERSION")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.artifact_digest): raise ValueError("INVALID_ARTIFACT_DIGEST")
        if not re.fullmatch(r"[0-9a-f]{64}", self.package_sha256): raise ValueError("INVALID_PACKAGE_SHA256")
        if not self.source_commit or not self.artifact or not self.schema_revision: raise ValueError("INCOMPLETE_RELEASE_IDENTITY")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    required: bool=True
    message: str=""
    metadata: dict[str, Any]=field(default_factory=dict)


@dataclass(frozen=True)
class TestRun:
    __test__=False
    run_id: str
    environment: str
    profile: str
    status: str
    artifact_digest: str
    started_at: str
    finished_at: str
    checks: tuple[CheckResult, ...]
    total_tests: int=0
    passed_tests: int=0
    failed_tests: int=0
    skipped_tests: int=0
    target_url: str=""
    report_path: str=""
    log_path: str=""


class ReleaseGatePolicy:
    """Pure policy. PASS is always bound to the candidate digest."""
    @staticmethod
    def valid_run(identity: ReleaseIdentity, run: TestRun|None, environment: str) -> tuple[bool,list[str]]:
        reasons=[]
        if not run: return False,[f"{environment}_RUN_MISSING"]
        if run.environment != environment: reasons.append(f"{environment}_ENVIRONMENT_MISMATCH")
        if run.artifact_digest != identity.artifact_digest: reasons.append("ARTIFACT_MISMATCH")
        if run.status != RunStatus.PASS.value: reasons.append(f"{environment}_{run.status}")
        for check in run.checks:
            if check.required and check.status != RunStatus.PASS.value: reasons.append(f"REQUIRED_CHECK_FAILED:{check.name}")
        return not reasons,reasons

    @classmethod
    def evaluate(cls, identity: ReleaseIdentity, *, build_status: str, local_run: TestRun|None,
                 test_deployment: dict[str,Any]|None, test_run: TestRun|None,
                 known_issues: list[dict[str,Any]], evidence_complete: bool) -> dict[str,Any]:
        identity.validate(); reasons=[]
        if build_status != "PASS": return {"decision":"BUILD_FAILED","ready":False,"reasons":["BUILD_NOT_PASS"]}
        local_ok,local_reasons=cls.valid_run(identity,local_run,"LOCAL")
        if not local_ok: return {"decision":GateDecision.LOCAL_FAILED.value,"ready":False,"reasons":local_reasons}
        if not test_deployment or test_deployment.get("status") != "PASS":
            return {"decision":GateDecision.TEST_DEPLOY_FAILED.value,"ready":False,"reasons":["TEST_DEPLOY_NOT_PASS"]}
        if test_deployment.get("deployed_digest") != identity.artifact_digest:
            return {"decision":GateDecision.TEST_DEPLOY_FAILED.value,"ready":False,"reasons":["ARTIFACT_MISMATCH"]}
        test_ok,test_reasons=cls.valid_run(identity,test_run,"TEST")
        if not test_ok: return {"decision":GateDecision.TEST_FAILED.value,"ready":False,"reasons":test_reasons}
        blockers=[i for i in known_issues if i.get("blocking") and not i.get("resolved_at")]
        if blockers: reasons.append(f"BLOCKING_KNOWN_ISSUES:{len(blockers)}")
        if not evidence_complete: reasons.append("EVIDENCE_INCOMPLETE")
        return {"decision":GateDecision.READY_FOR_PRODUCTION_APPROVAL.value if not reasons else GateDecision.EVIDENCE_INCOMPLETE.value,
                "ready":not reasons,"reasons":reasons}


class ReleaseEvidenceStore:
    """Append-only runs plus regenerated summary. Atomic writes, no run overwrite."""
    _lock=threading.RLock()
    def __init__(self, release_dir: Path): self.root=Path(release_dir)/"evidence"; self.runs=self.root/"runs"
    @staticmethod
    def _write_new(path:Path,payload:dict[str,Any]) -> None:
        if path.exists(): raise FileExistsError(f"EVIDENCE_RUN_EXISTS:{path.name}")
        path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(path.suffix+".tmp")
        tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8");tmp.replace(path)
    def append_run(self, run:TestRun) -> Path:
        with self._lock:
            path=self.runs/f"{run.run_id}.json";self._write_new(path,asdict(run));return path
    def list_runs(self) -> list[dict[str,Any]]:
        out=[]
        for path in sorted(self.runs.glob("*.json")) if self.runs.exists() else []:
            try: out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError,json.JSONDecodeError): continue
        return out
    def write_summary(self,payload:dict[str,Any]) -> dict[str,str]:
        """Mutable projection; its SHA makes silent modification detectable."""
        self.root.mkdir(parents=True,exist_ok=True)
        json_path=self.root/"release-evidence.json";md_path=self.root/"release-evidence.md"
        data=json.dumps(payload,ensure_ascii=False,indent=2,sort_keys=True)+"\n";json_path.write_text(data,encoding="utf-8")
        release=payload["release"];decision=payload["decision"]
        md=(f"# MESFlow {release['version']} — Release Evidence\n\n"
            f"- Commit: `{release['source_commit']}`\n- Artifact: `{release['artifact']}`\n"
            f"- Digest: `{release['artifact_digest']}`\n- Build: **{payload['build']['status']}**\n"
            f"- LOCAL: **{payload['local']['status']}**\n- TEST deploy: **{payload['test_deployment']['status']}**\n"
            f"- TEST QA: **{payload['test']['status']}**\n- Known blocking issues: **{payload['known_blocking_issues']}**\n"
            f"- Decision: **{decision['decision']}**\n")
        md_path.write_text(md,encoding="utf-8")
        digest=sha256(data.encode()).hexdigest();(self.root/"release-evidence.json.sha256").write_text(f"{digest}  release-evidence.json\n",encoding="utf-8")
        return {"json":str(json_path),"markdown":str(md_path),"sha256":digest}


def utc_now() -> str: return datetime.now(timezone.utc).isoformat()
