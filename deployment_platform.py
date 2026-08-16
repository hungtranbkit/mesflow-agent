"""Common deployment orchestration for MESFlow ecosystem components.

The component adapters retain their existing low-level mechanics.  This
module owns the shared invariants: explicit targets, immutable identity,
idempotency, per-target locks, evidence, health/version verification,
rollback classification and retention policy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeploymentComponent(str, Enum):
    MESFLOW = "MESFLOW"
    QA_CENTER = "QA_CENTER"
    DEPLOY_AGENT = "DEPLOY_AGENT"


class DeploymentEnvironment(str, Enum):
    LOCAL = "LOCAL"
    TEST = "TEST"
    PRODUCTION = "PRODUCTION"


class FailureCategory(str, Enum):
    PRECHECK_FAILED = "PRECHECK_FAILED"
    ARTIFACT_INVALID = "ARTIFACT_INVALID"
    TARGET_UNREACHABLE = "TARGET_UNREACHABLE"
    STAGE_FAILED = "STAGE_FAILED"
    ACTIVATION_FAILED = "ACTIVATION_FAILED"
    MIGRATION_FAILED = "MIGRATION_FAILED"
    HEALTH_FAILED = "HEALTH_FAILED"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    DIGEST_MISMATCH = "DIGEST_MISMATCH"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    LOCKED = "LOCKED"


@dataclass(frozen=True)
class ReleaseIdentity:
    component: DeploymentComponent
    version: str
    release_id: str
    source_commit: str
    artifact_name: str
    artifact_path: str
    artifact_digest: str
    built_at: str
    image_digest: str = ""
    migration_class: str = "NONE"

    def validate(self) -> None:
        if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,127}", self.version):
            raise ValueError("INVALID_VERSION")
        if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._:-]{0,191}", self.release_id):
            raise ValueError("INVALID_RELEASE_ID")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.artifact_digest):
            raise ValueError("INVALID_ARTIFACT_DIGEST")
        if self.image_digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_digest):
            raise ValueError("INVALID_IMAGE_DIGEST")
        if self.migration_class not in {"NONE", "ADDITIVE", "ROLLBACK_SENSITIVE"}:
            raise ValueError("INVALID_MIGRATION_CLASS")
        if not self.source_commit or not self.artifact_name or not self.built_at:
            raise ValueError("INCOMPLETE_RELEASE_IDENTITY")

    def verify_file(self) -> None:
        path = Path(self.artifact_path)
        if not path.is_file():
            raise ValueError("ARTIFACT_MISSING")
        actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != self.artifact_digest:
            raise ValueError("ARTIFACT_DIGEST_MISMATCH")


@dataclass(frozen=True)
class DeploymentTarget:
    environment: DeploymentEnvironment
    name: str
    health_url: str
    deployment_path: str
    service_name: str
    production_mutation_allowed: bool = False

    def validate(self) -> None:
        if not self.name or not self.health_url or not self.service_name:
            raise ValueError("TARGET_INCOMPLETE")
        if self.environment is DeploymentEnvironment.PRODUCTION and self.production_mutation_allowed:
            # This flag means the target supports production actions; each run
            # still requires its own explicit approval.
            return


@dataclass
class DeploymentStep:
    name: str
    status: str
    at: str
    message: str = ""


@dataclass
class DeploymentRecord:
    deployment_id: str
    request_id: str
    correlation_id: str
    component: str
    environment: str
    release_id: str
    from_version: str
    to_version: str
    artifact_digest: str
    actor: str
    started_at: str
    operation: str = "DEPLOY"
    finished_at: str = ""
    status: str = "DEPLOYING"
    health_result: str = "NOT_RUN"
    rollback_status: str = "NOT_REQUIRED"
    running_version: str = ""
    failure_category: str = ""
    message: str = ""
    steps: list[DeploymentStep] = field(default_factory=list)

    def step(self, name: str, status: str, message: str = "") -> None:
        self.steps.append(DeploymentStep(name, status, utc_now(), message[:1000]))


class DeploymentAdapter(Protocol):
    def current(self, target: DeploymentTarget) -> dict[str, Any]: ...
    def precheck(self, release: ReleaseIdentity, target: DeploymentTarget) -> dict[str, Any]: ...
    def stage(self, release: ReleaseIdentity, target: DeploymentTarget, deployment_id: str) -> None: ...
    def activate(self, release: ReleaseIdentity, target: DeploymentTarget, deployment_id: str) -> None: ...
    def verify(self, release: ReleaseIdentity, target: DeploymentTarget) -> dict[str, Any]: ...
    def rollback(self, previous: dict[str, Any], target: DeploymentTarget, deployment_id: str) -> None: ...


class DeploymentEvidenceStore:
    """Append-only deployment evidence with an atomic index projection."""
    _lock = threading.RLock()

    def __init__(self, root: Path):
        self.root = Path(root)
        self.runs = self.root / "runs"

    def append(self, record: DeploymentRecord) -> Path:
        with self._lock:
            self.runs.mkdir(parents=True, exist_ok=True)
            path = self.runs / f"{record.deployment_id}.json"
            if path.exists():
                raise FileExistsError("DEPLOYMENT_EVIDENCE_EXISTS")
            payload = asdict(record)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
            return path

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(self.runs.glob("*.json"), reverse=True) if self.runs.exists() else []:
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        rows.sort(key=lambda row: row.get("started_at", ""), reverse=True)
        return rows[: max(1, min(limit, 500))]

    def by_request(self, component: DeploymentComponent, environment: DeploymentEnvironment, request_id: str) -> dict[str, Any] | None:
        return next((row for row in self.list(500) if row.get("component") == component.value and row.get("environment") == environment.value and row.get("request_id") == request_id), None)


class DeploymentService:
    _locks_guard = threading.Lock()
    _locks: dict[tuple[str, str], threading.Lock] = {}

    def __init__(self, adapters: dict[DeploymentComponent, DeploymentAdapter], evidence: DeploymentEvidenceStore):
        self.adapters = adapters
        self.evidence = evidence

    @classmethod
    def _target_lock(cls, component: DeploymentComponent, environment: DeploymentEnvironment) -> threading.Lock:
        key = (component.value, environment.value)
        with cls._locks_guard:
            return cls._locks.setdefault(key, threading.Lock())

    def deploy(self, release: ReleaseIdentity, target: DeploymentTarget, *, actor: str, request_id: str, correlation_id: str = "", production_approved: bool = False, operation: str = "DEPLOY") -> DeploymentRecord:
        release.validate()
        target.validate()
        if release.component not in self.adapters:
            raise ValueError("COMPONENT_ADAPTER_MISSING")
        if target.environment is DeploymentEnvironment.PRODUCTION and not (target.production_mutation_allowed and production_approved):
            raise PermissionError("PRODUCTION_APPROVAL_REQUIRED")
        existing = self.evidence.by_request(release.component, target.environment, request_id)
        if existing:
            return DeploymentRecord(**{**existing, "steps": [DeploymentStep(**step) for step in existing.get("steps", [])]})
        lock = self._target_lock(release.component, target.environment)
        if not lock.acquire(blocking=False):
            raise RuntimeError(FailureCategory.LOCKED.value)
        adapter = self.adapters[release.component]
        record = DeploymentRecord(
            deployment_id=uuid.uuid4().hex, request_id=request_id, correlation_id=correlation_id or uuid.uuid4().hex,
            component=release.component.value, environment=target.environment.value, release_id=release.release_id,
            from_version="", to_version=release.version, artifact_digest=release.artifact_digest,
            actor=actor, started_at=utc_now(), operation=operation,
        )
        previous: dict[str, Any] = {}
        try:
            try:
                release.verify_file()
            except ValueError as exc:
                return self._finish_failure(record, FailureCategory.ARTIFACT_INVALID, str(exc))
            previous = adapter.current(target)
            record.from_version = str(previous.get("version") or "UNKNOWN")
            record.step("PRECHECK", "RUNNING")
            precheck = adapter.precheck(release, target)
            if not precheck.get("ok"):
                return self._finish_failure(record, FailureCategory.PRECHECK_FAILED, str(precheck.get("reason") or "PRECHECK_FAILED"))
            if release.component is DeploymentComponent.MESFLOW and release.migration_class == "ROLLBACK_SENSITIVE" and not precheck.get("rollback_plan"):
                return self._finish_failure(record, FailureCategory.PRECHECK_FAILED, "ROLLBACK_PLAN_REQUIRED")
            record.step("PRECHECK", "PASS", "artifact/target/current health captured")
            record.step("STAGE", "RUNNING")
            adapter.stage(release, target, record.deployment_id)
            record.step("STAGE", "PASS")
            record.step("ACTIVATE", "RUNNING")
            adapter.activate(release, target, record.deployment_id)
            record.step("ACTIVATE", "PASS")
            record.step("HEALTH", "RUNNING")
            checks = [adapter.verify(release, target), adapter.verify(release, target)]
            failed = next((check for check in checks if not check.get("healthy")), None)
            if failed:
                return self._rollback_after_failure(record, adapter, target, previous, FailureCategory.HEALTH_FAILED, str(failed.get("reason") or "HEALTH_FAILED"))
            mismatch = next((check for check in checks if str(check.get("version")) != release.version), None)
            if mismatch:
                return self._rollback_after_failure(record, adapter, target, previous, FailureCategory.VERSION_MISMATCH, "RUNNING_VERSION_MISMATCH")
            digest_mismatch = next((check for check in checks if check.get("digest") and check.get("digest") != (release.image_digest or release.artifact_digest)), None)
            if digest_mismatch:
                return self._rollback_after_failure(record, adapter, target, previous, FailureCategory.DIGEST_MISMATCH, "RUNNING_DIGEST_MISMATCH")
            record.step("HEALTH", "PASS", "two bounded health/version checks passed")
            record.status = "ROLLED_BACK" if operation == "MANUAL_ROLLBACK" else "HEALTHY"
            record.health_result = "PASS"
            if operation == "MANUAL_ROLLBACK":
                record.rollback_status = "MANUAL_SUCCESS"
            record.running_version = release.version
            record.finished_at = utc_now()
            record.message = "DEPLOYMENT_VERIFIED"
            self.evidence.append(record)
            return record
        except Exception as exc:
            category = FailureCategory.ACTIVATION_FAILED if any(step.name == "ACTIVATE" for step in record.steps) else FailureCategory.STAGE_FAILED
            return self._rollback_after_failure(record, adapter, target, previous, category, f"{type(exc).__name__}: {exc}") if previous else self._finish_failure(record, category, str(exc))
        finally:
            lock.release()

    def manual_rollback(self, release: ReleaseIdentity, target: DeploymentTarget, *, actor: str, request_id: str, correlation_id: str = "", production_approved: bool = False) -> DeploymentRecord:
        if release.component is DeploymentComponent.MESFLOW and release.migration_class == "ROLLBACK_SENSITIVE":
            raise ValueError("ROLLBACK_SENSITIVE_RELEASE_REQUIRES_MANUAL_DB_RECOVERY_PLAN")
        return self.deploy(release, target, actor=actor, request_id=request_id, correlation_id=correlation_id,
                           production_approved=production_approved, operation="MANUAL_ROLLBACK")

    def _finish_failure(self, record: DeploymentRecord, category: FailureCategory, message: str) -> DeploymentRecord:
        record.status = "DEPLOY_FAILED"
        record.failure_category = category.value
        record.message = message[:1000]
        record.finished_at = utc_now()
        record.step("FAILED", "FAIL", record.message)
        self.evidence.append(record)
        return record

    def _rollback_after_failure(self, record: DeploymentRecord, adapter: DeploymentAdapter, target: DeploymentTarget, previous: dict[str, Any], category: FailureCategory, message: str) -> DeploymentRecord:
        record.failure_category = category.value
        record.health_result = "FAIL"
        record.step("ROLLBACK", "RUNNING", message)
        try:
            adapter.rollback(previous, target, record.deployment_id)
            verification = adapter.current(target)
            if not verification.get("healthy") or str(verification.get("version")) != str(previous.get("version")):
                raise RuntimeError("ROLLBACK_HEALTH_OR_VERSION_MISMATCH")
            record.rollback_status = "SUCCESS"
            record.running_version = str(previous.get("version") or "")
            record.step("ROLLBACK", "PASS", f"restored {record.running_version}")
        except Exception as exc:
            record.rollback_status = "FAILED"
            record.failure_category = FailureCategory.ROLLBACK_FAILED.value
            record.step("ROLLBACK", "FAIL", str(exc))
        record.status = "DEPLOY_FAILED"
        record.message = message[:1000]
        record.finished_at = utc_now()
        self.evidence.append(record)
        return record


@dataclass(frozen=True)
class RetentionPolicy:
    keep_last_successful: int = 5
    keep_current: bool = True
    keep_previous: bool = True
    keep_failed_days: int = 14


class RetentionService:
    def __init__(self, root: Path, policy: RetentionPolicy):
        self.root = Path(root).resolve()
        self.policy = policy

    def preview(self, releases: list[dict[str, Any]]) -> dict[str, Any]:
        successful = sorted((row for row in releases if row.get("status") == "SUCCESS"), key=lambda row: row.get("built_at", ""), reverse=True)
        recent = {str(row.get("version")) for row in successful[: max(0, self.policy.keep_last_successful)]}
        retained, eligible = [], []
        for row in releases:
            reasons = []
            version = str(row.get("version") or "")
            if self.policy.keep_current and row.get("current"): reasons.append("CURRENT")
            if self.policy.keep_previous and row.get("previous"): reasons.append("PREVIOUS_ROLLBACK")
            if row.get("pinned"): reasons.append("PINNED")
            if row.get("active_deployment"): reasons.append("ACTIVE_DEPLOYMENT")
            if row.get("evidence_protected"): reasons.append("EVIDENCE_PROTECTED")
            if version in recent: reasons.append("RECENT_SUCCESS")
            item = {**row, "reasons": reasons}
            (retained if reasons else eligible).append(item)
        return {
            "dry_run": True,
            "policy": asdict(self.policy),
            "retained": retained,
            "eligible": eligible,
            "reclaimable_bytes": sum(int(row.get("size_bytes") or 0) for row in eligible),
        }

    def apply(self, preview: dict[str, Any]) -> dict[str, Any]:
        removed, bytes_reclaimed = [], 0
        for row in preview.get("eligible", []):
            path = Path(str(row.get("path") or "")).resolve()
            if path.parent != self.root or path.is_symlink() or not path.is_dir():
                raise ValueError("UNSAFE_RETENTION_TARGET")
            size = int(row.get("size_bytes") or 0)
            shutil.rmtree(path)
            removed.append(str(row.get("version") or path.name))
            bytes_reclaimed += size
        return {"removed": removed, "bytes_reclaimed": bytes_reclaimed, "at": utc_now()}
