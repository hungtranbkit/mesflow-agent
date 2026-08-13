#!/usr/bin/env python3
"""MESFlow Deploy Agent host-side updater.

Runs as its own systemd service, entirely independent of the
mesflow-deploy-agent container it manages -- so it survives an in-progress
or failed Agent update instead of taking down its own recovery mechanism.
Stdlib only (no pip install, no venv) so it stays minimal and stable.

It NEVER touches mesflow-app, PostgreSQL, QA Center, or nginx: every
mutating action is `docker compose ... up -d --force-recreate
mesflow-deploy-agent`, scoped to that one service name, against the
already-deployed compose.yml under MESFLOW_AGENT_TARGET_DIR.

Endpoints:
  GET  /updater/health              no auth; liveness only
  GET  /updater/status              auth required; current Agent image/version/role
  POST /updater/update              auth required; body = the AGENT_UPDATE_<version>.zip
                                     bytes (Content-Type: application/zip)

Normal operation needs no SSH: DEV pushes the update artifact to this
service over HTTP with a bearer token. SSH remains available separately
for first bootstrap / offline recovery / diagnostics / emergency rollback.
"""
from __future__ import annotations
import hashlib, hmac, http.server, json, os, shutil, subprocess, sys, tempfile, time, zipfile
from pathlib import Path

UPDATER_HOME = Path(__file__).resolve().parent
TARGET_DIR = Path(os.environ.get("MESFLOW_AGENT_TARGET_DIR", "/opt/mesflow-deploy-agent"))
PORT = int(os.environ.get("MESFLOW_AGENT_UPDATER_PORT", "8099"))
BIND = os.environ.get("MESFLOW_AGENT_UPDATER_HOST", "127.0.0.1")
STATE_DIR = UPDATER_HOME / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = STATE_DIR / "updater.log"
UPDATER_VERSION = "1.0.0"
HEALTH_POLL_TIMEOUT = int(os.environ.get("MESFLOW_AGENT_UPDATE_HEALTH_TIMEOUT", "60"))
# Target layout is normally a single compose.yml + .env under TARGET_DIR
# (application code lives only in the image). During the transition off
# the older layered docker/compose.linux.yml(+overrides) layout, both the
# compose file list and the env file location are configurable so this one
# updater works against either without a forced live restructuring.
COMPOSE_FILES = [f.strip() for f in os.environ.get("MESFLOW_AGENT_COMPOSE_FILES", "compose.yml").split(":") if f.strip()]
ENV_FILE = Path(os.environ.get("MESFLOW_AGENT_ENV_FILE", str(TARGET_DIR / ".env")))


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_token() -> str:
    env_file = UPDATER_HOME / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("MESFLOW_AGENT_UPDATER_TOKEN="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("MESFLOW_AGENT_UPDATER_TOKEN", "").strip()


def run(cmd, timeout=180, cwd=None):
    r = subprocess.run(cmd, cwd=cwd or str(TARGET_DIR), capture_output=True, text=True, timeout=timeout)
    log(f"RUN {' '.join(cmd)} rc={r.returncode}")
    if r.stdout.strip():
        log("STDOUT: " + r.stdout.strip()[-4000:])
    if r.stderr.strip():
        log("STDERR: " + r.stderr.strip()[-4000:])
    return r


def compose_env() -> dict:
    data = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
    return data


def write_compose_env(data: dict) -> None:
    ENV_FILE.write_text("\n".join(f"{k}={v}" for k, v in data.items()) + "\n", encoding="utf-8")


def current_image_ref() -> str:
    return compose_env().get("AGENT_IMAGE", "")


def compose_up(image_ref: str, timeout=120):
    data = compose_env()
    data["AGENT_IMAGE"] = image_ref
    write_compose_env(data)
    file_args = []
    for f in COMPOSE_FILES:
        file_args += ["-f", f]
    return run(["docker", "compose", "--env-file", str(ENV_FILE), *file_args,
                "up", "-d", "--no-build", "--force-recreate", "mesflow-deploy-agent"], timeout=timeout)


def poll_health(expected_version: str, expected_role: str, timeout=None):
    # Read HEALTH_POLL_TIMEOUT fresh rather than binding it as a default
    # argument value: a default is evaluated once at import time, which
    # would ignore any later reconfiguration (including in tests).
    if timeout is None:
        timeout = HEALTH_POLL_TIMEOUT
    deadline = time.time() + timeout
    env = compose_env()
    bind = env.get("AGENT_BIND_IP", "127.0.0.1")
    port = env.get("AGENT_PORT", "8090")
    last = {}
    while time.time() < deadline:
        time.sleep(2)
        r = run(["curl", "-fsS", f"http://{bind}:{port}/agent/health"], timeout=10)
        if r.returncode != 0 or not r.stdout.strip():
            continue
        try:
            payload = json.loads(r.stdout)
        except json.JSONDecodeError:
            continue
        last = payload
        if payload.get("agent_version") == expected_version and payload.get("server_role") == expected_role:
            return True, payload
    return False, last


def extract_and_verify_artifact(zip_bytes: bytes, workdir: Path):
    zip_path = workdir / "update.zip"
    zip_path.write_bytes(zip_bytes)
    extract = workdir / "extracted"
    extract.mkdir()
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if not names or any(n.startswith("/") or ".." in n for n in names):
            raise ValueError("ZIP contains unsafe paths")
        z.extractall(extract)
    roots = [p for p in extract.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise ValueError("Update package must contain exactly one root directory")
    root = roots[0]
    manifest_path = root / "agent-release.json"
    checksums_path = root / "checksums.txt"
    if not manifest_path.is_file() or not checksums_path.is_file():
        raise ValueError("Update package missing agent-release.json or checksums.txt")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("type") != "mesflow-agent-release":
        raise ValueError("agent-release.json type must be mesflow-agent-release")
    # Verify every checksum line against the actual extracted bytes.
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        expected_hash, _, fname = line.partition("  ")
        fpath = root / fname.strip()
        if not fpath.is_file():
            raise ValueError(f"Checksum references missing file: {fname}")
        actual = hashlib.sha256(fpath.read_bytes()).hexdigest()
        if actual != expected_hash.strip():
            raise ValueError(f"Checksum mismatch for {fname}: expected {expected_hash}, got {actual}")
    bundle = root / str(manifest.get("bundle") or "")
    if not bundle.is_file():
        raise ValueError("Update package missing its image bundle tar")
    return manifest, bundle


def do_update(zip_bytes: bytes) -> dict:
    # SERVER_ROLE/MESFLOW_BUILD_ENABLED are never touched by this updater --
    # only AGENT_IMAGE changes in .env, so role/build-enabled are preserved
    # by construction. "Role compatibility" is enforced by verifying the
    # freshly-updated Agent still reports the SAME role it had before.
    prev_image_ref = current_image_ref()
    prev_role = compose_env().get("SERVER_ROLE", "")
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        manifest, bundle = extract_and_verify_artifact(zip_bytes, workdir)
        new_version = str(manifest.get("version") or "")
        expected_image_id = str(manifest.get("image_id") or "")
        if not new_version or not expected_image_id.startswith("sha256:"):
            raise ValueError("agent-release.json missing version or a valid image_id")

        log(f"UPDATE START from={prev_image_ref or 'unknown'} to={new_version}")
        load_r = run(["docker", "load", "-i", str(bundle)], timeout=300)
        if load_r.returncode != 0:
            raise ValueError("docker load failed")
        image = str(manifest.get("image") or "")
        # docker load already tagged the image with the tag baked into the
        # tar by `docker save` on DEV -- verify by id, matching how MESFlow
        # image releases are verified (never trust the tag alone).
        inspect_r = run(["docker", "image", "inspect", image, "--format", "{{.Id}}"], timeout=30)
        actual_id = inspect_r.stdout.strip()
        if inspect_r.returncode != 0 or actual_id != expected_image_id:
            raise ValueError(f"IMAGE_ID_MISMATCH expected={expected_image_id} actual={actual_id or 'unavailable'}")

        up_r = compose_up(image)
        if up_r.returncode != 0:
            return _rollback(prev_image_ref, prev_role, "COMPOSE_UP_FAILED")

        healthy, payload = poll_health(new_version, prev_role)
        if not healthy:
            return _rollback(prev_image_ref, prev_role, f"HEALTH_CHECK_FAILED last={payload}")

        log(f"UPDATE SUCCESS to={new_version}")
        return {"status": "SUCCESS", "from_image": prev_image_ref, "to_version": new_version,
                "to_image_id": actual_id, "health": payload}


def _rollback(prev_image_ref: str, prev_role: str, reason: str) -> dict:
    log(f"ROLLBACK START reason={reason} restoring={prev_image_ref}")
    if not prev_image_ref:
        log("ROLLBACK FAILED: no previous image reference recorded")
        return {"status": "ROLLBACK_FAILED", "reason": reason, "detail": "no previous AGENT_IMAGE recorded"}
    up_r = compose_up(prev_image_ref)
    if up_r.returncode != 0:
        log("ROLLBACK FAILED: compose up of previous image failed")
        return {"status": "ROLLBACK_FAILED", "reason": reason, "detail": "compose up of previous image failed"}
    # We don't know the previous exact AGENT_VERSION string here (only its
    # image ref), so verify by role + reachability rather than exact version.
    deadline = time.time() + HEALTH_POLL_TIMEOUT
    env = compose_env()
    bind = env.get("AGENT_BIND_IP", "127.0.0.1"); port = env.get("AGENT_PORT", "8090")
    last = {}
    while time.time() < deadline:
        time.sleep(2)
        r = run(["curl", "-fsS", f"http://{bind}:{port}/agent/health"], timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            try:
                last = json.loads(r.stdout)
            except json.JSONDecodeError:
                continue
            if last.get("server_role") == prev_role:
                log("ROLLBACK SUCCESS: previous Agent healthy again")
                return {"status": "ROLLED_BACK", "reason": reason, "restored_image": prev_image_ref, "health": last}
    log(f"ROLLBACK FAILED: previous Agent did not become healthy again; last={last}")
    return {"status": "ROLLBACK_FAILED", "reason": reason, "detail": f"previous image restored but not healthy: {last}"}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        token = load_token()
        if not token:
            return False
        supplied = self.headers.get("Authorization", "")
        if not supplied.lower().startswith("bearer "):
            return False
        return hmac.compare_digest(supplied[7:].strip(), token)

    def do_GET(self):
        if self.path == "/updater/health":
            self._json(200, {"ok": True, "updater_version": UPDATER_VERSION})
            return
        if self.path == "/updater/status":
            if not self._authed():
                self._json(401, {"ok": False, "error": "AUTH_REQUIRED"})
                return
            env = compose_env()
            self._json(200, {"ok": True, "target_dir": str(TARGET_DIR), "agent_image": env.get("AGENT_IMAGE", ""),
                              "server_role": env.get("SERVER_ROLE", ""), "build_enabled": env.get("MESFLOW_BUILD_ENABLED", "")})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/updater/update":
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._authed():
            self._json(401, {"ok": False, "error": "AUTH_REQUIRED"})
            return
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 1024 * 1024 * 1024:
            self._json(400, {"ok": False, "error": "INVALID_CONTENT_LENGTH"})
            return
        body = self.rfile.read(length)
        try:
            result = do_update(body)
        except Exception as ex:
            log(f"UPDATE FAILED before mutation (safe to retry): {type(ex).__name__}: {ex}")
            self._json(409, {"ok": False, "error": f"{type(ex).__name__}: {ex}"})
            return
        code = 200 if result.get("status") == "SUCCESS" else 200 if result.get("status") == "ROLLED_BACK" else 500
        self._json(code, {"ok": result.get("status") == "SUCCESS", **result})


def main():
    if not load_token():
        log("WARNING: no MESFLOW_AGENT_UPDATER_TOKEN configured in .env -- all authenticated requests will be refused")
    server = http.server.ThreadingHTTPServer((BIND, PORT), Handler)
    log(f"mesflow-agent-updater {UPDATER_VERSION} listening on {BIND}:{PORT}, target={TARGET_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
