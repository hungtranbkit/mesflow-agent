"""Build and verify a deterministic, secret-free Deploy Agent source ZIP."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
OUTPUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else (ROOT.parent / "artifacts" / "deploy-agent" / "source" / f"mesflow-deploy-agent-source-{VERSION}.zip")
PREFIX = f"mesflow-deploy-agent-source-{VERSION}"
EXCLUDED_DIRS = {".git", ".venv", ".test-venv", ".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__", "node_modules", "dist", "build", "runtime", "artifacts", "logs", "tmp", "temp", "test-results", "playwright-report", "htmlcov"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3", ".tar", ".zip", ".coverage"}
SECRET_NAMES = {".env", "id_rsa", "id_ed25519", "credentials.json"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
REQUIRED = {"agent.py", "agent_backend/system_health.py", "deployment_platform.py", "VERSION.txt", "requirements.txt", "docker/Dockerfile", "tests/test_p0_hygiene.py", "tests/test_p1_deployment_platform.py"}
PRIVATE_KEY_MARKERS = (b"-----BEGIN PRIVATE KEY-----", b"-----BEGIN RSA PRIVATE KEY-----", b"-----BEGIN OPENSSH PRIVATE KEY-----")


def candidates() -> list[Path]:
    result: list[Path] = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if path.is_dir() or any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        # Historical one-off release checks at project root pin obsolete
        # versions.  Maintained tests live under tests/ and are the only test
        # suite shipped in a new source package.
        if len(rel.parts) == 1 and path.name.startswith("test_") and path.suffix == ".py":
            continue
        # These are local deployment/recovery state snapshots, not source.
        if rel.parts[:2] == ("docker", "recovery") or (rel.parent == Path("docker") and path.name.startswith("promotion-state.")):
            continue
        if path.name in SECRET_NAMES or path.name.startswith(".env.") or path.suffix.lower() in SECRET_SUFFIXES:
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES or path.name in {".DS_Store"}:
            continue
        result.append(path)
    return sorted(result, key=lambda item: item.relative_to(ROOT).as_posix())


def verify_names(names: set[str]) -> None:
    missing = REQUIRED - names
    if missing:
        raise SystemExit(f"SOURCE_ZIP_MISSING_REQUIRED: {sorted(missing)}")
    forbidden = [name for name in names if any(part in EXCLUDED_DIRS for part in Path(name).parts) or Path(name).name in SECRET_NAMES or Path(name).suffix.lower() in EXCLUDED_SUFFIXES | SECRET_SUFFIXES]
    if forbidden:
        raise SystemExit(f"SOURCE_ZIP_FORBIDDEN_FILES: {forbidden[:10]}")
    historical_tests = [name for name in names if "/" not in name and name.startswith("test_") and name.endswith(".py")]
    if historical_tests:
        raise SystemExit(f"SOURCE_ZIP_STALE_ROOT_TESTS: {historical_tests}")


def verify_secret_content(files: list[Path]) -> None:
    """Fail closed for private-key material without printing its contents."""
    contaminated = []
    for path in files:
        # This verifier necessarily contains the marker literals it scans for.
        if path.resolve() == Path(__file__).resolve():
            continue
        data = path.read_bytes()
        if any(marker in data for marker in PRIVATE_KEY_MARKERS):
            contaminated.append(path.relative_to(ROOT).as_posix())
    if contaminated:
        raise SystemExit(f"SOURCE_ZIP_SECRET_MATERIAL: {contaminated}")


def main() -> None:
    files = candidates()
    verify_secret_content(files)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            rel = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(f"{PREFIX}/{rel}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if os.access(path, os.X_OK) else 0o644) << 16
            archive.writestr(info, path.read_bytes())
    with zipfile.ZipFile(OUTPUT) as archive:
        names = {name.removeprefix(f"{PREFIX}/") for name in archive.namelist() if not name.endswith("/")}
        verify_names(names)
    digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    result = {"version": VERSION, "filename": OUTPUT.name, "file_count": len(files), "size": OUTPUT.stat().st_size, "sha256": digest, "status": "PASS"}
    OUTPUT.with_suffix(OUTPUT.suffix + ".sha256").write_text(f"{digest}  {OUTPUT.name}\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
