from __future__ import annotations
import hashlib, hmac, http.cookiejar, io, json, os, secrets, shutil, socket, ssl, subprocess, sys, threading, time, urllib.request, urllib.error, uuid, zipfile, platform, re, tempfile
from urllib.parse import quote, urlencode
import psutil
import paramiko
from datetime import datetime
from pathlib import Path
from typing import Any
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for, send_file
from waitress import serve
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
import ota_control as ota

AGENT_VERSION="2.17.1-docker-runtime"
AGENT_HOME=Path(os.environ.get("WORKSHOP_AGENT_HOME","/opt/mesflow-agent"))
TARGET_HOME=Path(os.environ.get("WORKSHOP_TARGET_HOME","/opt/mesflow"))
QA_HOME=Path(os.environ.get("MESFLOW_QA_HOME","/opt/mesflow-qa-center"))
HOST=os.environ.get("WORKSHOP_AGENT_HOST","0.0.0.0"); PORT=int(os.environ.get("WORKSHOP_AGENT_PORT","8090"))
MES_URL=os.environ.get("WORKSHOP_MES_URL","http://127.0.0.1:8080").rstrip("/")
QA_URL=os.environ.get("MESFLOW_QA_URL","http://mesflow-qa-center:8095").rstrip("/")
QA_PORT=int(os.environ.get("MESFLOW_QA_PORT","8095"))
QA_VERIFY_TIMEOUT=max(20,int(os.environ.get("MESFLOW_QA_VERIFY_TIMEOUT","60")))
RUNTIME_MODE=os.environ.get("MESFLOW_AGENT_RUNTIME","host").strip().lower() or "host"
DEPLOY_ENABLED=os.environ.get("MESFLOW_AGENT_DEPLOY_ENABLED","1").strip().lower() in {"1","true","yes","on"}
HOST_OPS_ENABLED=os.environ.get("MESFLOW_AGENT_HOST_OPS_ENABLED","1").strip().lower() in {"1","true","yes","on"}
MAX_UPLOAD_MB=int(os.environ.get("WORKSHOP_AGENT_MAX_UPLOAD_MB","1024")); URL_PREFIX=os.environ.get("WORKSHOP_AGENT_URL_PREFIX","/agent").rstrip("/")
MAX_EXTRACT_MB=int(os.environ.get("WORKSHOP_AGENT_MAX_EXTRACT_MB",str(MAX_UPLOAD_MB*4)))
MES_VERIFY_TIMEOUT=max(60,int(os.environ.get("WORKSHOP_MES_VERIFY_TIMEOUT","180")))
ZIP_EXTRACT_TIMEOUT=max(30,int(os.environ.get("WORKSHOP_AGENT_ZIP_EXTRACT_TIMEOUT","180")))
COMPOSE_VALIDATE_TIMEOUT=max(30,int(os.environ.get("WORKSHOP_AGENT_COMPOSE_VALIDATE_TIMEOUT","120")))
BUILD_TIMEOUT=max(300,int(os.environ.get("WORKSHOP_AGENT_BUILD_TIMEOUT","1800")))
IMAGE_RELEASE_TYPE="mesflow-image-release"
CONFIG_DIR=AGENT_HOME/"config"; DATA_DIR=AGENT_HOME/"data"; UPLOAD_DIR=DATA_DIR/"uploads"; RELEASES_DIR=DATA_DIR/"releases"; LOG_DIR=DATA_DIR/"logs"
STATE_FILE=DATA_DIR/"state.json"; CONFIG_FILE=CONFIG_DIR/"agent.json"; AGENT_LOG=LOG_DIR/"agent.log"
DEPLOY_TX_FILE=DATA_DIR/"deploy_transaction.json"; DEPLOY_LOCK_FILE=DATA_DIR/"deploy.lock"
QA_RELEASES_DIR=DATA_DIR/"qa_releases"; QA_UPLOAD_DIR=DATA_DIR/"qa_uploads"; QA_LOG=LOG_DIR/"qa_process.log"
ESP_TUTORIAL_UPLOAD_DIR=DATA_DIR/"esp_tutorial_uploads"
ESP_TUTORIAL_HOME=Path(os.environ.get("MESFLOW_ESP_TUTORIAL_HOME",str(TARGET_HOME/"runtime/tutorials/esp-kiosk")))
ESP_TUTORIAL_MAX_MB=max(32,int(os.environ.get("MESFLOW_ESP_TUTORIAL_MAX_MB","512")))
def _default_workspace_root() -> Path:
    """Resolve the MESFlow workspace root when no explicit env var is set.

    The Agent frequently runs as root (systemd `User=root`, or root inside
    the Docker image), where `Path.home()` resolves to `/root` and silently
    produces a non-existent `/root/workspace/mesflow` instead of the real
    invoking user's workspace (e.g. `/home/dell/workspace/mesflow`). Prefer,
    in order: an explicit override, the sudo-invoking user's home, the
    login-owner of the controlling terminal, then finally `Path.home()`.
    """
    override = os.environ.get("MESFLOW_WORKSPACE_ROOT")
    if override:
        return Path(override).expanduser()
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        candidate = Path(f"/home/{sudo_user}/workspace/mesflow")
        if candidate.exists():
            return candidate
    if os.geteuid() == 0:
        try:
            login = os.getlogin()
        except OSError:
            login = ""
        if login and login != "root":
            candidate = Path(f"/home/{login}/workspace/mesflow")
            if candidate.exists():
                return candidate
    return Path.home()/"workspace/mesflow"

WORKSPACE_ROOT=_default_workspace_root()
ESP_BUILD_ENABLED=os.environ.get("ESP_BUILD_ENABLED","1").strip().lower() in {"1","true","yes","on"}
ESP_KIOSK_SOURCE_DIR=Path(os.environ.get("ESP_KIOSK_SOURCE_DIR",str(WORKSPACE_ROOT/"esp-kiosk"))).expanduser()
ESP_BUILDER_LOCK=threading.Lock()
MESFLOW_SOURCE_DIR=Path(os.environ.get("MESFLOW_SOURCE_DIR",str(WORKSPACE_ROOT/"mesflow"))).expanduser()
MESFLOW_BUILD_ENABLED=os.environ.get("MESFLOW_BUILD_ENABLED","1").strip().lower() in {"1","true","yes","on"}
MESFLOW_BUILD_LOCK=threading.Lock()
SERVER_ROLE=os.environ.get("SERVER_ROLE","DEV").strip().upper()
for d in (CONFIG_DIR,UPLOAD_DIR,RELEASES_DIR,LOG_DIR,QA_RELEASES_DIR,QA_UPLOAD_DIR,ESP_TUTORIAL_UPLOAD_DIR): d.mkdir(parents=True,exist_ok=True)
app=Flask(__name__); app.config.update(
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB*1024*1024,
    APPLICATION_ROOT=URL_PREFIX or "/",
    SESSION_COOKIE_NAME="mesflow_agent_session",
    SESSION_COOKIE_PATH=f"{URL_PREFIX}/" if URL_PREFIX else "/",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

class DirectPrefixMiddleware:
    """Allow direct access at /agent while still supporting nginx X-Forwarded-Prefix."""
    def __init__(self, wrapped, prefix):
        self.wrapped=wrapped; self.prefix=(prefix or "").rstrip("/")
    def __call__(self, environ, start_response):
        path=environ.get("PATH_INFO","")
        if self.prefix and not environ.get("SCRIPT_NAME") and (path==self.prefix or path.startswith(self.prefix+"/")):
            environ["SCRIPT_NAME"]=self.prefix
            environ["PATH_INFO"]=path[len(self.prefix):] or "/"
        return self.wrapped(environ,start_response)

app.wsgi_app=DirectPrefixMiddleware(
    ProxyFix(app.wsgi_app,x_for=1,x_proto=1,x_host=1,x_port=1,x_prefix=1),
    URL_PREFIX,
)
_state_lock=threading.Lock(); _job_lock=threading.Lock(); _qa_job_lock=threading.Lock(); _tutorial_job_lock=threading.Lock(); _esp_tutorial_job_lock=threading.Lock()
MES_JOB_ACTIVE_STATES={"queued","uploading","validating","building","deploying","verifying"}
def now_text(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def display_datetime(value):
    try:return datetime.fromisoformat(str(value).replace("Z","+00:00")).strftime("%d/%m/%Y %H:%M")
    except (TypeError,ValueError):return str(value or "")
def load_json(p,default):
    try:return json.loads(Path(p).read_text(encoding="utf-8"))
    except (FileNotFoundError,json.JSONDecodeError,OSError):return default
def save_json(p,v):
    p=Path(p); t=p.with_suffix(p.suffix+".tmp"); t.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding="utf-8"); t.replace(p)
def append_log(m):
    line=f"[{now_text()}] {m}"; print(line,flush=True)
    with AGENT_LOG.open("a",encoding="utf-8") as f:f.write(line+"\n")

def esp_builder_job_update(status,message,**extra):
    def mutate(state):
        job=dict(state.get("esp_builder_job") or {})
        job.update(status=status,message=message,updated_at=now_text(),**extra)
        state["esp_builder_job"]=job
    return update_state(mutate).get("esp_builder_job")

def _register_built_ota_package(package):
    with zipfile.ZipFile(package) as archive:
        root="esp-kiosk-firmware/"
        manifest=json.loads(archive.read(root+"manifest.json").decode("utf-8"))
        version=str(manifest.get("version") or "").strip()
        hardware=str(manifest.get("board") or manifest.get("hardware_model") or "").strip()
        binary=archive.read(root+"firmware.bin")
    if not version or not hardware or not binary: raise ValueError("PACKAGE_FAILED")
    return ota._save_firmware(io.BytesIO(binary),f"ESP_Kiosk_{version}.bin",version,hardware)

def _run_esp_builder():
    try:
        if not ESP_BUILD_ENABLED: raise ValueError("ESP_BUILD_DISABLED")
        if not ESP_KIOSK_SOURCE_DIR.is_dir(): raise ValueError("SOURCE_NOT_FOUND")
        script=ESP_KIOSK_SOURCE_DIR/"scripts"/"build-ota-package.sh"
        if not script.is_file(): raise ValueError("BUILD_SCRIPT_NOT_FOUND")
        esp_builder_job_update("CHECKING","Checking source and Arduino build configuration",source_path=str(ESP_KIOSK_SOURCE_DIR))
        esp_builder_job_update("BUILDING","Compiling ESP firmware")
        log_path=LOG_DIR/"esp-builder.log"; env=os.environ.copy(); env["PYTHONUTF8"]="1"
        with log_path.open("w",encoding="utf-8") as log:
            result=subprocess.run([str(script)],cwd=str(ESP_KIOSK_SOURCE_DIR),env=env,stdout=log,stderr=subprocess.STDOUT,text=True,timeout=BUILD_TIMEOUT)
        log_tail=tail(log_path,80)
        if result.returncode != 0:
            reason="COMPILE_FAILED" if "error:" in log_tail.lower() else "BUILD_FAILED"
            esp_builder_job_update("FAILED",reason,log_file=str(log_path),log_tail=log_tail,error_code=reason); return
        esp_builder_job_update("PACKAGING","Packaging and validating OTA artifact",log_file=str(log_path),log_tail=log_tail)
        packages=sorted((ESP_KIOSK_SOURCE_DIR/"artifacts/esp-kiosk/ota").glob("*_OTA.zip"),key=lambda p:p.stat().st_mtime,reverse=True)
        if not packages: raise ValueError("PACKAGE_FAILED")
        artifact=_register_built_ota_package(packages[0])
        esp_builder_job_update("SUCCESS",f"Firmware {artifact['version']} đã sẵn sàng cho OTA",artifact=artifact,package=str(packages[0]),log_file=str(log_path),log_tail=log_tail)
    except subprocess.TimeoutExpired:
        esp_builder_job_update("FAILED","BUILD_TIMEOUT",error_code="BUILD_TIMEOUT")
    except Exception as exc:
        code=str(exc) if str(exc).isupper() else "REGISTER_FAILED"
        esp_builder_job_update("FAILED",code,error_code=code)
    finally:
        if ESP_BUILDER_LOCK.locked(): ESP_BUILDER_LOCK.release()

def start_esp_builder():
    if not ESP_BUILDER_LOCK.acquire(False): raise RuntimeError("BUILD_ALREADY_RUNNING")
    try:
        esp_builder_job_update("QUEUED","Build job đã được xếp hàng")
        threading.Thread(target=_run_esp_builder,daemon=True).start()
    except Exception:
        ESP_BUILDER_LOCK.release(); raise

PROMOTION_STATE_FILE=CONFIG_DIR/"promotion-state.json"
_promotion_lock=threading.Lock()

def _releases_root():
    return MESFLOW_SOURCE_DIR.parent/"artifacts"/"releases"

def _release_dir_for(version):
    return _releases_root()/version

def _release_contamination(version):
    """Detect a version whose frozen artifact cannot be trusted.

    Two independent checks: (1) an explicit CONTAMINATED.json marker placed
    next to a known-bad release (never overwrites release.json/checksums.txt
    themselves), and (2) a live check that the Docker tag recorded in this
    release's release.json still points at the exact image id that was
    frozen — if a later build silently moved the tag (the 65.8.44.65
    incident), this catches it even without a marker file.
    """
    root=_release_dir_for(version)
    marker=load_json(root/"CONTAMINATED.json",{})
    if marker.get("contaminated"):
        return {"contaminated":True,"reason":marker.get("reason","Marked contaminated"),"source":"marker"}
    meta=load_json(root/"release.json",{})
    image=str(meta.get("image") or ""); expected_id=str(meta.get("image_id") or "")
    if image and expected_id:
        try:
            r=subprocess.run(["docker","image","inspect",image,"--format","{{.Id}}"],capture_output=True,text=True,timeout=15)
            if r.returncode==0:
                actual_id=r.stdout.strip()
                if actual_id and actual_id!=expected_id:
                    return {"contaminated":True,"reason":f"Docker tag {image} now points at {actual_id}, not the frozen id {expected_id}. A later build moved the tag.","source":"tag_drift"}
        except Exception:
            pass
    return {"contaminated":False}

def _load_promotion_state():
    with _promotion_lock:
        return load_json(PROMOTION_STATE_FILE,{})

def _reset_promotion_state(version,artifact):
    state={
        "version":version,
        "source_commit":artifact.get("source_commit",""),
        "package_sha256":artifact.get("package_sha256",""),
        "image":artifact.get("image",""),
        "image_digest":artifact.get("digest") or artifact.get("image_digest",""),
        "image_id":artifact.get("image_id",""),
        "expected_schema_revision":artifact.get("schema_revision",""),
        "build_status":"PASS",
        "local_status":"NOT_TESTED","local_pass":False,
        "test_status":"NOT_DEPLOYED","test_pass":False,
        "production_status":"NOT_DEPLOYED","production_pass":False,
    }
    with _promotion_lock:
        save_json(PROMOTION_STATE_FILE,state)
    return state

def _update_promotion_state(version,artifact,**fields):
    """Merge fields into the tracked promotion state for `version`.

    If the tracked state belongs to a different version, it is reset first
    — promotion status never silently carries over to a different artifact.
    """
    with _promotion_lock:
        state=load_json(PROMOTION_STATE_FILE,{})
    if state.get("version")!=version:
        state=_reset_promotion_state(version,artifact)
    state.update(fields)
    with _promotion_lock:
        save_json(PROMOTION_STATE_FILE,state)
    return state

def _actual_running_mes_image_id():
    ds=_docker_mes_state()
    cid=(ds.get("container_ids") or {}).get("mesflow","")
    if not cid: return ""
    try:
        r=subprocess.run(["docker","inspect",cid,"--format","{{.Image}}"],capture_output=True,text=True,timeout=15)
        return r.stdout.strip() if r.returncode==0 else ""
    except Exception:
        return ""

def _actual_schema_revision(base_urls=None):
    """Read the real applied Alembic head from /api/system/ready.

    Deliberately not `schema_version` (an app-defined system_meta label that
    looks like an app version string) — see AGENTS.md: never infer schema
    from the app version.
    """
    payload,code=http_json("/api/system/ready",6)
    if code==200: return str(payload.get("migration_head") or "")
    return ""

def _http_smoke_ok(base_urls=None,timeout=6):
    """Minimal server-side smoke check: the login page renders and looks like MESFlow."""
    for base in _mes_url_candidates():
        try:
            ctx=ssl._create_unverified_context() if base.lower().startswith("https://") else None
            req=urllib.request.Request(base+"/login",headers={"Host":"localhost","User-Agent":"MESFlow-Deploy-Agent"})
            with urllib.request.urlopen(req,timeout=timeout,context=ctx) as r:
                body=r.read(4096).decode("utf-8",errors="replace")
                if r.status==200 and ("MESFlow" in body or "mesflow" in body.lower()):
                    return True
        except Exception:
            continue
    return False

def _record_local_deploy_result(version,success,message=""):
    """Called after a Release Manager-initiated local deploy finishes.

    Computes LOCAL_PASS from real evidence: exact image bytes (image id, not
    tag), applied schema vs the frozen expected schema, and an HTTP smoke
    check — never from the deploy job's return code alone.
    """
    artifact=_artifact_for_release(version)
    if not artifact.get("version"): return
    contamination=_release_contamination(version)
    if not success:
        _update_promotion_state(version,artifact,local_status="FAILED",local_pass=False,local_message=message)
        return
    if contamination.get("contaminated"):
        _update_promotion_state(version,artifact,local_status="FAILED",local_pass=False,
                                 local_message=f"CONTAMINATED release: {contamination.get('reason')}")
        return
    actual_id=_actual_running_mes_image_id()
    expected_id=str(artifact.get("image_id") or "")
    image_ok=bool(expected_id) and actual_id==expected_id
    expected_schema=str(artifact.get("schema_revision") or "")
    actual_schema=_actual_schema_revision()
    schema_ok=bool(expected_schema) and expected_schema not in ("unknown","") and actual_schema==expected_schema
    smoke_ok=_http_smoke_ok()
    local_pass=image_ok and schema_ok and smoke_ok
    reasons=[]
    if not image_ok: reasons.append(f"running image id {actual_id or 'unknown'} != frozen id {expected_id or 'unknown'}")
    if not schema_ok: reasons.append(f"schema expected={expected_schema or 'unknown'} actual={actual_schema or 'unknown'}")
    if not smoke_ok: reasons.append("browser/HTTP smoke check failed")
    _update_promotion_state(version,artifact,
        local_status="PASS" if local_pass else "FAILED",local_pass=local_pass,
        local_message="LOCAL_PASS" if local_pass else "; ".join(reasons),
        actual_schema_revision=actual_schema,image_id_verified=image_ok,smoke_ok=smoke_ok,
        zip_sha_unchanged=True,image_digest_unchanged=image_ok)

def _release_summary():
    version_file=MESFLOW_SOURCE_DIR/"VERSION.txt"
    version=version_file.read_text(encoding="utf-8-sig").strip() if version_file.is_file() else "—"
    try:
        commit=subprocess.run(["git","-C",str(MESFLOW_SOURCE_DIR),"rev-parse","--short","HEAD"],capture_output=True,text=True,timeout=5).stdout.strip() or "unknown"
    except Exception: commit="unknown"
    latest=sorted(_releases_root().glob("*/image-info.json"),key=lambda p:p.stat().st_mtime,reverse=True)
    build=load_json(latest[0],{}) if latest else {}
    if latest:
        package=latest[0].parent/f"MESFlow_{build.get('version','')}.deploy.zip"
        release_meta=load_json(latest[0].parent/"release.json",{})
        build={**build,"package":str(package),"package_sha256":sha(package) if package.is_file() else "",
               "image_id":release_meta.get("image_id",""),"schema_revision":release_meta.get("schema_revision","")}
    contamination=_release_contamination(build.get("version","")) if build.get("version") else {"contaminated":False}
    build_job=load_state().get("release_build_job") or {}
    build_state={"QUEUED":"BUILDING","BUILDING":"BUILDING","SUCCESS":"PASS","FAILED":"FAILED"}.get(build_job.get("status",""),"IDLE" if not build_job else "IDLE")
    promotion=load_json(PROMOTION_STATE_FILE,{})
    if promotion.get("version")!=build.get("version"): promotion={"version":build.get("version",""),"local_status":"NOT_TESTED","test_status":"NOT_DEPLOYED","production_status":"NOT_DEPLOYED"}
    expected=str(promotion.get("expected_schema_revision") or build.get("schema_revision") or "")
    actual=str(promotion.get("actual_schema_revision") or "")
    schema_pass=bool(expected and actual and expected not in ("unknown","") and expected==actual)
    local_pass=bool(promotion.get("local_pass")); test_pass=bool(promotion.get("test_pass"))
    zip_ok=bool(promotion.get("zip_sha_unchanged")); digest_ok=bool(promotion.get("image_digest_unchanged"))
    production_promote_enabled=os.environ.get("MESFLOW_PRODUCTION_PROMOTE_ENABLED","").strip().lower() in {"1","true","yes","on"}
    gate_ok=local_pass and test_pass and zip_ok and digest_ok and schema_pass and not contamination.get("contaminated")
    # Exact blocking reason, so the UI never just shows a disabled button.
    build_block=None if MESFLOW_BUILD_ENABLED else "Build is disabled on this Agent (MESFLOW_BUILD_ENABLED=0 — this host is not DEV)."
    local_block=None
    if not build.get("version"): local_block="No release has been built yet."
    elif contamination.get("contaminated"): local_block=f"Release {build.get('version')} is CONTAMINATED: {contamination.get('reason')}"
    test_block=None
    if not local_pass: test_block="LOCAL_PASS is required before promoting to Production Test."
    elif contamination.get("contaminated"): test_block=f"Release {build.get('version')} is CONTAMINATED: {contamination.get('reason')}"
    elif not (os.environ.get("MESFLOW_PRODUCTION_TEST_AGENT_URL") or "").strip(): test_block="No Production Test Agent is configured (set MESFLOW_PRODUCTION_TEST_AGENT_URL/_USER/_PASSWORD)."
    production_block=None
    if not gate_ok:
        missing=[]
        if not local_pass: missing.append("LOCAL_PASS")
        if not test_pass: missing.append("TEST_PASS")
        if not zip_ok: missing.append("ZIP SHA unchanged")
        if not digest_ok: missing.append("image digest unchanged")
        if not schema_pass: missing.append("schema PASS")
        if contamination.get("contaminated"): missing.append("release not contaminated")
        production_block="Blocked: "+", ".join(missing) if missing else "Blocked"
    elif not production_promote_enabled:
        production_block="Gate PASS, but production execution is disabled on this Agent until a human sets MESFLOW_PRODUCTION_PROMOTE_ENABLED=1 and clicks confirm."
    production_status=promotion.get("production_status") or ("READY" if gate_ok else "NOT_DEPLOYED")
    return {"source_path":str(MESFLOW_SOURCE_DIR),"source_exists":MESFLOW_SOURCE_DIR.is_dir(),"source_version":version,"git":commit,
            "status":"clean" if MESFLOW_SOURCE_DIR.is_dir() else "missing","latest":build,"contamination":contamination,
            "states":{"build":build_state,"local":promotion.get("local_status","NOT_TESTED"),
                      "test":promotion.get("test_status","NOT_DEPLOYED"),"production":production_status},
            "blocked":{"build":build_block,"local":local_block,"test":test_block,"production":production_block},
            "promotion":{**promotion,"expected_schema_revision":expected,"actual_schema_revision":actual,"schema_pass":schema_pass,
                        "production_gate":gate_ok,"production_promote_enabled":production_promote_enabled,
                        "history":["Built" if build.get("version") else "Not built",
                                   "Local PASS" if local_pass else "Local NOT TESTED",
                                   "Test PASS" if test_pass else "Test NOT DEPLOYED",
                                   "Production PASS" if promotion.get("production_pass") else "Production NOT DEPLOYED"]}}

def release_build_update(status,message,**extra):
    def mutate(s):
        j=dict(s.get("release_build_job") or {});j.update(status=status,message=message,updated_at=now_text(),**extra);s["release_build_job"]=j
    return update_state(mutate).get("release_build_job")

def _run_release_build():
    try:
        if not MESFLOW_BUILD_ENABLED: raise ValueError("BUILD_DISABLED")
        script=MESFLOW_SOURCE_DIR/"scripts"/"build-release.sh"
        if not MESFLOW_SOURCE_DIR.is_dir(): raise ValueError("SOURCE_NOT_FOUND")
        if not script.is_file(): raise ValueError("BUILD_SCRIPT_NOT_FOUND")
        release_build_update("BUILDING","Đang build Docker image tại DEV",source_path=str(MESFLOW_SOURCE_DIR))
        log_path=LOG_DIR/"mesflow-builder.log"
        with log_path.open("w",encoding="utf-8") as log:
            result=subprocess.run([str(script)],cwd=str(MESFLOW_SOURCE_DIR),stdout=log,stderr=subprocess.STDOUT,text=True,timeout=BUILD_TIMEOUT)
        log_tail=tail(log_path,100)
        if result.returncode: release_build_update("FAILED","BUILD_FAILED",error_code="BUILD_FAILED",log_tail=log_tail,log_file=str(log_path));return
        summary=_release_summary(); latest=sorted((MESFLOW_SOURCE_DIR.parent/"artifacts"/"releases").glob("*/image-info.json"),key=lambda p:p.stat().st_mtime,reverse=True)
        if not latest: raise ValueError("PACKAGE_NOT_FOUND")
        info=load_json(latest[0],{}); release_build_update("SUCCESS","Local build complete",latest={**info,"package":str(latest[0].parent/("MESFlow_"+info.get("version","")+".deploy.zip"))},source=summary,log_tail=log_tail,log_file=str(log_path))
    except subprocess.TimeoutExpired:
        release_build_update("FAILED","BUILD_TIMEOUT",error_code="BUILD_TIMEOUT")
    except Exception as exc:
        code=str(exc) if str(exc).isupper() else "BUILD_FAILED";release_build_update("FAILED",code,error_code=code)
    finally:
        if MESFLOW_BUILD_LOCK.locked(): MESFLOW_BUILD_LOCK.release()

def start_release_build():
    if not MESFLOW_BUILD_LOCK.acquire(False): raise RuntimeError("BUILD_ALREADY_RUNNING")
    try:
        release_build_update("QUEUED","Build release queued");threading.Thread(target=_run_release_build,daemon=True).start()
    except Exception:
        MESFLOW_BUILD_LOCK.release();raise

def _promotion_state(version):
    p=MESFLOW_SOURCE_DIR.parent/"artifacts"/"releases"/version/"PROMOTION.json"
    return load_json(p,{}) if p.is_file() else {}

def _artifact_for_release(version):
    root=MESFLOW_SOURCE_DIR.parent/"artifacts"/"releases"/version
    package=root/f"MESFlow_{version}.deploy.zip"
    info=load_json(root/"image-info.json",{}); meta=load_json(root/"release.json",{})
    return {"version":version,"image":meta.get("image",info.get("image")),"digest":meta.get("image_digest",info.get("digest")),
            "image_id":meta.get("image_id",""),"schema_revision":meta.get("schema_revision",""),
            "package":str(package),"package_sha256":sha(package) if package.is_file() else "",
            "source_commit":meta.get("source_commit",""),"built_at":meta.get("built_at","")}

def _release_targets():
    raw=load_json(CONFIG_DIR/"release-targets.json",{})
    return raw if isinstance(raw,list) else [{"name":"LOCAL","role":"DEV","url":"local","status":"AVAILABLE"}]

# ---------------- Promote Production Test: remote Deploy Agent client ----------------
_promote_test_lock=threading.Lock()

class _RemoteAgentError(RuntimeError):
    pass

def _remote_agent_client(base_url,user,password,timeout=20):
    """Log in to a remote Deploy Agent's session-based auth, mirroring
    scripts/promote-test.sh exactly (same endpoints, same CSRF contract).
    Returns (opener, csrf_token)."""
    base=base_url.rstrip("/")
    jar=http.cookiejar.CookieJar()
    opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    def _get(path):
        req=urllib.request.Request(base+path,headers={"User-Agent":"MESFlow-Deploy-Agent-Promote"})
        with opener.open(req,timeout=timeout) as r: return r.read().decode("utf-8",errors="replace")
    def _csrf(html):
        m=re.search(r'name="csrf"\s+value="([^"]+)"',html)
        return m.group(1) if m else ""
    login_page=_get("/login")
    csrf=_csrf(login_page)
    data=urlencode({"username":user,"password":password,"csrf":csrf}).encode()
    req=urllib.request.Request(base+"/login",data=data,headers={"Content-Type":"application/x-www-form-urlencoded","User-Agent":"MESFlow-Deploy-Agent-Promote"})
    with opener.open(req,timeout=timeout) as r: r.read()
    home=_get("/")
    csrf2=_csrf(home)
    if not csrf2: raise _RemoteAgentError("REMOTE_LOGIN_FAILED: no session/csrf obtained (check target URL/credentials)")
    return opener,base,csrf2

def _remote_agent_upload(opener,base,csrf,zip_path,timeout=600):
    """multipart/form-data POST of the release ZIP to the remote /upload endpoint."""
    boundary=uuid.uuid4().hex
    zip_path=Path(zip_path)
    parts=[]
    def field(name,value):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    field("csrf",csrf)
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="release_zip"; filename="{zip_path.name}"\r\n'
                   'Content-Type: application/zip\r\n\r\n').encode())
    parts.append(zip_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body=b"".join(parts)
    req=urllib.request.Request(base+"/upload",data=body,headers={
        "Content-Type":f"multipart/form-data; boundary={boundary}",
        "X-CSRF-Token":csrf,"User-Agent":"MESFlow-Deploy-Agent-Promote"})
    with opener.open(req,timeout=timeout) as r:
        return r.status,r.read().decode("utf-8",errors="replace")

def _remote_agent_deploy(opener,base,csrf,version,timeout=600):
    req=urllib.request.Request(base+f"/deploy/{quote(version,safe='')}",data=urlencode({"csrf":csrf}).encode(),
        headers={"Content-Type":"application/x-www-form-urlencoded","X-CSRF-Token":csrf,"User-Agent":"MESFlow-Deploy-Agent-Promote"})
    with opener.open(req,timeout=timeout) as r:
        return r.status,r.read().decode("utf-8",errors="replace")

def _remote_agent_status(base,timeout=15):
    req=urllib.request.Request(base+"/api/status",headers={"User-Agent":"MESFlow-Deploy-Agent-Promote"})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8",errors="replace"))

def _production_test_target():
    url=(os.environ.get("MESFLOW_PRODUCTION_TEST_AGENT_URL") or "").strip().rstrip("/")
    user=(os.environ.get("MESFLOW_PRODUCTION_TEST_AGENT_USER") or "admin").strip()
    password=(os.environ.get("MESFLOW_PRODUCTION_TEST_AGENT_PASSWORD") or "").strip()
    return url,user,password

def promote_test_update(status,message,**extra):
    def mutate(s):
        j=dict(s.get("promote_test_job") or {});j.update(status=status,message=message,updated_at=now_text(),**extra);s["promote_test_job"]=j
    return update_state(mutate).get("promote_test_job")

def _run_promote_test(version):
    try:
        artifact=_artifact_for_release(version)
        if not artifact.get("package") or not Path(artifact["package"]).is_file():
            raise _RemoteAgentError("PACKAGE_NOT_FOUND")
        contamination=_release_contamination(version)
        if contamination.get("contaminated"):
            raise _RemoteAgentError(f"RELEASE_CONTAMINATED: {contamination.get('reason')}")
        promotion=_load_promotion_state()
        if promotion.get("version")!=version or not promotion.get("local_pass"):
            raise _RemoteAgentError("LOCAL_PASS_REQUIRED")
        url,user,password=_production_test_target()
        if not url or not password:
            raise _RemoteAgentError("PRODUCTION_TEST_AGENT_NOT_CONFIGURED: set MESFLOW_PRODUCTION_TEST_AGENT_URL/_USER/_PASSWORD")
        zip_path=Path(artifact["package"])
        local_sha=sha(zip_path)
        if artifact.get("package_sha256") and artifact["package_sha256"]!=local_sha:
            raise _RemoteAgentError("PACKAGE_SHA_SELF_CHECK_FAILED")
        promote_test_update("UPLOADING",f"Uploading {zip_path.name} (sha256={local_sha[:12]}...) to {url}",version=version)
        opener,base,csrf=_remote_agent_client(url,user,password)
        _remote_agent_upload(opener,base,csrf,zip_path)
        promote_test_update("DEPLOYING",f"Deploying {version} on target (--no-build; server enforces image-release contract)",version=version)
        _remote_agent_deploy(opener,base,csrf,version)
        deadline=time.time()+MES_VERIFY_TIMEOUT
        last={}
        while time.time()<deadline:
            time.sleep(3)
            try: last=_remote_agent_status(base)
            except Exception as ex: last={"error":str(ex)}; continue
            job=last.get("job") or {}
            if job.get("status")=="success" and canonical_version(str(job.get("version") or ""))==canonical_version(version):
                break
            if job.get("status")=="failed":
                raise _RemoteAgentError(f"REMOTE_DEPLOY_FAILED: {job.get('message')}")
        else:
            raise _RemoteAgentError(f"REMOTE_DEPLOY_TIMEOUT: last status {last}")
        # The remote Agent's own run_job() already verified the image digest
        # byte-for-byte before starting the container (IMAGE_DIGEST_MISMATCH
        # aborts the deploy) — see _verify_and_prepare_image_release(). Here
        # we only need to confirm the remote reports the right version and is
        # healthy; a failed remote digest check would already show as
        # job.status == "failed" above.
        mes=(last or {}).get("mes") or {}
        remote_version=canonical_version(str(mes.get("version") or ""))
        version_ok=remote_version==canonical_version(version)
        healthy_ok=bool(mes.get("healthy"))
        if not (version_ok and healthy_ok):
            raise _RemoteAgentError(f"REMOTE_VERIFY_FAILED: version_ok={version_ok} healthy_ok={healthy_ok} mes={mes}")
        promote_test_update("VERIFYING","Checking remote schema and browser smoke",version=version)
        _update_promotion_state(version,artifact,
            test_status="PASS",test_pass=True,
            test_message=f"TEST_PASS: remote version={remote_version} healthy={healthy_ok}",
            test_target=url)
        promote_test_update("SUCCESS",f"TEST_PASS: {version} verified on {url}",version=version)
    except Exception as exc:
        code=str(exc) if isinstance(exc,_RemoteAgentError) else f"{type(exc).__name__}: {exc}"
        try:
            artifact=_artifact_for_release(version)
            _update_promotion_state(version,artifact,test_status="FAILED",test_pass=False,test_message=code)
        except Exception:
            pass
        promote_test_update("FAILED",code,error_code=code)
    finally:
        if _promote_test_lock.locked(): _promote_test_lock.release()

def start_promote_test(version):
    if not _promote_test_lock.acquire(False): raise RuntimeError("PROMOTE_TEST_ALREADY_RUNNING")
    try:
        promote_test_update("QUEUED","Promote to Production Test queued",version=version)
        threading.Thread(target=_run_promote_test,args=(version,),daemon=True).start()
    except Exception:
        _promote_test_lock.release(); raise
def _new_recovery_code():
    raw=secrets.token_hex(8).upper()
    return "-".join(raw[i:i+4] for i in range(0,16,4))
def _normalize_recovery_code(value):
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())
def ensure_config():
    c=load_json(CONFIG_FILE,{}); changed=False; first_password=None; recovery_code=None
    configured_password=os.environ.get("MESFLOW_AGENT_ADMIN_PASSWORD","")
    if configured_password and len(configured_password)<10:
        raise RuntimeError("MESFLOW_AGENT_ADMIN_PASSWORD must contain at least 10 characters")
    if not c.get("secret_key"):c["secret_key"]=secrets.token_urlsafe(48);changed=True
    if configured_password:
        if not c.get("password_hash") or not check_password_hash(c["password_hash"],configured_password):
            c["password_hash"]=generate_password_hash(configured_password);c["password_changed_at"]=now_text();changed=True
    elif not c.get("password_hash"):
        first_password=secrets.token_urlsafe(12);c["password_hash"]=generate_password_hash(first_password);changed=True
    if not c.get("recovery_code_hash"):
        recovery_code=_new_recovery_code()
        c["recovery_code_hash"]=generate_password_hash(_normalize_recovery_code(recovery_code));changed=True
        c["recovery_code_created_at"]=now_text()
    if changed:save_json(CONFIG_FILE,c)
    if first_password or recovery_code:
        old=(CONFIG_DIR/"FIRST_LOGIN.txt")
        existing=old.read_text(encoding="utf-8",errors="replace") if old.is_file() else ""
        if first_password is None:
            for line in existing.splitlines():
                if line.startswith("Password: "):first_password=line.split(": ",1)[1].strip()
        text=(f"Workshop Update Agent\nURL: http://SERVER-IP:{PORT}{URL_PREFIX or '/'}\nUsername: admin\n"
              f"Password: {first_password or '[existing password unchanged]'}\n"
              f"Recovery Code: {recovery_code or '[already created - use local reset or installer if lost]'}\n"
              "Store this file securely and delete it after recording the credentials.\n")
        old.write_text(text,encoding="utf-8")
    return c
app.secret_key=ensure_config()["secret_key"]
def load_state():return load_json(STATE_FILE,{"job":None,"history":[],"tutorial_job":None,"tutorial_history":[],"esp_tutorial_job":None,"esp_tutorial_history":[],"esp_builder_job":None})
def update_state(fn):
    with _state_lock:
        s=load_state();fn(s);save_json(STATE_FILE,s);return s
def logged_in():return bool(session.get("authenticated"))
@app.before_request
def protect():
    if request.endpoint in {"login","forgot_password","local_reset","static","live","health","ota_console","ota_firmware_list","ota_devices","ota_upload_and_flash","ota_job_status","ota_jobs","ota_cancel_job","ota_device_check","ota_device_download","ota_device_event"}:return None
    if not logged_in():return redirect(url_for("login",next=f"{request.script_root}{request.path}"))
def verify_csrf():
    supplied=request.form.get("csrf","") or request.headers.get("X-CSRF-Token","")
    if request.is_json and not supplied:
        supplied=(request.get_json(silent=True) or {}).get("csrf","")
    if not hmac.compare_digest(str(supplied),str(session.get("csrf",""))):abort(403)
def safe_extract_zip(z,d):
    d=d.resolve()
    with zipfile.ZipFile(z) as a:
        if not a.infolist(): raise ValueError("ZIP is empty")
        total_uncompressed=sum(max(0,m.file_size) for m in a.infolist())
        if total_uncompressed>MAX_EXTRACT_MB*1024*1024:
            raise ValueError(f"ZIP expands beyond the {MAX_EXTRACT_MB} MB extraction limit")
        for m in a.infolist():
            if m.is_dir(): continue
            # A release package must contain ordinary files only.  Reject
            # symlink/special-file entries before extraction: they are not a
            # portable MESFlow package and can escape the intended tree on
            # platforms that restore link metadata.
            mode=(m.external_attr >> 16) & 0o170000
            if mode and mode not in (0o100000,):
                raise ValueError("ZIP contains a symlink or unsupported special file")
            target=(d/m.filename.replace("\\","/")).resolve()
            if d not in target.parents and target!=d:raise ValueError("ZIP contains unsafe path")
        a.extractall(d)
def locate_root(e):
    c=[]
    if (e/"VERSION.txt").is_file():c.append(e)
    c += [x for x in e.iterdir() if x.is_dir() and (x/"VERSION.txt").is_file()]
    if len(c)!=1:raise ValueError("ZIP must contain exactly one release root")
    r=c[0]
    docker_release=(r/"compose.yml").is_file() and (r/"release.json").is_file()
    legacy_release=(r/"install/deploy_release.py").is_file() and (r/"requirements.txt").is_file()
    if not (docker_release or legacy_release):
        raise ValueError("Release must contain compose.yml + release.json (Linux Docker) or legacy install/deploy_release.py")
    return r
def validate_mes_release_contract(root):
    """Validate the portable MESFlow package contract before it enters releases."""
    root=Path(root)
    if not (root/"compose.yml").is_file():
        raise ValueError("MESFlow package is missing compose.yml")
    manifest=root/"release.json"
    if not manifest.is_file():
        raise ValueError("MESFlow package is missing release.json")
    try: meta=json.loads(manifest.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as ex: raise ValueError(f"Invalid release.json: {ex.msg}") from ex
    if not isinstance(meta,dict): raise ValueError("release.json must be a JSON object")
    version=norm((root/"VERSION.txt").read_text(encoding="utf-8-sig"))
    manifest_version=norm(str(meta.get("version", "")))
    if canonical_version(manifest_version)!=canonical_version(version):
        raise ValueError(f"VERSION.txt ({version}) does not match release.json ({manifest_version})")
    if str(meta.get("name","")).strip().lower()!="mesflow":
        raise ValueError("release.json name must be MESFlow")
    promotion=load_json(root/"PROMOTION.json",{}) if (root/"PROMOTION.json").is_file() else {}
    if promotion:
        if not isinstance(promotion,dict): raise ValueError("PROMOTION.json must be a JSON object")
        promoted_version=canonical_version(promotion.get("version"))
        if promoted_version != canonical_version(version):
            raise ValueError(f"PROMOTION.json version ({promoted_version}) does not match VERSION.txt ({version})")
        declared_hash=str(promotion.get("sha256") or "").strip().lower()
        if declared_hash and not re.fullmatch(r"[0-9a-f]{64}",declared_hash):
            raise ValueError("PROMOTION.json sha256 must be 64 lowercase hexadecimal characters")
    return version,meta

def validate_image_release_contract(root):
    root=Path(root)
    required=("VERSION.txt","release.json","compose.yml","checksums.txt","PROMOTION.json")
    missing=[x for x in required if not (root/x).is_file()]
    if missing: raise ValueError("Image release missing: "+", ".join(missing))
    meta=json.loads((root/"release.json").read_text(encoding="utf-8-sig"))
    version=norm((root/"VERSION.txt").read_text(encoding="utf-8-sig"))
    if meta.get("type") != IMAGE_RELEASE_TYPE: raise ValueError("release.json type must be mesflow-image-release")
    if canonical_version(str(meta.get("version",""))) != canonical_version(version): raise ValueError("Image release version mismatch")
    image=str(meta.get("image") or "").strip(); digest=str(meta.get("image_digest") or "").strip()
    if not image or not re.fullmatch(r"sha256:[0-9a-f]{64}",digest): raise ValueError("Image release requires image and image_digest")
    promotion=json.loads((root/"PROMOTION.json").read_text(encoding="utf-8-sig"))
    if promotion.get("version") and canonical_version(str(promotion["version"])) != canonical_version(version): raise ValueError("PROMOTION version mismatch")
    return version,meta
def norm(v):
    v=v.strip()
    if not v or len(v)>64 or any(ch not in set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-") for ch in v):raise ValueError("Invalid version")
    return v
def canonical_version(v):
    # Compare deploy/runtime versions without harmless formatting differences.
    # Keep every numeric component: 65.8.44.7 must NOT equal 65.8.44.
    return str(v or "").strip().lstrip("Vv").strip()
def sha(p):
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(1048576),b""):h.update(b)
    return h.hexdigest()
def tail(p,n=200):
    try:return "\n".join(Path(p).read_text(encoding="utf-8",errors="replace").splitlines()[-n:])
    except OSError:return ""
def current_file_version():
    for p in (TARGET_HOME/"VERSION.txt", TARGET_HOME/"app/current/VERSION.txt"):
        if p.is_file():
            return p.read_text(encoding="utf-8-sig").strip()
    return "Chưa cài"
def _mes_url_candidates():
    values=[]
    for base in [MES_URL, "http://127.0.0.1", "https://127.0.0.1", "http://localhost", "https://localhost"]:
        base=(base or "").rstrip("/")
        if base and base not in values: values.append(base)
    return values

def _http_json_base(base,path,timeout=4):
    try:
        ctx=ssl._create_unverified_context() if base.lower().startswith("https://") else None
        req=urllib.request.Request(base+path,headers={"Host":"localhost","User-Agent":"MESFlow-Deploy-Agent"})
        with urllib.request.urlopen(req,timeout=timeout,context=ctx) as r:
            raw=r.read().decode("utf-8",errors="replace")
            try: payload=json.loads(raw)
            except json.JSONDecodeError: payload={"ok":False,"error":"Invalid JSON response","body":raw[:2000]}
            payload.setdefault("_agent_base_url",base)
            return payload,r.status
    except urllib.error.HTTPError as e:
        raw=e.read().decode("utf-8",errors="replace")
        try: payload=json.loads(raw)
        except json.JSONDecodeError: payload={"ok":False,"error":str(e),"body":raw[:2000]}
        payload.setdefault("http_status",e.code);payload.setdefault("_agent_base_url",base)
        return payload,e.code
    except Exception as e:
        return {"ok":False,"error":str(e),"_agent_base_url":base},0

def http_json(path,timeout=4):
    last=({"ok":False,"error":"No MES URL candidates"},0)
    first_http_error=None
    for base in _mes_url_candidates():
        payload,code=_http_json_base(base,path,timeout)
        last=(payload,code)
        if code==200:
            return payload,code
        if code and first_http_error is None:
            first_http_error=(payload,code)
    return first_http_error or last

def _docker_mes_state():
    if os.name=="nt" or not (TARGET_HOME/"compose.yml").is_file():
        return {"available":False,"running":False,"healthy":False,"health":"unknown","services":[]}
    try:
        base=["docker","compose","-f",str(TARGET_HOME/"compose.yml")]
        env=os.environ.copy(); meta=load_json(TARGET_HOME/"release.json",{}); ref=meta.get("image_ref") or (f"{meta.get('image')}@{meta.get('image_digest')}" if meta.get("image") and meta.get("image_digest") else "")
        if ref: env["MESFLOW_IMAGE"]=ref
        r=subprocess.run([*base,"ps","--status","running","--services"],cwd=str(TARGET_HOME),env=env,capture_output=True,text=True,errors="replace",timeout=12)
        services=[x.strip() for x in r.stdout.splitlines() if x.strip()]
        # The production gateway is an independent Compose project.  Application
        # health is intentionally limited to services owned by this stack.
        required=("postgres","mesflow"); service_health={}; ids={}
        for service in required:
            if service not in services: service_health[service]="stopped"; continue
            q=subprocess.run([*base,"ps","-q",service],cwd=str(TARGET_HOME),env=env,capture_output=True,text=True,errors="replace",timeout=12)
            cid=q.stdout.strip().splitlines()[0] if q.returncode==0 and q.stdout.strip() else "";ids[service]=cid
            health="unknown"
            if cid:
                ins=subprocess.run(["docker","inspect","--format","{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",cid],capture_output=True,text=True,errors="replace",timeout=12)
                if ins.returncode==0 and ins.stdout.strip(): health=ins.stdout.strip()
            service_health[service]=health
        running=r.returncode==0 and all(x in services for x in required)
        healthy=running and all(service_health.get(x) in ("healthy","none") for x in required)
        health="healthy" if healthy else "unhealthy" if running else "stopped"
        return {"available":r.returncode==0,"running":running,"healthy":healthy,"health":health,"services":services,"service_health":service_health,"container_id":ids.get("mesflow","")+"","container_ids":ids,"stderr":r.stderr.strip()}
    except Exception as e:
        return {"available":False,"running":False,"healthy":False,"health":"unknown","services":[],"error":str(e)}

def _classify_health(payload, http_status):
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    db_check = checks.get("database") if isinstance(checks.get("database"), dict) else {}
    disk_check = checks.get("disk") if isinstance(checks.get("disk"), dict) else {}
    critical_errors = payload.get("critical_errors") or []
    disk_warning = bool(disk_check) and (
        disk_check.get("warning") is True
        or disk_check.get("capacity_ok") is False
        or disk_check.get("ok") is False
    )
    critical_ok = bool(payload.get("critical_ok", payload.get("ok", False)))
    legacy_disk_only_warning = bool(db_check.get("ok")) and not critical_errors and disk_warning
    if (http_status == 200 and critical_ok) or legacy_disk_only_warning:
        return "warning" if disk_warning or payload.get("status") == "warning" else "healthy"
    return "failed"

def mes_status():
    docker_state=_docker_mes_state()
    v,vs=http_json("/api/system/version");h,hs=http_json("/api/system/health")
    health_state=_classify_health(h,hs)
    api_online=(vs==200)
    online=api_online or bool(docker_state.get("running"))
    if online and health_state=="failed" and docker_state.get("running"):
        # Nginx/backend may intentionally not expose 8080 to the host. Do not call the server OFFLINE.
        health_state="running" if hs==0 else "warning"
    return {"online":online,"healthy":health_state in ("healthy","warning","running"),"health_state":health_state,"version":v.get("version") or current_file_version(),"deployment_id":v.get("deployment_id",""),"version_payload":v,"health_payload":h,"docker":docker_state,"api_online":api_online}
def list_releases():
    out=[]
    for x in RELEASES_DIR.iterdir():
        if x.is_dir() and not x.name.startswith('.'):
            m=load_json(x/"agent_manifest.json",{});meta=load_json(x/"release.json",{});promotion=load_json(x/"PROMOTION.json",{})
            local_status=str(promotion.get("local_verification") or promotion.get("local_status") or "NOT VERIFIED").upper()
            out.append({"version":x.name,"uploaded_at":m.get("uploaded_at",""),"source_name":m.get("source_name",""),"channel":meta.get("channel","stable"),"protocol":meta.get("deployment_protocol",1),
                        "sha256":m.get("sha256",""),"source_commit":promotion.get("source_commit","NOT VERIFIED"),"local_verification":local_status})
    return sorted(out,key=lambda z:z["uploaded_at"],reverse=True)
def _mark_stale_job_interrupted(reason="Agent restarted or deployment worker is no longer active"):
    changed={"value":False}
    def f(s):
        j=s.get("job") or {}
        if j.get("status")=="running":
            j=dict(j)
            j.update(status="interrupted",finished_at=now_text(),message=reason,return_code=-2)
            for step in (j.get("steps") or {}).values():
                if step.get("status")=="running":
                    step["status"]="interrupted"
                    step["message"]=(step.get("message") or "")+" (interrupted)"
                    step["at"]=now_text()
            s["job"]=j
            history=s.setdefault("history",[])
            if not history or history[0].get("deployment_id")!=j.get("deployment_id"):
                history.insert(0,dict(j))
                s["history"]=history[:50]
            changed["value"]=True
    update_state(f)
    if changed["value"]:append_log(f"Recovered stale deployment state: {reason}")
    return changed["value"]

def job_running():
    state_running=(load_state().get("job") or {}).get("status") in MES_JOB_ACTIVE_STATES
    worker_running=_job_lock.locked()
    if state_running and not worker_running:
        _mark_stale_job_interrupted()
        return False
    return worker_running
def set_job_phase(status,message=""):
    if status not in MES_JOB_ACTIVE_STATES|{"success","failed"}: raise ValueError(f"Unknown MES job state: {status}")
    def f(s):
        j=s.get("job") or {}
        transitions=j.setdefault("transitions",[])
        if not transitions or transitions[-1].get("status")!=status:
            transitions.append({"status":status,"at":now_text(),"message":message})
        j.update(status=status,message=message or status,phase=status)
        s["job"]=j
    update_state(f)
def set_step(name,status,message=""):
    def f(s):
        j=s.get("job") or {};steps=j.setdefault("steps",{});steps[name]={"status":status,"message":message,"at":now_text()};j["message"]=message or name;s["job"]=j
    update_state(f)
def port_pids(port=8080):
    pids=set()
    if os.name != "nt":
        return pids
    try:
        result=subprocess.run(["netstat","-ano","-p","tcp"],capture_output=True,text=True,errors="replace",timeout=10)
        for line in result.stdout.splitlines():
            cols=line.split()
            if len(cols)>=5 and cols[0].upper()=="TCP" and cols[1].rsplit(":",1)[-1]==str(port) and cols[3].upper()=="LISTENING":
                if cols[-1].isdigit():pids.add(int(cols[-1]))
    except Exception:
        pass
    return pids

def port_open(host="127.0.0.1",port=8080,timeout=0.5):
    try:
        with socket.create_connection((host,port),timeout=timeout):return True
    except OSError:return False

def _windows_process_info(pid):
    if os.name != "nt":
        return {"pid": int(pid)}
    script = (
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=%d\" -ErrorAction SilentlyContinue; "
        "if($p){$o=[ordered]@{ProcessId=$p.ProcessId;ParentProcessId=$p.ParentProcessId;"
        "Name=$p.Name;ExecutablePath=$p.ExecutablePath;CommandLine=$p.CommandLine};"
        "$owner=Invoke-CimMethod -InputObject $p -MethodName GetOwner -ErrorAction SilentlyContinue;"
        "if($owner -and $owner.ReturnValue -eq 0){$o.Owner=($owner.Domain+'\\'+$owner.User)};"
        "$o|ConvertTo-Json -Compress}"
    ) % int(pid)
    try:
        r=subprocess.run(["powershell","-NoProfile","-ExecutionPolicy","Bypass","-Command",script],capture_output=True,text=True,errors="replace",timeout=15,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if r.stdout.strip():
            return json.loads(r.stdout.strip())
        return {"pid":int(pid),"missing":True,"stderr":r.stderr.strip()}
    except Exception as e:
        return {"pid":int(pid),"info_error":str(e)}

def _terminate_windows_pid(pid, log):
    pid=int(pid)
    info=_windows_process_info(pid)
    log.write(f"[STOP] Target process: {json.dumps(info,ensure_ascii=False)}\n");log.flush()
    attempts=[]
    commands=[
        ["taskkill","/PID",str(pid),"/T","/F"],
        ["powershell","-NoProfile","-ExecutionPolicy","Bypass","-Command",f"Stop-Process -Id {pid} -Force -ErrorAction Stop"],
        ["powershell","-NoProfile","-ExecutionPolicy","Bypass","-Command",f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' -ErrorAction SilentlyContinue;if($p){{Invoke-CimMethod -InputObject $p -MethodName Terminate | ConvertTo-Json -Compress}}"],
    ]
    for cmd in commands:
        try:
            r=subprocess.run(cmd,capture_output=True,text=True,errors="replace",timeout=20,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            attempts.append({"cmd":cmd[0],"returncode":r.returncode,"stdout":r.stdout.strip(),"stderr":r.stderr.strip()})
        except Exception as e:
            attempts.append({"cmd":cmd[0],"error":str(e)})
        time.sleep(0.7)
        if pid not in port_pids(8080) and not process_alive(pid):break
    log.write(f"[STOP] Termination attempts: {json.dumps(attempts,ensure_ascii=False)}\n");log.flush()
    return attempts

def _docker_compose(log, args, check=True, timeout=600, env_extra=None):
    compose=TARGET_HOME/"compose.yml"
    if not compose.is_file():
        raise RuntimeError(f"Missing {compose}")
    cmd=["docker","compose","-f",str(compose),*args]
    log.write("RUN: "+" ".join(cmd)+"\n"); log.flush()
    env=os.environ.copy(); meta=load_json(TARGET_HOME/"release.json",{}); ref=meta.get("image_ref") or (f"{meta.get('image')}@{meta.get('image_digest')}" if meta.get("image") and meta.get("image_digest") else "")
    if ref: env["MESFLOW_IMAGE"]=ref
    env.update(env_extra or {})
    r=subprocess.run(cmd,cwd=str(TARGET_HOME),env=env,stdout=log,stderr=subprocess.STDOUT,text=True,timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"docker compose {' '.join(args)} failed with code {r.returncode}")
    return r.returncode

def _compose_at(root, log, args, check=True, timeout=600, env_extra=None):
    root=Path(root); compose=root/"compose.yml"
    if not compose.is_file(): raise RuntimeError(f"Missing {compose}")
    env_file=TARGET_HOME/".env"
    if not env_file.is_file() or not os.access(env_file,os.R_OK):
        try:
            stat=env_file.stat()
            detail=f"mode={stat.st_mode & 0o777:04o} uid={stat.st_uid} gid={stat.st_gid}"
        except OSError as ex:
            detail=f"stat failed: {type(ex).__name__}: {ex}"
        raise RuntimeError(f"Agent cannot read persistent environment file: {env_file} ({detail}). Check the bind mount and Agent UID/GID; do not copy or chmod the file broadly.")
    cmd=["docker","compose","--env-file",str(env_file),"-f",str(compose),*args]
    log.write("RUN: "+" ".join(cmd)+"\n"); log.flush()
    env=os.environ.copy(); env.update(env_extra or {})
    r=subprocess.run(cmd,cwd=str(root),env=env,stdout=log,stderr=subprocess.STDOUT,text=True,timeout=timeout)
    if check and r.returncode: raise RuntimeError(f"docker compose {' '.join(args)} failed with code {r.returncode}")
    return r.returncode

def _verify_and_prepare_image_release(release, meta, log):
    image=str(meta["image"]); expected=str(meta["image_digest"])
    distribution=str(meta.get("distribution") or "registry")
    if distribution == "bundle":
        bundle=Path(release)/str(meta.get("bundle") or "")
        if not bundle.is_file(): raise RuntimeError("IMAGE_BUNDLE_MISSING")
        log.write(f"Loading image bundle {bundle.name}\n");log.flush()
        r=subprocess.run(["docker","load","-i",str(bundle)],stdout=log,stderr=subprocess.STDOUT,text=True,timeout=BUILD_TIMEOUT)
        if r.returncode: raise RuntimeError("IMAGE_LOAD_FAILED")
    else:
        r=subprocess.run(["docker","pull",f"{image}@{expected}"],stdout=log,stderr=subprocess.STDOUT,text=True,timeout=BUILD_TIMEOUT)
        if r.returncode: raise RuntimeError("IMAGE_PULL_FAILED")
    ref=image if distribution == "bundle" else f"{image}@{expected}"
    fmt="{{.Id}}" if distribution == "bundle" else "{{index .RepoDigests 0}}"
    inspect=subprocess.run(["docker","image","inspect",ref,"--format",fmt],capture_output=True,text=True,timeout=30)
    actual=inspect.stdout.strip()
    if distribution == "bundle":
        expected_id=str(meta.get("image_id") or "")
        if inspect.returncode or not expected_id or actual != expected_id: raise RuntimeError(f"IMAGE_ID_MISMATCH expected={expected_id} actual={actual}")
    elif inspect.returncode or expected not in actual:
        raise RuntimeError(f"IMAGE_DIGEST_MISMATCH expected={expected} actual={actual}")
    return ref

def stop_mes(log, timeout=120):
    set_step("restart","running","Stopping MES Docker stack")
    if os.name=="nt":
        deadline=time.time()+timeout
        while time.time()<deadline:
            for pid in sorted(port_pids(8080)):_terminate_windows_pid(pid,log)
            time.sleep(1)
            if not port_open(): return
        raise RuntimeError("MES did not stop on Windows")
    if (TARGET_HOME/"compose.yml").is_file():
        # Never destroy the Compose stack/network or stop PostgreSQL for an app cutover.
        _docker_compose(log,["stop","mesflow"],check=False,timeout=timeout)
    deadline=time.time()+timeout
    while time.time()<deadline:
        if not port_open(): return
        time.sleep(1)
    raise RuntimeError("MES did not stop; port 8080 is still open")

def process_alive(pid):
    if not pid:return False
    if os.name=="nt":
        r=subprocess.run(["tasklist","/FI",f"PID eq {pid}","/FO","CSV","/NH"],capture_output=True,text=True,errors="replace",creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        return str(pid) in r.stdout and "No tasks" not in r.stdout
    try:os.kill(int(pid),0);return True
    except OSError:return False

def start_mes(log):
    if os.name!="nt" and (TARGET_HOME/"compose.yml").is_file():
        _docker_compose(log,["up","-d","--build","postgres","mesflow"],check=True,timeout=900)
        log.write("MES Docker stack started\n");log.flush()
        return None
    python_exe=TARGET_HOME/".venv/Scripts/python.exe"
    current=TARGET_HOME/"app/current"
    launcher=current/"launcher.py"
    entry=launcher if launcher.is_file() else current/"start_server.py"
    if not python_exe.is_file():raise RuntimeError(f"Missing MES Python: {python_exe}")
    if not entry.is_file():raise RuntimeError(f"Missing MES entry point: {entry}")
    env=os.environ.copy();env.update({"WORKSHOP_HOME":str(TARGET_HOME),"WORKSHOP_DATA_DIR":str(TARGET_HOME/"data"),"WORKSHOP_CONFIG_DIR":str(TARGET_HOME/"config"),"PYTHONUTF8":"1"})
    flags=getattr(subprocess,"CREATE_NEW_PROCESS_GROUP",0)|getattr(subprocess,"DETACHED_PROCESS",0)|getattr(subprocess,"CREATE_NO_WINDOW",0)
    mes_log=LOG_DIR/"mes_process.log"
    handle=mes_log.open("a",encoding="utf-8")
    proc=subprocess.Popen([str(python_exe),str(entry)],cwd=str(current),env=env,stdout=handle,stderr=subprocess.STDOUT,creationflags=flags,close_fds=True)
    save_json(DATA_DIR/"mes_process.json",{"pid":proc.pid,"started_at":now_text(),"entry":str(entry),"log":str(mes_log)})
    log.write(f"Started MES PID {proc.pid} using {entry}; verify timeout={MES_VERIFY_TIMEOUT}s\n");log.flush()
    return proc.pid

def wait_verify(version,deployment_id,timeout=None,require_health=True,pid=None):
    timeout=timeout or MES_VERIFY_TIMEOUT
    end=time.time()+timeout;last={};wrong_version_count=0
    mes_log=LOG_DIR/"mes_process.log"
    while time.time()<end:
        time.sleep(1.5)
        if pid and not process_alive(pid) and not port_open():
            detail=tail(mes_log,80)
            raise RuntimeError(f"MES process exited before opening port 8080. MES log:\n{detail or '[empty mes_process.log]'}")
        v,code=http_json("/api/system/version",3);last=v
        if code==200:
            actual=str(v.get("version","")).lstrip("Vv")
            expected=version.lstrip("Vv")
            if actual and actual!=expected:
                wrong_version_count += 1
                if wrong_version_count>=4:
                    raise RuntimeError(f"Wrong MES process is running: expected {version}, got {actual}. Last response: {v}")
                continue
            if actual==expected and v.get("deployment_id","")==deployment_id:
                if not require_health:
                    return v, {"ok": True, "verification": "version_and_deployment_id"}
                h,hc=http_json("/api/system/health",3)
                health_ok=bool(h.get("critical_ok",h.get("ok",h.get("status") in ("healthy","warning","UP"))))
                checks=h.get("checks") if isinstance(h.get("checks"),dict) else {}
                db_check=checks.get("database") if isinstance(checks.get("database"),dict) else {}
                disk_check=checks.get("disk") if isinstance(checks.get("disk"),dict) else {}
                critical_errors=h.get("critical_errors") or []
                # Compatibility guard: releases 62.6.44/45 returned HTTP 503 solely
                # because disk usage crossed the warning threshold. A healthy DB,
                # matching version and matching deployment id are sufficient; disk
                # pressure remains a visible warning and must never trigger rollback.
                disk_only_warning=(
                    bool(db_check.get("ok"))
                    and not critical_errors
                    and bool(disk_check)
                    and not bool(disk_check.get("ok",True))
                )
                if (hc==200 and health_ok) or disk_only_warning:
                    if disk_only_warning:
                        h=dict(h);h["agent_override"]="disk_warning_noncritical";h["ok"]=True
                    return v,h
                last={"version":v,"health":h,"health_http_status":hc}
                if hc==503 and critical_errors:
                    raise RuntimeError(f"MES critical health failure: {critical_errors}. Full response: {h}")
        elif port_open() and time.time()>end-20:
            # A process owns the port but cannot serve the system API; do not wait the full timeout.
            last={**last,"port_8080":True}
    detail=tail(mes_log,80)
    raise RuntimeError(f"MES verification timeout after {timeout}s. Last response: {last}. MES log tail:\n{detail or '[empty mes_process.log]'}")

def rollback(log,failed_version):
    set_step("rollback","running","Automatic rollback")
    current=TARGET_HOME/"app/current";old=TARGET_HOME/"app/current.old";failed=TARGET_HOME/"app"/f"failed_{failed_version}_{datetime.now():%Y%m%d_%H%M%S}"
    stop_mes(log)
    if not old.is_dir():raise RuntimeError("Rollback unavailable: current.old not found")
    if current.exists():current.replace(failed)
    old.replace(current)
    previous=(current/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
    marker=TARGET_HOME/"config/deployment.json";rid="rollback-"+uuid.uuid4().hex
    save_json(marker,{"deployment_id":rid,"version":previous,"channel":"rollback","deployed_at":datetime.now().astimezone().isoformat(timespec="seconds")})
    pid=start_mes(log)
    try:
        wait_verify(previous,rid,60,require_health=False,pid=pid)
    except Exception:
        # Legacy releases before protocol v2 do not expose /api/system/version.
        # Confirm that the old process is alive and VERSION.txt matches instead.
        end=time.time()+45
        while time.time()<end:
            time.sleep(2)
            _, code=http_json("/api/health",5)
            if code==200 and current_file_version().lstrip("Vv")==previous.lstrip("Vv"):
                break
        else:
            raise
    set_step("rollback","success",f"Rolled back to {previous}");return previous
PERSISTENT_MES_NAMES={".env","certs","runtime"}
FIRST_INSTALL_BACKUP_MARKER=".mesflow_first_install_baseline"

def _validate_persistent_paths():
    TARGET_HOME.mkdir(parents=True,exist_ok=True)
    runtime=TARGET_HOME/"runtime"
    runtime.mkdir(parents=True,exist_ok=True)
    if runtime.is_symlink() or not runtime.is_dir(): raise RuntimeError(f"Unsafe persistent runtime path: {runtime}")
    env=TARGET_HOME/".env"
    if not env.is_file(): raise RuntimeError(f"Missing persistent environment file: {env}")
    postgres=(runtime/"postgres-v65")
    if postgres.exists() and (postgres.is_symlink() or not postgres.is_dir()): raise RuntimeError(f"Unsafe PostgreSQL path: {postgres}")
    return {"runtime":str(runtime),"env":str(env),"postgres":str(postgres)}

def _copy_release_tree(source, destination):
    """Copy replaceable release files only. Persistent package entries are ignored."""
    source=Path(source); destination=Path(destination); destination.mkdir(parents=True,exist_ok=True)
    for x in source.iterdir():
        if x.name=="agent_manifest.json" or x.name in PERSISTENT_MES_NAMES: continue
        dst=destination/x.name
        if x.is_dir(): shutil.copytree(x,dst,symlinks=True)
        elif x.is_file(): shutil.copy2(x,dst)

def _stage_linux_release(release, log):
    persistent=_validate_persistent_paths()
    env_stat=(TARGET_HOME/".env").stat()
    log.write(f"PERSISTENT ENV OK path={persistent['env']} mode={env_stat.st_mode & 0o777:04o} uid={env_stat.st_uid} gid={env_stat.st_gid}; secrets not logged\n")
    log.flush()
    stage=DATA_DIR/"staging"/f"mes_{uuid.uuid4().hex}"
    stage.parent.mkdir(parents=True,exist_ok=True); stage.mkdir()
    _copy_release_tree(release,stage)
    for name in PERSISTENT_MES_NAMES:
        src=TARGET_HOME/name
        if src.exists(): (stage/name).symlink_to(src,target_is_directory=src.is_dir())
    if not (stage/"compose.yml").is_file(): raise RuntimeError("Staged release has no compose.yml")
    image_meta=load_json(release/"release.json",{})
    image_mode=image_meta.get("type")==IMAGE_RELEASE_TYPE
    image_ref=None
    if image_mode:
        log.write("IMAGE RELEASE MODE: source build disabled\n");log.flush()
        image_ref=_verify_and_prepare_image_release(release,image_meta,log)
    log.write("COMPOSE VALIDATION START\n"); log.flush()
    _compose_at(stage,log,["config","--quiet"],timeout=COMPOSE_VALIDATE_TIMEOUT,env_extra={"MESFLOW_IMAGE":image_ref} if image_ref else None)
    log.write("COMPOSE VALIDATION DONE\nBUILD START\nBUILD IMAGE mesflow\n"); log.flush()
    if image_mode:
        log.write(f"IMAGE VERIFIED {image_ref}; no server-side docker build\n"); log.flush()
    else:
        started=time.monotonic(); _compose_at(stage,log,["build","mesflow"],timeout=BUILD_TIMEOUT)
        log.write(f"BUILD DONE elapsed={time.monotonic()-started:.1f}s\n"); log.flush()
    # Static migration graph validation only; never connects to or changes PostgreSQL.
    log.write("MIGRATION CHECK START (Alembic heads; no migration is run)\n"); log.flush()
    _compose_at(stage,log,["run","--rm","--no-deps","--entrypoint","alembic","mesflow","heads"],timeout=COMPOSE_VALIDATE_TIMEOUT,env_extra={"MESFLOW_IMAGE":image_ref} if image_ref else None)
    log.write("MIGRATION CHECK DONE\n"); log.flush()
    log.write(f"Staging validated at {stage}; persistent package entries excluded\n");log.flush()
    return stage

def _create_linux_backup(log):
    """Back up the current replaceable MES source.

    A brand-new server legitimately has persistent .env/runtime plus the
    bootstrap PostgreSQL compose, but no application compose.yml yet.  That
    state is recorded as a first-install baseline instead of being rejected.
    """
    backup=DATA_DIR/"mes_backups"/f"source_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    backup.parent.mkdir(parents=True,exist_ok=True); backup.mkdir()
    for x in TARGET_HOME.iterdir():
        if x.name in PERSISTENT_MES_NAMES: continue
        dst=backup/x.name
        if x.is_dir(): shutil.copytree(x,dst,symlinks=True)
        elif x.is_file(): shutil.copy2(x,dst)
    if not (backup/"compose.yml").is_file():
        # Valid only for first application install.  Preserve any bootstrap
        # files (notably compose.bootstrap.yml) so rollback can return to the
        # postgres-only pre-application state.
        (backup/FIRST_INSTALL_BACKUP_MARKER).write_text(
            "No application compose.yml existed before this deployment.\n",
            encoding="utf-8",
        )
        log.write(f"Backed up first-install baseline to {backup}; application compose.yml not present yet\n")
        log.flush()
        return backup
    log.write(f"Backed up exact MES source to {backup}\n");log.flush()
    return backup

def _replace_nonpersistent(source):
    source=Path(source)
    for x in list(TARGET_HOME.iterdir()):
        if x.name in PERSISTENT_MES_NAMES: continue
        if x.is_dir(): shutil.rmtree(x)
        else: x.unlink(missing_ok=True)
    _copy_release_tree(source,TARGET_HOME)
    if not (TARGET_HOME/"compose.yml").is_file(): raise RuntimeError("Cutover did not install compose.yml")

def _save_deploy_tx(**values):
    tx=load_json(DEPLOY_TX_FILE,{})
    tx.update(values); save_json(DEPLOY_TX_FILE,tx); return tx

def _stop_application_tier(log, root=None, image_ref=None):
    root=Path(root or TARGET_HOME)
    _compose_at(root,log,["stop","mesflow"],check=False,timeout=120,env_extra={"MESFLOW_IMAGE":image_ref} if image_ref else None)

def _start_stack(log, root=None, build=False, image_ref=None):
    root=Path(root or TARGET_HOME); args=["up","-d"]
    if not build: args.append("--no-build")
    # PostgreSQL is persistent infrastructure.  It must already be running
    # (bootstrap/operations owns that lifecycle); deployment never recreates
    # it just because the staged application Compose project has a new hash.
    postgres_state=(_docker_mes_state().get("service_health") or {}).get("postgres")
    if postgres_state not in {"healthy","none"}:
        raise RuntimeError(f"Persistent PostgreSQL is not healthy ({postgres_state or 'not running'}); refusing to start/recreate it during application deployment")
    # --no-deps is intentional: only the replaceable application tier is
    # started.  Gateway and PostgreSQL are neither recreated nor reset.
    args.extend(["--no-deps","mesflow"])
    _compose_at(root,log,args,check=True,timeout=300,env_extra={"MESFLOW_IMAGE":image_ref} if image_ref else None)

def _rollback_linux_source(backup, expected_version, log):
    set_step("rollback","running",f"Restoring exact source {backup}")
    backup=Path(backup)
    first_install=(backup/FIRST_INSTALL_BACKUP_MARKER).is_file()
    if (TARGET_HOME/"compose.yml").is_file():
        try:
            _stop_application_tier(log,image_ref=None)
        except Exception as ex:
            log.write(f"Rollback stop warning: {ex}\n")
            log.flush()
    elif first_install:
        log.write("FIRST INSTALL ROLLBACK: no application compose.yml to stop\n")
        log.flush()
    _replace_nonpersistent(backup)

    if first_install:
        # There was no MES application before this deployment.  Roll back to
        # the bootstrap postgres-only state, not to a nonexistent app.
        marker=TARGET_HOME/FIRST_INSTALL_BACKUP_MARKER
        marker.unlink(missing_ok=True)
        bootstrap=TARGET_HOME/"compose.bootstrap.yml"
        if bootstrap.is_file():
            _compose_at(TARGET_HOME,log,["-f","compose.bootstrap.yml","up","-d","postgres"],check=True,timeout=180)
        set_step("rollback","success","ROLLBACK SUCCESS: restored first-install postgres-only baseline")
        return

    _start_stack(log,build=False)
    deadline=time.time()+MES_VERIFY_TIMEOUT; last={}
    while time.time()<deadline:
        time.sleep(2); st=mes_status(); last=st
        if canonical_version(st.get("version"))==canonical_version(expected_version) and st.get("healthy"):
            set_step("rollback","success",f"ROLLBACK SUCCESS: {expected_version}"); return
    set_step("rollback","failed",f"ROLLBACK FAILED: {last}")
    raise RuntimeError(f"Rollback healthcheck failed for {expected_version}: {last}")

def _copy_linux_release(release, log):
    """Install Docker release into /opt/mesflow while preserving persistent runtime."""
    TARGET_HOME.mkdir(parents=True,exist_ok=True)
    backup=_create_linux_backup(log)
    _replace_nonpersistent(release)
    return backup

def _restore_linux_source(backup, log):
    backup=Path(backup)
    if (backup/FIRST_INSTALL_BACKUP_MARKER).is_file():
        _rollback_linux_source(backup,"FIRST_INSTALL",log)
        return
    _rollback_linux_source(backup,(backup/"VERSION.txt").read_text(encoding="utf-8-sig").strip(),log)

def run_job(version, dry_run=False):
    release=RELEASES_DIR/version
    release_meta=load_json(release/"release.json",{})
    image_ref=f"{release_meta.get('image')}@{release_meta.get('image_digest')}" if release_meta.get("type")==IMAGE_RELEASE_TYPE else None
    if image_ref:
        release_meta["image_ref"]=image_ref
    # The release's VERSION.txt is authoritative. Do not verify against a stale
    # route/UI label or filename-derived value.
    release_version=norm((release/"VERSION.txt").read_text(encoding="utf-8-sig")) if (release/"VERSION.txt").is_file() else norm(version)
    expected=canonical_version(release_version)
    requested=canonical_version(version)
    if requested != expected:
        raise RuntimeError(f"Release version mismatch before deploy: requested={requested!r}, VERSION.txt={expected!r}")
    deployment_id=uuid.uuid4().hex; prefix="dry_run" if dry_run else "deploy"; logpath=LOG_DIR/f"{prefix}_{release_version}_{datetime.now():%Y%m%d_%H%M%S}.log";before=mes_status().get("version",current_file_version())
    def begin(s):s["job"]={"type":prefix,"dry_run":dry_run,"version":version,"from_version":before,"deployment_id":deployment_id,"status":"queued","phase":"queued","started_at":now_text(),"finished_at":None,"log_file":str(logpath),"message":"Queued","steps":{},"transitions":[{"status":"uploading","at":now_text(),"message":"ZIP received"},{"status":"validating","at":now_text(),"message":"ZIP contract validated"},{"status":"queued","at":now_text(),"message":"Deploy worker queued"}]}
    update_state(begin);append_log(f"Deploy {before} -> {version}, id={deployment_id}")
    status="failed";message="";rc=-1;rolled="";backup=None;stage=None;cutover=False
    try:
        with logpath.open("w",encoding="utf-8") as log:
            if os.name!="nt" and (release/"compose.yml").is_file():
                set_job_phase("validating","Validating compose, package contract and migration chain")
                set_step("stage","running","Staging release; production remains online")
                set_job_phase("building","Building MESFlow image; this can take several minutes")
                stage=_stage_linux_release(release,log)
                set_step("stage","success",f"Validated stage: {stage}")
                if dry_run:
                    set_step("backup","skipped","Dry-run: no production backup/cutover")
                    set_step("install","skipped","Dry-run: production source unchanged")
                    set_step("restart","skipped","Dry-run: production stack not stopped")
                    set_step("health","success","Dry-run validation passed; production untouched")
                    set_step("rollback","skipped","Not required")
                    status="success";message=f"Dry-run verified: {expected}; production unchanged";rc=0
                else:
                    set_job_phase("deploying","Creating backup and updating application only; PostgreSQL stays running")
                    set_step("backup","running","Backing up exact current MES source")
                    backup=_create_linux_backup(log)
                    set_step("backup","success",f"Backup: {backup}")
                    _save_deploy_tx(status="prepared",phase="BACKUP",version=version,from_version=before,backup=str(backup),stage=str(stage),deployment_id=deployment_id)
                    set_step("restart","running","CUTOVER: stopping application only; gateway and PostgreSQL remain running")
                    cutover=True; _save_deploy_tx(status="running",phase="CUTOVER")
                    if (TARGET_HOME/"compose.yml").is_file():
                        _stop_application_tier(log,image_ref=image_ref)
                    else:
                        log.write("FIRST INSTALL: no existing application compose.yml; skip application stop\n")
                        log.flush()
                    _replace_nonpersistent(stage)
                    if image_ref:
                        save_json(TARGET_HOME/"release.json",{**release_meta,"image_ref":image_ref})
                    set_step("install","success",f"Installed staged Docker release files for {version}")
                    _save_deploy_tx(phase="START")
                    _start_stack(log,build=False,image_ref=image_ref);set_step("restart","success","MES Docker stack started; PostgreSQL data preserved")
                    set_job_phase("verifying",f"Checking container, API, database and version for up to {MES_VERIFY_TIMEOUT}s")
                    set_step("health","running",f"Waiting up to {MES_VERIFY_TIMEOUT}s for MES")
                    end=time.time()+MES_VERIFY_TIMEOUT;last={}
                    while time.time()<end:
                        time.sleep(2)
                        st=mes_status(); v=st.get("version_payload") or {}; h=st.get("health_payload") or {}; ds=st.get("docker") or {}
                        actual=canonical_version(st.get("version"))
                        last={"expected_version":expected,"resolved_version":actual,"version":v,"health":h,"docker":ds}
                        checks=h.get("checks") if isinstance(h.get("checks"),dict) else {}
                        database_ok=bool((checks.get("database") or {}).get("ok")) or h.get("database_backend")=="postgresql"
                        api_ok=bool(st.get("api_online")) and st.get("health_state") in ("healthy","warning")
                        docker_ok=bool(ds.get("running")) and ds.get("health") in ("healthy","none")
                        log.write(f"VERIFY expected={expected!r} actual={actual!r} api_ok={api_ok} docker_ok={docker_ok} database_ok={database_ok} health_state={st.get('health_state')!r} docker_health={ds.get('health')!r}\n"); log.flush()
                        if actual == expected and api_ok and docker_ok and database_ok: break
                    else: raise RuntimeError(f"MES verification timeout. expected={expected!r}, actual={last.get('resolved_version')!r}. Last response: {last}")
                    set_step("health","success",f"Version {expected} and health verified");set_step("version","success",f"Running version {expected}");set_step("rollback","skipped","Not required")
                    _save_deploy_tx(status="success",phase="SUCCESS",finished_at=now_text())
                    status="success";message=f"Deployment verified: {expected}";rc=0
            else:
                stop_mes(log)
                set_step("backup","running","Installing legacy release and backing up database")
                cmd=[sys.executable,str(release/"install/deploy_release.py"),"--home",str(TARGET_HOME),"--source",str(release),"--deployment-id",deployment_id,"--channel",load_json(release/"release.json",{}).get("channel","stable")]
                env=os.environ.copy();env["PYTHONUTF8"]="1";rc=subprocess.run(cmd,cwd=str(release),stdout=log,stderr=subprocess.STDOUT,text=True,env=env,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)).returncode
                if rc:raise RuntimeError(f"Installer exited with code {rc}")
                pid=start_mes(log);set_step("restart","success",f"MES started, PID {pid}")
                wait_verify(version,deployment_id,MES_VERIFY_TIMEOUT,pid=pid);set_step("health","success","Health verified")
                status="success";message=f"Deployment verified: {version}";rc=0
    except Exception as e:
        message=str(e);append_log(f"MES deploy failed stage={((load_state().get('job') or {}).get('phase','unknown'))} exception={type(e).__name__} error={message}")
        if os.name!="nt" and cutover and backup:
            try:
                with logpath.open("a",encoding="utf-8") as log:
                    _save_deploy_tx(status="rollback",phase="ROLLBACK")
                    _rollback_linux_source(backup,before,log);rolled=before;message += f"; ROLLBACK SUCCESS to {before}"
                    _save_deploy_tx(status="rolled_back",phase="ROLLBACK_SUCCESS",finished_at=now_text())
            except Exception as re:
                _save_deploy_tx(status="rollback_failed",phase="ROLLBACK_FAILED",error=str(re),finished_at=now_text())
                message += f"; ROLLBACK FAILED: {re}"
    if not dry_run and image_ref:
        # Image-release deploy (the Build-Once/Promote-Same-Artifact path):
        # record LOCAL_PASS/FAILED from real evidence (image id, schema,
        # smoke check), never from the job return code alone.
        try: _record_local_deploy_result(version,status=="success",message)
        except Exception as ex: append_log(f"LOCAL_PASS recording failed: {type(ex).__name__}: {ex}")
    def done(s):
        j=s.get("job") or {};j.update(status=status,phase=status,finished_at=now_text(),return_code=rc,message=message,rolled_back_to=rolled)
        j.setdefault("transitions",[]).append({"status":status,"at":now_text(),"message":message})
        s["job"]=j;s.setdefault("history",[]).insert(0,dict(j));s["history"]=s["history"][:50]
    try:update_state(done);append_log(f"Deploy finished status={status}: {message}")
    finally:
        if stage and Path(stage).exists(): shutil.rmtree(stage,ignore_errors=True)
        DEPLOY_LOCK_FILE.unlink(missing_ok=True)
        if _job_lock.locked():_job_lock.release()

def start_deploy(v, dry_run=False):
    if not dry_run and not DEPLOY_ENABLED:
        raise RuntimeError("Production deploy is disabled for this Agent runtime. Set MESFLOW_AGENT_DEPLOY_ENABLED=1 only in a reviewed full-deploy environment.")
    if not (RELEASES_DIR/v).is_dir():raise FileNotFoundError("Release not found")
    job_running()
    if not _job_lock.acquire(False):raise RuntimeError("Another deployment is running")
    file_lock_acquired=False
    try:
        try:
            fd=os.open(str(DEPLOY_LOCK_FILE),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            os.write(fd,f"{os.getpid()} {now_text()}\n".encode());os.close(fd)
            file_lock_acquired=True
        except FileExistsError:
            raise RuntimeError("Another deployment lock exists; inspect deploy transaction before clearing it")
        def worker():
            # Keep the durable state and locks correct even if an unexpected
            # error happens before run_job enters its own try/finally block.
            try:
                run_job(v,dry_run)
            except Exception as ex:
                message=f"Unexpected deployment worker failure: {type(ex).__name__}: {ex}"
                append_log(f"MES deploy worker failed stage=queued exception={type(ex).__name__} error={ex}")
                def failed(s):
                    j=s.get("job") or {"type":"deploy","version":v,"started_at":now_text(),"steps":{},"transitions":[]}
                    j.update(status="failed",phase="failed",finished_at=now_text(),return_code=-1,message=message)
                    j.setdefault("transitions",[]).append({"status":"failed","at":now_text(),"message":message})
                    s["job"]=j; s.setdefault("history",[]).insert(0,dict(j)); s["history"]=s["history"][:50]
                update_state(failed)
                DEPLOY_LOCK_FILE.unlink(missing_ok=True)
                if _job_lock.locked(): _job_lock.release()
        threading.Thread(target=worker,daemon=True).start()
    except Exception:
        if file_lock_acquired: DEPLOY_LOCK_FILE.unlink(missing_ok=True)
        _job_lock.release()
        raise

def recover_interrupted_deploy():
    """Recover a process crash after CUTOVER using the durable transaction journal."""
    tx=load_json(DEPLOY_TX_FILE,{})
    if tx.get("status") not in {"running","rollback"} or tx.get("phase") not in {"CUTOVER","START","ROLLBACK"}:
        DEPLOY_LOCK_FILE.unlink(missing_ok=True); return False
    backup=Path(tx.get("backup","") or "/nonexistent")
    valid_backup=backup.is_dir() and (
        (backup/"compose.yml").is_file() or
        (backup/FIRST_INSTALL_BACKUP_MARKER).is_file()
    )
    if not valid_backup:
        append_log(f"ROLLBACK FAILED after Agent restart: invalid backup {backup}")
        _save_deploy_tx(status="rollback_failed",phase="ROLLBACK_FAILED",error="backup unavailable",finished_at=now_text())
        DEPLOY_LOCK_FILE.unlink(missing_ok=True); return False
    recovery_log=LOG_DIR/f"recovery_{datetime.now():%Y%m%d_%H%M%S}.log"
    try:
        with recovery_log.open("w",encoding="utf-8") as log:
            if (backup/FIRST_INSTALL_BACKUP_MARKER).is_file():
                old=tx.get("from_version") or "FIRST_INSTALL"
            else:
                old=tx.get("from_version") or (backup/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
            _rollback_linux_source(backup,old,log)
        _save_deploy_tx(status="rolled_back",phase="ROLLBACK_SUCCESS",recovered_after_restart=True,finished_at=now_text())
        append_log("ROLLBACK SUCCESS after Agent restart"); return True
    except Exception as ex:
        _save_deploy_tx(status="rollback_failed",phase="ROLLBACK_FAILED",error=str(ex),finished_at=now_text())
        append_log(f"ROLLBACK FAILED after Agent restart: {ex}"); return False
    finally: DEPLOY_LOCK_FILE.unlink(missing_ok=True)

def locate_qa_root(e):
    c=[]
    if (e/"VERSION").is_file(): c.append(e)
    c += [x for x in e.iterdir() if x.is_dir() and (x/"VERSION").is_file()]
    if len(c)!=1: raise ValueError("QA ZIP must contain exactly one root with VERSION")
    r=c[0]
    for q in (r/"agent.py", r/"requirements.txt", r/"templates"):
        if not q.exists(): raise ValueError(f"Missing QA file/folder: {q.relative_to(r)}")
    return r

def _qa_source_dir():
    current=QA_HOME/"current"
    if (current/"agent.py").is_file() and (current/"VERSION").is_file():
        return current
    # Legacy standalone QA installer placed the app directly in QA_HOME.
    if (QA_HOME/"agent.py").is_file() and (QA_HOME/"VERSION").is_file():
        return QA_HOME
    return current

def qa_current_version():
    src=_qa_source_dir()
    p=src/"VERSION"
    return p.read_text(encoding="utf-8-sig").strip() if p.is_file() else "Chưa cài"

def qa_http_json(path, timeout=4):
    try:
        with urllib.request.urlopen(QA_URL+path, timeout=timeout) as r:
            raw=r.read().decode("utf-8",errors="replace")
            try: payload=json.loads(raw)
            except json.JSONDecodeError: payload={"ok":False,"error":"Invalid JSON response","body":raw[:1000]}
            return payload,r.status
    except urllib.error.HTTPError as e:
        raw=e.read().decode("utf-8",errors="replace")
        try: payload=json.loads(raw)
        except json.JSONDecodeError: payload={"ok":False,"error":str(e),"body":raw[:1000]}
        return payload,e.code
    except Exception as e:
        return {"ok":False,"error":str(e)},0

def _qa_service_state():
    if RUNTIME_MODE=="docker-linux":
        try:
            r=subprocess.run(["docker","inspect","mesflow-qa-center"],capture_output=True,text=True,timeout=8)
            if r.returncode!=0:
                return {"active":False,"pid":0,"runtime":"docker"}
            payload=json.loads(r.stdout)[0]
            state=payload.get("State",{})
            running=bool(state.get("Running"))
            pid=int(state.get("Pid") or 0)
            health=(state.get("Health") or {}).get("Status","")
            return {"active":running,"pid":pid,"runtime":"docker","health":health}
        except Exception:
            return {"active":False,"pid":0,"runtime":"docker"}
    if os.name=="nt": return {"active":False,"pid":0}
    try:
        a=subprocess.run(["systemctl","is-active","mesflow-qa-center.service"],capture_output=True,text=True,timeout=8)
        q=subprocess.run(["systemctl","show","-p","MainPID","--value","mesflow-qa-center.service"],capture_output=True,text=True,timeout=8)
        pid=int((q.stdout or "0").strip() or 0)
        return {"active":a.returncode==0 and (a.stdout or "").strip()=="active","pid":pid,"runtime":"systemd"}
    except Exception:
        return {"active":False,"pid":0}

def qa_status():
    v,code=qa_http_json("/api/version",3)
    actual=str(v.get("version") or qa_current_version())
    svc=_qa_service_state()
    return {"online": code==200 and bool(v.get("ok",True)), "version":actual, "payload":v, "port":QA_PORT, "home":str(QA_HOME), "service_active":svc.get("active",False), "service_pid":svc.get("pid",0)}

def list_qa_releases():
    out=[]
    for x in QA_RELEASES_DIR.iterdir():
        if x.is_dir() and not x.name.startswith('.'):
            m=load_json(x/"agent_manifest.json",{})
            out.append({"version":x.name,"uploaded_at":m.get("uploaded_at",""),"source_name":m.get("source_name","")})
    return sorted(out,key=lambda z:z["uploaded_at"],reverse=True)

def qa_job_running():
    j=(load_state().get("qa_job") or {})
    return j.get("status")=="running"

def set_qa_step(name,status,message=""):
    def f(st):
        j=st.get("qa_job") or {}; steps=j.setdefault("steps",{})
        steps[name]={"status":status,"message":message,"at":now_text()}; j["message"]=message or name; st["qa_job"]=j
    update_state(f)

def _qa_runtime_signature(source: Path) -> str:
    manifest=source/"docker"/"runtime-manifest.json"
    if not manifest.is_file():
        # Legacy QA package: force build.
        return ""
    try:
        meta=json.loads(manifest.read_text(encoding="utf-8"))
        files=list(meta.get("signature_files") or [])
    except Exception:
        return ""
    h=hashlib.sha256()
    for rel in files:
        p=source/str(rel)
        if not p.is_file():
            return ""
        h.update(str(rel).encode("utf-8")); h.update(b"\\0"); h.update(p.read_bytes()); h.update(b"\\0")
    return h.hexdigest()


def _qa_runtime_image_exists(source: Path) -> bool:
    compose=source/"docker"/"compose.yml"
    if not compose.is_file():
        return False
    # Resolve image from compose config rather than hard-coding.
    try:
        r=subprocess.run(["docker","compose","-p","mesflow-qa","-f",str(compose),"config","--images"],
                         cwd=str(source),capture_output=True,text=True,timeout=30)
        images=[x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
        if r.returncode!=0 or not images:
            return False
        for image in images:
            q=subprocess.run(["docker","image","inspect",image],capture_output=True,text=True,timeout=15)
            if q.returncode!=0:
                return False
        return True
    except Exception:
        return False


def _qa_compose_file():
    current=QA_HOME/"current"
    p=current/"docker"/"compose.yml"
    if not p.is_file():
        raise RuntimeError(f"QA Docker package missing: {p}")
    return p

def _qa_compose(args, log=None, check=True, timeout=900):
    cmd=["docker","compose","-p","mesflow-qa","-f",str(_qa_compose_file())]+list(args)
    return subprocess.run(cmd,cwd=str(QA_HOME/"current"),check=check,stdout=log or subprocess.PIPE,
                          stderr=subprocess.STDOUT if log else subprocess.PIPE,text=True,timeout=timeout)

def _qa_docker_stop(log=None):
    if not (QA_HOME/"current"/"docker"/"compose.yml").is_file():
        return
    _qa_compose(["stop","mesflow-qa-center"],log=log,check=False,timeout=60)

def _qa_docker_start(log=None, build=True):
    args=["up","-d"]
    if build:
        args.append("--build")
    else:
        args.extend(["--no-build","--force-recreate"])
    args.append("mesflow-qa-center")
    if log:
        log.write(("QA runtime rebuild required\n" if build else "FAST QA DEPLOY: runtime unchanged; skipping Docker build\n"))
        log.flush()
    _qa_compose(args,log=log,check=True,timeout=1200 if build else 180)
    # Do not return Docker host PID. The Deploy Agent runs in its own PID
    # namespace, so process_alive(host_pid) is invalid and caused false
    # "QA Center process exited" failures even when the QA container was healthy.
    return 0

def _stop_qa(timeout=30):
    end=time.time()+timeout
    if RUNTIME_MODE=="docker-linux":
        _qa_docker_stop()
        return
    if os.name!="nt":
        subprocess.run(["systemctl","stop","mesflow-qa-center.service"],capture_output=True,text=True,timeout=20)
    meta=load_json(QA_HOME/"qa_process.json",{})
    pids=set(port_pids(QA_PORT))
    if str(meta.get("pid","")).isdigit(): pids.add(int(meta["pid"]))
    for pid in sorted(pids):
        if pid==os.getpid(): continue
        try:
            if os.name=="nt": subprocess.run(["taskkill","/PID",str(pid),"/T","/F"],capture_output=True,text=True,timeout=15,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            else: os.kill(pid,15)
        except Exception: pass
    while time.time()<end:
        svc=_qa_service_state()
        if not port_open(port=QA_PORT) and not svc.get("active"):
            return
        time.sleep(1)
    for pid in sorted(set(port_pids(QA_PORT))|pids):
        try:
            if os.name!="nt": os.kill(pid,9)
        except Exception: pass
    time.sleep(1)
    if port_open(port=QA_PORT): raise RuntimeError(f"QA Center did not stop; port {QA_PORT} is still open")

def _qa_python():
    return QA_HOME/(".venv/Scripts/python.exe" if os.name=="nt" else ".venv/bin/python")

def _ensure_qa_venv(release, log):
    py=_qa_python()
    if not py.is_file():
        QA_HOME.mkdir(parents=True,exist_ok=True)
        subprocess.run([sys.executable,"-m","venv",str(QA_HOME/".venv")],check=True,stdout=log,stderr=subprocess.STDOUT,text=True)
    subprocess.run([str(py),"-m","pip","install","--disable-pip-version-check","-r",str(release/"requirements.txt")],check=True,stdout=log,stderr=subprocess.STDOUT,text=True)
    return py

def _write_qa_systemd_service():
    if os.name=="nt": return
    current=QA_HOME/"current"
    py=_qa_python(); entry=current/"agent.py"
    unit=Path("/etc/systemd/system/mesflow-qa-center.service")
    text=f"""[Unit]
Description=MESFlow QA Center managed by MESFlow Deploy Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory={current}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONUTF8=1
Environment=MESFLOW_QA_HOST=0.0.0.0
Environment=MESFLOW_QA_PORT={QA_PORT}
ExecStart={py} {entry}
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""
    unit.write_text(text,encoding="utf-8")
    subprocess.run(["systemctl","daemon-reload"],check=True,capture_output=True,text=True,timeout=20)
    subprocess.run(["systemctl","enable","mesflow-qa-center.service"],check=True,capture_output=True,text=True,timeout=20)

def _start_qa(log=None, build=True):
    current=QA_HOME/"current"; entry=current/"agent.py"
    if not entry.is_file(): raise RuntimeError(f"Missing QA entry: {entry}")
    if RUNTIME_MODE=="docker-linux":
        return _qa_docker_start(log=log,build=build)
    py=_qa_python()
    if not py.is_file(): raise RuntimeError(f"Missing QA Python: {py}")
    if os.name!="nt":
        _write_qa_systemd_service()
        r=subprocess.run(["systemctl","restart","mesflow-qa-center.service"],capture_output=True,text=True,timeout=30)
        if r.returncode!=0:
            raise RuntimeError("Cannot start mesflow-qa-center.service: "+((r.stderr or r.stdout or "").strip()))
        time.sleep(0.5)
        svc=_qa_service_state(); pid=int(svc.get("pid") or 0)
        save_json(QA_HOME/"qa_process.json",{"pid":pid,"started_at":now_text(),"version":qa_current_version(),"log":"journalctl -u mesflow-qa-center.service"})
        if log: log.write(f"Started QA Center systemd service PID {pid} on port {QA_PORT}\n"); log.flush()
        return pid
    env=os.environ.copy(); env.update({"MESFLOW_QA_HOST":"0.0.0.0","MESFLOW_QA_PORT":str(QA_PORT),"PYTHONUTF8":"1"})
    flags=getattr(subprocess,"CREATE_NEW_PROCESS_GROUP",0)|getattr(subprocess,"DETACHED_PROCESS",0)|getattr(subprocess,"CREATE_NO_WINDOW",0)
    h=QA_LOG.open("a",encoding="utf-8")
    proc=subprocess.Popen([str(py),str(entry)],cwd=str(current),env=env,stdout=h,stderr=subprocess.STDOUT,creationflags=flags,close_fds=True)
    save_json(QA_HOME/"qa_process.json",{"pid":proc.pid,"started_at":now_text(),"version":qa_current_version(),"log":str(QA_LOG)})
    if log: log.write(f"Started QA Center PID {proc.pid} on port {QA_PORT}\n"); log.flush()
    return proc.pid

def _qa_docker_log_tail(lines=80):
    try:
        r=subprocess.run(["docker","logs","--tail",str(lines),"mesflow-qa-center"],
                         capture_output=True,text=True,timeout=10)
        return ((r.stdout or "")+(r.stderr or "")).strip()
    except Exception as ex:
        return f"<docker logs unavailable: {ex}>"

def _wait_qa(version,timeout=None,pid=None):
    end=time.time()+(timeout or QA_VERIFY_TIMEOUT); last={}
    while time.time()<end:
        time.sleep(1)
        if RUNTIME_MODE=="docker-linux":
            svc=_qa_service_state()
            if not svc.get("active"):
                raise RuntimeError("QA Center container exited. Docker log tail:\n"+_qa_docker_log_tail(80))
        elif pid and not process_alive(pid) and not port_open(port=QA_PORT):
            raise RuntimeError("QA Center process exited. Log tail:\n"+tail(QA_LOG,80))
        v,code=qa_http_json("/api/version",3); last=v
        if code==200 and str(v.get("version","")).lstrip("Vv")==version.lstrip("Vv"):
            return v
    extra=_qa_docker_log_tail(80) if RUNTIME_MODE=="docker-linux" else tail(QA_LOG,80)
    raise RuntimeError(f"QA Center verify timeout. Last response: {last}. Log tail:\n{extra}")

def _preserve_qa_data(old_current,new_current):
    # Keep operator configuration and reports across QA upgrades, including legacy root installs.
    if not old_current.is_dir(): return
    for name in ("config.json","reports","runtime"):
        src=old_current/name; dst=new_current/name
        if not src.exists(): continue
        if dst.exists(): shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
        shutil.copytree(src,dst) if src.is_dir() else shutil.copy2(src,dst)

def run_qa_job(version):
    release=QA_RELEASES_DIR/version; logpath=LOG_DIR/f"qa_deploy_{version}_{datetime.now():%Y%m%d_%H%M%S}.log"; before=qa_current_version()
    def begin(st): st["qa_job"]={"type":"qa_deploy","version":version,"from_version":before,"status":"running","started_at":now_text(),"finished_at":None,"log_file":str(logpath),"message":"Starting","steps":{}}
    update_state(begin); append_log(f"QA deploy {before} -> {version}")
    status="failed"; message=""; rc=-1; rolled=""
    current=QA_HOME/"current"; previous=QA_HOME/"previous"; staging=QA_HOME/f"staging_{uuid.uuid4().hex[:8]}"
    old_source=_qa_source_dir()
    old_runtime_sig=_qa_runtime_signature(old_source) if old_source.is_dir() else ""
    new_runtime_sig=_qa_runtime_signature(release)
    runtime_image_ready=_qa_runtime_image_exists(old_source) if old_source.is_dir() else False
    fast_deploy=bool(old_runtime_sig and new_runtime_sig and old_runtime_sig==new_runtime_sig and runtime_image_ready)
    try:
        with logpath.open("a",encoding="utf-8") as log:
            set_qa_step("stop","skipped","Blue/green-style source staging; existing QA stays up until recreate")
            set_qa_step("backup","running","Backing up current QA Center")
            if previous.exists(): shutil.rmtree(previous,ignore_errors=True)
            if old_source.is_dir() and (old_source/"agent.py").is_file():
                ignore=shutil.ignore_patterns(".venv","current","previous","staging_*","qa_process.json","__pycache__",".pytest_cache")
                shutil.copytree(old_source,previous,ignore=ignore)
            set_qa_step("backup","success",f"Backup: {before}")
            set_qa_step("install","running","Installing QA release and dependencies")
            shutil.copytree(release,staging,ignore=shutil.ignore_patterns("agent_manifest.json","__pycache__",".pytest_cache"))
            _preserve_qa_data(old_source,staging)
            if RUNTIME_MODE!="docker-linux":
                _ensure_qa_venv(staging,log)
            if current.exists(): shutil.rmtree(current)
            staging.replace(current)
            set_qa_step("install","success",f"Installed QA {version}")
            set_qa_step("start","running","Fast recreate" if fast_deploy else "Building runtime and starting QA Center")
            pid=_start_qa(log,build=not fast_deploy)
            set_qa_step("start","success","FAST DEPLOY" if fast_deploy else f"Runtime rebuilt; PID {pid}")
            set_qa_step("health","running","Verifying /api/version")
            _wait_qa(version,pid=pid); set_qa_step("health","success",f"QA {version} healthy")
            set_qa_step("rollback","skipped","Not required")
            status="success"; message=f"QA deployment verified: {version}"; rc=0
    except Exception as ex:
        message=str(ex); append_log(f"QA deploy failed: {ex}")
        try:
            if staging.exists(): shutil.rmtree(staging,ignore_errors=True)
            if previous.is_dir():
                set_qa_step("rollback","running","Restoring previous QA Center")
                _stop_qa()
                if current.exists(): shutil.rmtree(current,ignore_errors=True)
                shutil.copytree(previous,current)
                with logpath.open("a",encoding="utf-8") as log:
                    if RUNTIME_MODE!="docker-linux":
                        _ensure_qa_venv(current,log)
                    rollback_build=not _qa_runtime_image_exists(current)
                    pid=_start_qa(log,build=rollback_build); _wait_qa(before,pid=pid)
                rolled=before; set_qa_step("rollback","success",f"Rolled back to {before}")
                message += f"; rolled back to {before}"
        except Exception as re: message += f"; rollback failed: {re}"
    def done(st):
        j=st.get("qa_job") or {}; j.update(status=status,finished_at=now_text(),return_code=rc,message=message,rolled_back_to=rolled); st["qa_job"]=j; st.setdefault("qa_history",[]).insert(0,dict(j)); st["qa_history"]=st["qa_history"][:50]
    update_state(done); append_log(f"QA deploy finished status={status}: {message}")
    if _qa_job_lock.locked(): _qa_job_lock.release()

def start_qa_deploy(version):
    if not HOST_OPS_ENABLED and RUNTIME_MODE!="docker-linux":
        raise RuntimeError("QA deployment is disabled in this runtime")
    if not (QA_RELEASES_DIR/version).is_dir(): raise FileNotFoundError("QA release not found")
    if qa_job_running(): raise RuntimeError("QA deployment is already running")
    if not _qa_job_lock.acquire(False): raise RuntimeError("Another QA deployment is running")
    try: threading.Thread(target=run_qa_job,args=(version,),daemon=True).start()
    except Exception:
        _qa_job_lock.release(); raise

def _password_valid(value):
    return len(value or "")>=10
def _set_password(new_password):
    if not _password_valid(new_password):raise ValueError("Mật khẩu mới tối thiểu 10 ký tự")
    c=ensure_config();c["password_hash"]=generate_password_hash(new_password);c["password_changed_at"]=now_text();save_json(CONFIG_FILE,c)
    (CONFIG_DIR/"FIRST_LOGIN.txt").unlink(missing_ok=True)
    append_log("Administrator password reset")
def _is_local_reset_request():
    host=(request.host or "").split(":",1)[0].strip("[]").lower()
    return request.remote_addr in {"127.0.0.1","::1"} and host in {"127.0.0.1","localhost","::1"}
@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        c=ensure_config()
        if hmac.compare_digest(request.form.get("username",""),"admin") and check_password_hash(c["password_hash"],request.form.get("password","")):
            session.clear();session["authenticated"]=True;session["csrf"]=secrets.token_urlsafe(24);return redirect(url_for("index"))
        flash("Sai tài khoản hoặc mật khẩu","error")
    return render_template("login.html")
@app.route("/forgot-password",methods=["GET","POST"])
def forgot_password():
    if request.method=="POST":
        c=ensure_config();code=_normalize_recovery_code(request.form.get("recovery_code",""));new=request.form.get("new_password","");confirm=request.form.get("confirm_password","")
        if not code or not check_password_hash(c.get("recovery_code_hash",""),code):flash("Recovery Code không đúng","error")
        elif new!=confirm:flash("Xác nhận mật khẩu không khớp","error")
        else:
            try:_set_password(new);flash("Đã đặt lại mật khẩu. Hãy đăng nhập lại.","success");return redirect(url_for("login"))
            except ValueError as ex:flash(str(ex),"error")
    return render_template("forgot_password.html",local_reset_url=f"http://localhost:{PORT}{URL_PREFIX}/local-reset")
@app.route("/local-reset",methods=["GET","POST"])
def local_reset():
    if not _is_local_reset_request():abort(403)
    if request.method=="POST":
        new=request.form.get("new_password","");confirm=request.form.get("confirm_password","")
        if new!=confirm:flash("Xác nhận mật khẩu không khớp","error")
        else:
            try:_set_password(new);flash("Đã đặt lại mật khẩu từ máy chủ. Hãy đăng nhập.","success");return redirect(url_for("login"))
            except ValueError as ex:flash(str(ex),"error")
    return render_template("local_reset.html")
@app.post("/logout")
def logout():verify_csrf();session.clear();return redirect(url_for("login"))

def tutorial_job_running():
    return _tutorial_job_lock.locked()

def _tutorial_update(**values):
    def f(st):
        job=dict(st.get("tutorial_job") or {})
        job.update(values)
        st["tutorial_job"]=job
    update_state(f)

def _tutorial_finish(status,message):
    def f(st):
        job=dict(st.get("tutorial_job") or {})
        job.update({"status":status,"message":message,"finished_at":now_text()})
        st["tutorial_job"]=job
        hist=st.setdefault("tutorial_history",[])
        hist.insert(0,dict(job))
        st["tutorial_history"]=hist[:20]
    update_state(f)

def _tutorial_builder_dockerfile():
    return """FROM mcr.microsoft.com/playwright:v1.62.1-noble
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \\
 && apt-get install -y --no-install-recommends ffmpeg python3 python3-venv ca-certificates curl \\
 && rm -rf /var/lib/apt/lists/* \\
 && python3 -m venv /opt/tts \\
 && /opt/tts/bin/pip install --no-cache-dir edge-tts
ENV PATH="/opt/tts/bin:${PATH}"
WORKDIR /opt/mesflow
"""

def _tutorial_login_preflight(username,password,log):
    payload=json.dumps({"username":username or "admin","password":password}).encode("utf-8")
    req=urllib.request.Request(
        "http://mesflow-app:8080/api/auth/login",
        data=payload,
        headers={"Content-Type":"application/json","Accept":"application/json"},
        method="POST",
    )
    last=""
    for attempt in range(1,4):
        try:
            with urllib.request.urlopen(req,timeout=10) as resp:
                body=resp.read(2000).decode("utf-8","replace")
                if 200 <= resp.status < 300:
                    try:
                        data=json.loads(body or "{}")
                    except Exception:
                        data={}
                    if data.get("ok"):
                        log.write("[AUTH] Tài khoản MESFlow hợp lệ.\n"); log.flush()
                        return True
                last=f"HTTP {resp.status}"
        except urllib.error.HTTPError as ex:
            # Never include response bodies here: an auth failure only needs status,
            # and this avoids accidentally persisting sensitive payload details.
            last=f"HTTP {ex.code}"
            if ex.code==429:
                wait=5*attempt
                log.write(f"[AUTH] Đang bị giới hạn đăng nhập; chờ {wait}s rồi thử lại.\n"); log.flush()
                time.sleep(wait)
                continue
            if ex.code in (401,403):
                raise RuntimeError("Sai tài khoản hoặc mật khẩu MESFlow")
            raise RuntimeError(f"Không xác minh được đăng nhập MESFlow: {last}")
        except Exception as ex:
            last=type(ex).__name__
            time.sleep(2)
    raise RuntimeError(f"Không xác minh được đăng nhập MESFlow: {last}")

def _tutorial_seed(log,action):
    cmd=["docker","exec","-e","MESFLOW_TUTORIAL_DATA_ALLOW_PRODUCTION=1",
         "mesflow-app","python","-m","mesflow.tutorial_data",action]
    log.write(f"RUN: docker exec mesflow-app ... tutorial_data {action}\n");log.flush()
    r=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,text=True,timeout=240)
    if r.returncode:
        raise RuntimeError(f"Tutorial data {action} failed with code {r.returncode}")

def _run_tutorial_job(username,password,seed_data=True):
    log_path=LOG_DIR/f"tutorial_{datetime.now():%Y%m%d_%H%M%S}.log"
    _tutorial_update(
        status="running",message="Đang chuẩn bị xuất video hướng dẫn",
        started_at=now_text(),finished_at="",log_file=str(log_path),
        username=username,seed_data=bool(seed_data)
    )
    seeded=False
    final_status="failed"
    final_message="Job xuất video bị dừng"
    try:
        if job_running():
            raise RuntimeError("MESFlow đang deploy; chờ deploy hoàn tất rồi xuất video")
        if qa_job_running():
            raise RuntimeError("QA Center đang deploy; chờ deploy hoàn tất rồi xuất video")
        if not (TARGET_HOME/"scripts/make-user-guide-video.sh").is_file():
            raise RuntimeError("MESFlow hiện tại chưa có scripts/make-user-guide-video.sh")
        if not (TARGET_HOME/"tutorial/narration").is_dir():
            raise RuntimeError("MESFlow hiện tại chưa có bộ lời thuyết minh tutorial")
        if not password:
            raise RuntimeError("Cần nhập mật khẩu tài khoản MESFlow để quay video")

        with log_path.open("w",encoding="utf-8") as log:
            log.write(f"[{now_text()}] Tutorial export requested user={username or 'admin'} seed={bool(seed_data)}\n")
            log.flush()

            _tutorial_update(message="Đang kiểm tra tài khoản MESFlow")
            _tutorial_login_preflight(username,password,log)

            if seed_data:
                _tutorial_update(message="Đang tạo dữ liệu hướng dẫn tạm thời")
                _tutorial_seed(log,"seed")
                seeded=True

            image="mesflow-tutorial-builder:1.62.1-v1"
            if subprocess.run(["docker","image","inspect",image],
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
                _tutorial_update(message="Đang build môi trường quay video lần đầu")
                log.write("BUILD tutorial builder image (first run only)\n");log.flush()
                r=subprocess.run(
                    ["docker","build","-t",image,"-"],
                    input=_tutorial_builder_dockerfile(),
                    text=True,stdout=log,stderr=subprocess.STDOUT,timeout=1800
                )
                if r.returncode:
                    raise RuntimeError(f"Build tutorial builder failed with code {r.returncode}")

            tutorial_runtime=TARGET_HOME/"runtime/tutorial-builder"
            output_dir=tutorial_runtime/"output"
            workspace_dir=tutorial_runtime/"workspace"
            publish_dir=TARGET_HOME/"runtime/tutorials"
            for d in (tutorial_runtime,output_dir,workspace_dir,publish_dir):
                d.mkdir(parents=True,exist_ok=True)

            _tutorial_update(message="Đang quay, lồng tiếng và publish video hướng dẫn")
            cmd=[
                "docker","run","--rm","--name","mesflow-tutorial-builder",
                "--network","mesflow-edge",
                "-e",f"MESFLOW_TUTORIAL_USERNAME={username or 'admin'}",
                "-e","MESFLOW_TUTORIAL_PASSWORD",
                "-e","MESFLOW_TUTORIAL_SEED_DATA=0",
                "-e","MESFLOW_TUTORIAL_DATA_ALLOW_PRODUCTION=0",
                "-e","MESFLOW_TUTORIAL_WITH_VOICE=1",
                "-e","MESFLOW_TUTORIAL_AUTO_PUBLISH=1",
                "-e","MESFLOW_VIDEO_WORKSPACE=/work/workspace",
                "-e","MESFLOW_VIDEO_OUTPUT=/work/output",
                "-v",f"{TARGET_HOME}:/opt/mesflow:ro",
                "-v",f"{tutorial_runtime}:/work",
                "-v",f"{publish_dir}:/opt/mesflow/runtime/tutorials",
                "-w","/opt/mesflow",
                image,
                "bash","scripts/make-user-guide-video.sh","http://mesflow-app:8080"
            ]
            child_env=os.environ.copy()
            child_env["MESFLOW_TUTORIAL_PASSWORD"]=password
            log.write("RUN: docker run ... mesflow-tutorial-builder (MESFlow password hidden)\n");log.flush()
            r=subprocess.run(cmd,env=child_env,stdout=log,stderr=subprocess.STDOUT,text=True,timeout=7200)
            if r.returncode not in (0,3):
                raise RuntimeError(f"Tutorial builder exited with code {r.returncode}")
            if r.returncode==3:
                log.write("[WARN] Một số video thất bại sau retry; các video tạo được vẫn đã publish.\n")

            manifest=publish_dir/"manifest.json"
            if not manifest.is_file():
                raise RuntimeError("Không tìm thấy runtime/tutorials/manifest.json sau khi quay")
            manifest_data=json.loads(manifest.read_text(encoding="utf-8"))
            count=len(manifest_data.get("items") or [])
            if count<1:
                raise RuntimeError("manifest.json chưa có video")

            final_status="success" if r.returncode==0 else "warning"
            final_message=(f"Đã xuất và publish {count} video hướng dẫn"
                           if r.returncode==0 else
                           f"Đã publish {count} video; một số video cần xem lại log")
    except Exception as ex:
        final_status="failed"
        final_message=str(ex)
        append_log(f"Tutorial video export failed: {ex}")
    finally:
        if seeded:
            try:
                _tutorial_update(message="Đang dọn dữ liệu hướng dẫn tạm thời")
                with log_path.open("a",encoding="utf-8") as log:
                    _tutorial_seed(log,"cleanup")
            except Exception as cleanup_ex:
                append_log(f"Tutorial cleanup warning: {cleanup_ex}")
                if final_status=="success":
                    final_status="warning"
                    final_message += f"; cảnh báo cleanup: {cleanup_ex}"
        _tutorial_finish(final_status,final_message)
        append_log(f"Tutorial video export finished: {final_status}: {final_message}")
        _tutorial_job_lock.release()

def start_tutorial_job(username,password,seed_data=True):
    if not _tutorial_job_lock.acquire(False):
        raise RuntimeError("Đang có job xuất video hướng dẫn")
    try:
        threading.Thread(target=_run_tutorial_job,args=(username,password,bool(seed_data)),daemon=True).start()
    except Exception:
        _tutorial_job_lock.release()
        raise

@app.post("/tutorial/check-login")
def tutorial_check_login():
    verify_csrf()
    return jsonify(ok=False,error="SERVER_VIDEO_GENERATION_DISABLED",message="Video được build trên máy local và upload qua Agent"),410
    username=(request.form.get("tutorial_username") or "admin").strip() or "admin"
    password=request.form.get("tutorial_password") or ""
    if not password:
        flash("Hãy nhập mật khẩu MESFlow","error")
        return redirect(url_for("index"))
    try:
        tmp=LOG_DIR/"tutorial_login_check.log"
        with tmp.open("a",encoding="utf-8") as log:
            _tutorial_login_preflight(username,password,log)
        flash("Tài khoản MESFlow hợp lệ. Có thể xuất video.","success")
    except Exception as ex:
        flash(str(ex),"error")
    return redirect(url_for("index"))

@app.post("/tutorial/rebuild")
def tutorial_rebuild():
    verify_csrf()
    return jsonify(ok=False,error="SERVER_VIDEO_GENERATION_DISABLED",message="Video được build trên máy local và upload qua Agent"),410
    try:
        username=(request.form.get("tutorial_username") or "admin").strip() or "admin"
        password=request.form.get("tutorial_password") or ""
        seed_data=request.form.get("tutorial_seed_data")=="1"
        start_tutorial_job(username,password,seed_data)
        flash("Đã bắt đầu xuất lại video hướng dẫn. Job chạy nền và có thể mất khá lâu.","success")
    except Exception as ex:
        flash(f"Không thể xuất video hướng dẫn: {ex}","error")
    return redirect(url_for("index"))

ESP_TUTORIAL_STAGES=("uploading","validating","validating_manifest","staging","backing_up","publishing","verifying","success","failed")

def esp_tutorial_status():
    manifest=load_json(ESP_TUTORIAL_HOME/"manifest.json",{})
    version="Chưa publish"
    try:
        if (ESP_TUTORIAL_HOME/"VERSION.txt").is_file():
            version=(ESP_TUTORIAL_HOME/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
    except OSError: pass
    videos=manifest.get("videos") if isinstance(manifest,dict) else []
    valid=(version!="Chưa publish" and isinstance(manifest,dict) and manifest.get("type")=="esp-kiosk-tutorial" and manifest.get("tutorial_version")==version and isinstance(videos,list) and bool(videos))
    previous=""
    backups=ESP_TUTORIAL_HOME.parent/"backups"
    if backups.is_dir():
        candidates=sorted((p for p in backups.glob("esp-kiosk-*") if p.is_dir()),key=lambda p:p.stat().st_mtime,reverse=True)
        if candidates:
            try: previous=(candidates[0]/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
            except OSError: pass
    publish=load_state().get("esp_tutorial_publish") or {}
    return {"published":version!="Chưa publish","tutorial_version":version,
            "firmware_version":manifest.get("firmware_version","") if isinstance(manifest,dict) else "",
            "video_count":len(videos) if isinstance(videos,list) else 0,
            "generated_at":manifest.get("generated_at","") if isinstance(manifest,dict) else "",
            "published_at":display_datetime((publish.get("published_at","") or manifest.get("generated_at","")) if isinstance(manifest,dict) else ""),
            "audio":manifest.get("audio") if isinstance(manifest,dict) else None,
            "package_status":"ready" if valid else "invalid" if version!="Chưa publish" else "missing",
            "previous_version":previous,
            "path":str(ESP_TUTORIAL_HOME)}

def _esp_tutorial_job(status,message,**extra):
    def mutate(state):
        job=dict(state.get("esp_tutorial_job") or {})
        job.update(status=status,message=message,updated_at=now_text(),**extra)
        transitions=list(job.get("transitions") or [])
        if not transitions or transitions[-1].get("status")!=status:
            transitions.append({"status":status,"at":now_text(),"message":message})
        job["transitions"]=transitions;state["esp_tutorial_job"]=job
    return update_state(mutate).get("esp_tutorial_job")

def validate_esp_tutorial_zip(zip_path,extract_dir):
    zip_path=Path(zip_path);extract_dir=Path(extract_dir)
    if zip_path.stat().st_size>ESP_TUTORIAL_MAX_MB*1024*1024:
        raise ValueError(f"ZIP vượt giới hạn {ESP_TUTORIAL_MAX_MB} MB")
    with zipfile.ZipFile(zip_path) as archive:
        entries=archive.infolist()
        if not entries: raise ValueError("ZIP rỗng")
        total=sum(max(0,item.file_size) for item in entries)
        if total>ESP_TUTORIAL_MAX_MB*1024*1024: raise ValueError("ZIP giải nén vượt giới hạn")
        roots=set()
        for item in entries:
            name=item.filename.replace("\\","/")
            if not name or name.startswith("/") or ".." in Path(name).parts:
                raise ValueError("ZIP chứa đường dẫn không an toàn")
            roots.add(name.split("/",1)[0])
            mode=(item.external_attr>>16)&0o170000
            if not item.is_dir() and mode and mode!=0o100000:
                raise ValueError("ZIP chứa symlink hoặc file đặc biệt")
            target=(extract_dir/name).resolve()
            if extract_dir.resolve() not in target.parents and target!=extract_dir.resolve():
                raise ValueError("ZIP chứa path traversal")
        if roots!={"esp-kiosk-tutorial"}: raise ValueError("ZIP phải có đúng một root esp-kiosk-tutorial")
        archive.extractall(extract_dir)
    root=extract_dir/"esp-kiosk-tutorial"
    version_path=root/"VERSION.txt";manifest_path=root/"manifest.json"
    if not version_path.is_file() or not manifest_path.is_file():
        raise ValueError("Package thiếu VERSION.txt hoặc manifest.json")
    version=norm(version_path.read_text(encoding="utf-8-sig"))
    try: manifest=json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as ex: raise ValueError(f"manifest.json không hợp lệ: {ex.msg}") from ex
    if not isinstance(manifest,dict) or manifest.get("type")!="esp-kiosk-tutorial":
        raise ValueError("manifest.type phải là esp-kiosk-tutorial")
    if str(manifest.get("tutorial_version","")).strip()!=version:
        raise ValueError("tutorial_version không khớp VERSION.txt")
    videos=manifest.get("videos")
    if not isinstance(videos,list) or not videos: raise ValueError("manifest không có video")
    filenames=[]
    for video in videos:
        if not isinstance(video,dict): raise ValueError("Video manifest không hợp lệ")
        required={"id","filename","title","description","order","cases"}
        if not required.issubset(video):
            raise ValueError("Video manifest thiếu id/title/description/order/cases")
        if not str(video.get("id","")).strip() or not str(video.get("title","")).strip():
            raise ValueError("Video manifest có id hoặc title rỗng")
        if not isinstance(video.get("order"),int) or not isinstance(video.get("cases"),list):
            raise ValueError("Video manifest có order hoặc cases không hợp lệ")
        filename=str(video.get("filename","")).strip()
        if not filename or Path(filename).name!=filename or not filename.lower().endswith(".mp4"):
            raise ValueError("Tên video không hợp lệ")
        if filename in filenames: raise ValueError("Tên video bị trùng")
        filenames.append(filename)
        path=root/"videos"/filename
        if not path.is_file() or path.stat().st_size<=0: raise ValueError(f"Video thiếu hoặc rỗng: {filename}")
    root_files={p.name for p in root.iterdir() if p.is_file()}
    video_dir=root/"videos"
    actual={p.name for p in video_dir.iterdir() if p.is_file()} if video_dir.is_dir() else set()
    if root_files!={"VERSION.txt","manifest.json"} or {p.name for p in root.iterdir() if p.is_dir()}!={"videos"} or actual!=set(filenames) or any(p.is_dir() for p in video_dir.iterdir()):
        raise ValueError("Package chứa file hoặc thư mục ngoài contract")
    return root,version,manifest

def publish_esp_tutorial(zip_path,source_name):
    if not _esp_tutorial_job_lock.acquire(False): raise RuntimeError("Đang publish video ESP Kiosk")
    package_hash=sha(Path(zip_path));started=now_text();backup_path=None;publish_temp=None
    try:
        _esp_tutorial_job("uploading","Đã nhận ZIP",source_name=source_name,package_sha256=package_hash,started_at=started)
        with tempfile.TemporaryDirectory(prefix="esp-kiosk-tutorial-") as temp:
            extract=Path(temp)/"extract";extract.mkdir()
            _esp_tutorial_job("validating","Đang kiểm tra package")
            root,version,manifest=validate_esp_tutorial_zip(zip_path,extract)
            _esp_tutorial_job("validating_manifest","Manifest hợp lệ",tutorial_version=version)
            previous=load_state().get("esp_tutorial_publish") or {}
            current_version=(ESP_TUTORIAL_HOME/"VERSION.txt").read_text(encoding="utf-8-sig").strip() if (ESP_TUTORIAL_HOME/"VERSION.txt").is_file() else ""
            if current_version==version:
                same=(ESP_TUTORIAL_HOME/"manifest.json").is_file() and (ESP_TUTORIAL_HOME/"manifest.json").read_bytes()==(root/"manifest.json").read_bytes()
                same=same and all((ESP_TUTORIAL_HOME/"videos"/video["filename"]).is_file() and (ESP_TUTORIAL_HOME/"videos"/video["filename"]).read_bytes()==(root/"videos"/video["filename"]).read_bytes() for video in manifest["videos"])
                if not same: raise ValueError(f"Tutorial {version} đã tồn tại với nội dung khác; hãy tăng version")
                _esp_tutorial_job("success",f"Tutorial {version} đã publish với cùng nội dung")
                return version
            _esp_tutorial_job("staging","Đang chuẩn bị cấu trúc publish",tutorial_version=version)
            parent=ESP_TUTORIAL_HOME.parent;parent.mkdir(parents=True,exist_ok=True)
            publish_temp=parent/f".esp-kiosk-publish-{uuid.uuid4().hex}"
            (publish_temp/"videos").mkdir(parents=True)
            shutil.copy2(root/"VERSION.txt",publish_temp/"VERSION.txt")
            shutil.copy2(root/"manifest.json",publish_temp/"manifest.json")
            for video in manifest["videos"]: shutil.copy2(root/"videos"/video["filename"],publish_temp/"videos"/video["filename"])
            _esp_tutorial_job("backing_up","Đang backup tutorial hiện tại")
            if ESP_TUTORIAL_HOME.exists():
                old_version=(ESP_TUTORIAL_HOME/"VERSION.txt").read_text(encoding="utf-8-sig").strip() if (ESP_TUTORIAL_HOME/"VERSION.txt").is_file() else "unknown"
                backups=parent/"backups";backups.mkdir(exist_ok=True)
                backup_path=backups/f"esp-kiosk-{old_version}"
                if backup_path.exists(): shutil.rmtree(backup_path)
                ESP_TUTORIAL_HOME.replace(backup_path)
            _esp_tutorial_job("publishing","Đang chuyển bộ tutorial mới vào runtime")
            publish_temp.replace(ESP_TUTORIAL_HOME);publish_temp=None
            _esp_tutorial_job("verifying","Đang xác minh manifest và video")
            published=load_json(ESP_TUTORIAL_HOME/"manifest.json",{})
            if published.get("tutorial_version")!=version: raise RuntimeError("Version verify thất bại")
            for video in manifest["videos"]:
                if not (ESP_TUTORIAL_HOME/"videos"/video["filename"]).is_file(): raise RuntimeError("Video verify thất bại")
            backups_dir=parent/"backups"
            if backups_dir.is_dir():
                old=sorted((p for p in backups_dir.glob("esp-kiosk-*") if p.is_dir()),key=lambda p:p.stat().st_mtime,reverse=True)
                for stale in old[1:]: shutil.rmtree(stale)
            def done(state):
                state["esp_tutorial_publish"]={"tutorial_version":version,"package_sha256":package_hash,"published_at":now_text()}
                history=list(state.get("esp_tutorial_history") or []);history.insert(0,{"version":version,"status":"success","published_at":now_text()});state["esp_tutorial_history"]=history[:20]
            update_state(done);_esp_tutorial_job("success",f"Đã publish tutorial {version}")
            append_log(f"ESP tutorial publish success version={version} videos={len(manifest['videos'])}")
            return version
    except Exception as ex:
        if backup_path and backup_path.exists():
            if ESP_TUTORIAL_HOME.exists(): shutil.rmtree(ESP_TUTORIAL_HOME)
            backup_path.replace(ESP_TUTORIAL_HOME)
        if publish_temp and publish_temp.exists(): shutil.rmtree(publish_temp)
        _esp_tutorial_job("failed",f"{type(ex).__name__}: {ex}")
        append_log(f"ESP tutorial publish failed stage={load_state().get('esp_tutorial_job',{}).get('status')} exception={type(ex).__name__} error={ex}")
        raise
    finally:_esp_tutorial_job_lock.release()

@app.post("/esp-kiosk-tutorial/upload")
def esp_tutorial_upload():
    verify_csrf();upload=request.files.get("tutorial_zip")
    if not upload or not upload.filename: flash("Hãy chọn ZIP tutorial ESP Kiosk","error");return redirect(url_for("index"))
    if not upload.filename.lower().endswith(".zip"): flash("Chỉ chấp nhận file ZIP","error");return redirect(url_for("index"))
    saved=ESP_TUTORIAL_UPLOAD_DIR/f"{uuid.uuid4().hex}_{secure_filename(upload.filename)}"
    try:
        upload.save(saved);version=publish_esp_tutorial(saved,upload.filename);flash(f"Đã publish video ESP Kiosk {version}","success")
    except Exception as ex: flash(f"Publish video ESP Kiosk thất bại: {ex}","error")
    finally:
        try:saved.unlink(missing_ok=True)
        except OSError:pass
    return redirect(url_for("index"))

@app.get("/")
def index():
    st=load_state();j=st.get("job")
    if j and j.get("log_file"):j=dict(j);j["log_tail"]=tail(j["log_file"],160)
    qj=st.get("qa_job")
    if qj and qj.get("log_file"): qj=dict(qj); qj["log_tail"]=tail(qj["log_file"],160)
    tj=st.get("tutorial_job")
    if tj and tj.get("log_file"): tj=dict(tj); tj["log_tail"]=tail(tj["log_file"],180)
    host=(request.host or "localhost").split(":",1)[0]
    ptj=st.get("promote_test_job")
    return render_template("index.html",releases=list_releases(),mes=mes_status(),target_home=str(TARGET_HOME),job=j,history=st.get("history",[]),qa=qa_status(),qa_releases=list_qa_releases(),qa_home=str(QA_HOME),qa_job=qj,qa_history=st.get("qa_history",[]),tutorial_job=tj,tutorial_history=tj and st.get("tutorial_history",[]) or [],esp_tutorial=esp_tutorial_status(),esp_tutorial_job=st.get("esp_tutorial_job"),esp_builder_job=st.get("esp_builder_job"),esp_build_enabled=ESP_BUILD_ENABLED,esp_kiosk_source=str(ESP_KIOSK_SOURCE_DIR),release_build_job=st.get("release_build_job"),promote_test_job=ptj,release_summary=_release_summary(),mes_build_enabled=MESFLOW_BUILD_ENABLED,server_role=SERVER_ROLE,release_targets=_release_targets(),esp_tutorial_view_url=(os.environ.get("WORKSHOP_MES_PUBLIC_URL") or "").rstrip("/")+"/app?guide=esp-kiosk" if os.environ.get("WORKSHOP_MES_PUBLIC_URL") else "/app?guide=esp-kiosk",qa_open_url=f"http://{host}:{QA_PORT}/",csrf=session.get("csrf",""),agent_log=tail(AGENT_LOG,80),qa_log=tail(QA_LOG,100),agent_version=AGENT_VERSION)
@app.get("/api/status")
def api_status():return jsonify({"agent_version":AGENT_VERSION,"runtime_mode":RUNTIME_MODE,"server_role":SERVER_ROLE,"build_enabled":MESFLOW_BUILD_ENABLED,"source_dir":str(MESFLOW_SOURCE_DIR),"deploy_enabled":DEPLOY_ENABLED,"host_ops_enabled":HOST_OPS_ENABLED,"mes":mes_status(),"qa":qa_status(),"job":load_state().get("job"),"qa_job":load_state().get("qa_job"),"tutorial_job":load_state().get("tutorial_job"),"esp_tutorial":esp_tutorial_status(),"esp_tutorial_job":load_state().get("esp_tutorial_job"),"release_build":load_state().get("release_build_job"),"promote_test_job":load_state().get("promote_test_job"),"release_summary":_release_summary()})

@app.get("/api/release-manager")
def release_manager():
    if not logged_in(): return jsonify(ok=False,error="AUTH_REQUIRED"),401
    return jsonify(ok=True,enabled=MESFLOW_BUILD_ENABLED,summary=_release_summary(),job=load_state().get("release_build_job"))

@app.post("/api/release-manager/build")
def release_manager_build():
    if not logged_in(): return jsonify(ok=False,error="AUTH_REQUIRED"),401
    try: start_release_build(); return jsonify(ok=True,job=load_state().get("release_build_job")),202
    except RuntimeError as exc: return jsonify(ok=False,error=str(exc)),409

@app.post("/api/release-manager/deploy-local")
def release_manager_deploy_local():
    if not logged_in(): return jsonify(ok=False,error="AUTH_REQUIRED"),401
    job=load_state().get("release_build_job") or {}; artifact=job.get("latest") or {}
    if not artifact.get("version"):
        latest=_release_summary().get("latest") or {}
        if latest.get("version"): artifact=_artifact_for_release(str(latest["version"])); job={"status":"SUCCESS","latest":artifact}
    requested=(request.get_json(silent=True) or {}).get("version") or request.form.get("version")
    if requested:
        try: artifact=_artifact_for_release(norm(str(requested)))
        except Exception: artifact={}
        if artifact.get("package_sha256"):
            job={"status":"SUCCESS","latest":artifact}
    if artifact.get("package") and Path(artifact["package"]).is_file():
        package=Path(artifact["package"]); extract=UPLOAD_DIR/f"local_release_{uuid.uuid4().hex}"; extract.mkdir(parents=True)
        try:
            with zipfile.ZipFile(package) as z: safe_extract_zip(package,extract)
            root=locate_root(extract); version,meta=validate_image_release_contract(root)
            target=RELEASES_DIR/version
            if target.exists(): shutil.rmtree(target)
            shutil.copytree(root,target)
            save_json(target/"agent_manifest.json",{"version":version,"source_name":package.name,"uploaded_at":now_text(),"sha256":sha(package)})
        finally: shutil.rmtree(extract,ignore_errors=True)
    version=str(artifact.get("version") or "")
    if not version or job.get("status")!="SUCCESS": return jsonify(ok=False,error="LOCAL_BUILD_REQUIRED"),409
    contamination=_release_contamination(version)
    if contamination.get("contaminated"):
        return jsonify(ok=False,error=f"RELEASE_CONTAMINATED: {contamination.get('reason')}"),409
    try: start_deploy(version); return jsonify(ok=True,version=version),202
    except Exception as exc: return jsonify(ok=False,error=str(exc)),409

@app.get("/api/release-manager/promote-test")
def release_manager_promote_test_status():
    if not logged_in(): return jsonify(ok=False,error="AUTH_REQUIRED"),401
    return jsonify(ok=True,job=load_state().get("promote_test_job"),summary=_release_summary())

@app.post("/api/release-manager/promote-test")
def release_manager_promote_test():
    """LOCAL_PASS -> select the exact frozen release -> verify ZIP SHA/image
    digest/source commit -> upload the SAME zip to the configured
    PRODUCTION_TEST Agent -> remote verifies SHA -> deploy --no-build ->
    health/version/schema -> TEST_PASS. Never rebuilds anything."""
    if not logged_in(): return jsonify(ok=False,error="AUTH_REQUIRED"),401
    verify_csrf()
    summary=_release_summary()
    latest=summary.get("latest") or {}
    requested=(request.get_json(silent=True) or {}).get("version") or request.form.get("version")
    version=str(requested or latest.get("version") or "")
    if not version: return jsonify(ok=False,error="NO_RELEASE_TO_PROMOTE"),409
    promotion=_load_promotion_state()
    if promotion.get("version")!=version or not promotion.get("local_pass"):
        return jsonify(ok=False,error="LOCAL_PASS_REQUIRED"),409
    contamination=_release_contamination(version)
    if contamination.get("contaminated"):
        return jsonify(ok=False,error=f"RELEASE_CONTAMINATED: {contamination.get('reason')}"),409
    url,user,password=_production_test_target()
    if not url or not password:
        return jsonify(ok=False,error="PRODUCTION_TEST_AGENT_NOT_CONFIGURED"),409
    try: start_promote_test(version); return jsonify(ok=True,version=version),202
    except RuntimeError as exc: return jsonify(ok=False,error=str(exc)),409

@app.post("/api/release-manager/promote-production")
def release_manager_promote_production():
    """Wired but never executes without explicit human approval:
    MESFLOW_PRODUCTION_PROMOTE_ENABLED=1 on this Agent AND a request body of
    {"confirm": true}. Requires LOCAL_PASS + TEST_PASS + matching ZIP SHA +
    matching image digest + schema PASS + not contaminated, verified fresh
    on every call (never trusts a stale flag)."""
    if not logged_in(): return jsonify(ok=False,error="AUTH_REQUIRED"),401
    verify_csrf()
    summary=_release_summary()
    promotion=summary.get("promotion") or {}
    version=str((summary.get("latest") or {}).get("version") or "")
    if not promotion.get("production_gate"):
        return jsonify(ok=False,error="GATE_NOT_PASSED",blocked=summary.get("blocked",{}).get("production")),409
    enabled=os.environ.get("MESFLOW_PRODUCTION_PROMOTE_ENABLED","").strip().lower() in {"1","true","yes","on"}
    confirmed=bool((request.get_json(silent=True) or {}).get("confirm"))
    if not (enabled and confirmed):
        return jsonify(ok=False,error="PRODUCTION_PROMOTE_NOT_APPROVED",
                        detail="Requires MESFLOW_PRODUCTION_PROMOTE_ENABLED=1 on this Agent AND {\"confirm\":true} in the request body. Neither is a default — a human must set both explicitly."),403
    # Deliberately not implemented further in this change: even with the gate
    # passed and explicitly confirmed, this Agent does not perform a
    # production deploy in this codebase revision. See
    # reports/RELEASE_MANAGER_BUILD_ONCE.md — Production deploys remain a
    # separate, explicitly-scoped, human-executed action.
    return jsonify(ok=False,error="PRODUCTION_DEPLOY_NOT_IMPLEMENTED",
                    detail=f"Gate passed for {version} and explicitly approved, but this Agent build does not execute production deploys yet."),501


# ---------------- Minimal Deploy Agent ESP OTA ----------------
def _ota_admin_token_valid():
    auth=str(request.headers.get("Authorization") or "")
    supplied=auth[7:].strip() if auth.lower().startswith("bearer ") else str(request.headers.get("X-MESFlow-Deploy-Agent-Token") or "").strip()
    expected=str(os.environ.get("MESFLOW_DEPLOY_AGENT_TOKEN") or os.environ.get("MESFLOW_OTA_PUBLISH_TOKEN") or "").strip()
    return bool(expected and supplied and hmac.compare_digest(expected,supplied))

def _ota_admin_required():
    if logged_in() or _ota_admin_token_valid(): return None
    return jsonify(ok=False,error="AUTH_REQUIRED"),401

def _ota_device_kiosk_id():
    token=str(request.headers.get("X-Kiosk-Token") or "").strip()
    kiosk_id=str((request.get_json(silent=True) or {}).get("kiosk_id") or request.args.get("kiosk_id") or "").strip()
    if not token or not kiosk_id:return None
    # The Agent never opens MESFlow's database. MESFlow validates this device
    # token as part of the readiness request below.
    return kiosk_id

def _ota_download_url(artifact_id, kiosk_id):
    """Advertise the externally reachable HTTPS origin to ESP devices."""
    configured = str(os.environ.get("WORKSHOP_AGENT_PUBLIC_URL") or "").rstrip("/")
    if configured:
        return f"{configured}/api/esp-ota/file/{artifact_id}?kiosk_id={quote(kiosk_id, safe='')}"
    host = request.headers.get("X-Forwarded-Host") or request.host
    proto = (request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip()
    if not proto:
        proto = "https" if str(host).split(":", 1)[0].endswith("mesflow.net") else request.scheme
    return f"{proto}://{host}{request.script_root}/api/esp-ota/file/{artifact_id}?kiosk_id={quote(kiosk_id, safe='')}"

def _mesflow_readiness(kiosk_id,device_token,include_internal=True):
    path=f"/api/internal/kiosks/{quote(kiosk_id,safe='')}/ota-readiness"
    req=urllib.request.Request(MES_URL+path,headers={"X-Kiosk-Token":device_token,"X-Deploy-Agent":"1","User-Agent":"MESFlow-Deploy-Agent-OTA"})
    internal=str(os.environ.get("MESFLOW_INTERNAL_API_TOKEN") or "") if include_internal else ""
    if internal:req.add_header("X-MESFlow-Internal-Token",internal)
    try:
        with urllib.request.urlopen(req,timeout=5) as response:
            payload=json.loads(response.read().decode("utf-8")); return {"available":True,**payload}
    except urllib.error.HTTPError as exc:
        if exc.code in (401,403):
            return {"available":False,"auth_failed":True,"ota_ready":False,"reason":"AUTH_REQUIRED"}
        return {"available":False,"ota_ready":False,"reason":"WAITING_MESFLOW_STATE","error":str(exc)}
    except Exception as exc:
        return {"available":False,"ota_ready":False,"reason":"WAITING_MESFLOW_STATE","error":str(exc)}

def _mesflow_inventory():
    internal=str(os.environ.get("MESFLOW_INTERNAL_API_TOKEN") or "")
    if not internal: return None
    req=urllib.request.Request(MES_URL+"/api/internal/kiosks",headers={"X-MESFlow-Internal-Token":internal,"X-Deploy-Agent":"1"})
    try:
        with urllib.request.urlopen(req,timeout=5) as response:
            payload=json.loads(response.read().decode("utf-8")); return payload.get("items",payload if isinstance(payload,list) else [])
    except Exception:return None

def _device_token_valid(kiosk_id, token):
    return bool(token and _mesflow_readiness(kiosk_id, token, include_internal=False).get("available"))

@app.get("/ota")
def ota_console():
    gate=_ota_admin_required()
    if gate:return gate
    return render_template("ota.html",agent_version=AGENT_VERSION)

@app.post("/api/esp-builder/start")
def esp_builder_start():
    if not logged_in(): return jsonify(ok=False,error="AUTH_REQUIRED"),401
    try:
        start_esp_builder()
        return jsonify(ok=True,job=load_state().get("esp_builder_job")),202
    except RuntimeError as exc:
        return jsonify(ok=False,error=str(exc)),409

@app.get("/api/esp-builder/status")
def esp_builder_status():
    if not logged_in(): return jsonify(ok=False,error="AUTH_REQUIRED"),401
    return jsonify(ok=True,enabled=ESP_BUILD_ENABLED,source_path=str(ESP_KIOSK_SOURCE_DIR),job=load_state().get("esp_builder_job"))

@app.get("/api/esp-ota/devices")
@app.get("/api/ota/devices")
def ota_devices():
    gate=_ota_admin_required()
    if gate:return gate
    items=_mesflow_inventory()
    if items is None:return jsonify(ok=False,error="MESFLOW_UNAVAILABLE"),503
    return jsonify(ok=True,items=ota.sync_devices(items))

@app.get("/api/esp-ota/status/<job_id>")
@app.get("/api/ota/status/<job_id>")
def ota_job_status(job_id):
    gate=_ota_admin_required()
    if gate:return gate
    item=ota.get_job(job_id)
    return (jsonify(ok=True,job=item),200) if item else (jsonify(ok=False,error="NOT_FOUND"),404)

@app.get("/api/esp-ota/jobs")
def ota_jobs():
    gate=_ota_admin_required()
    if gate:return gate
    return jsonify(ok=True,items=ota.list_jobs())

@app.post("/api/esp-ota/jobs/<job_id>/cancel")
def ota_cancel_job(job_id):
    if not (_ota_admin_token_valid() or logged_in()):return jsonify(ok=False,error="AUTH_REQUIRED"),401
    item=ota.cancel_job(job_id)
    return (jsonify(ok=True,job=item),200) if item else (jsonify(ok=False,error="NOT_FOUND"),404)

@app.post("/api/esp-ota/upload")
def ota_upload_and_flash():
    if not (_ota_admin_token_valid() or logged_in()):return jsonify(ok=False,error="AUTH_REQUIRED"),401
    upload=request.files.get("firmware"); form=request.form
    if not upload:return jsonify(ok=False,error="OTA_REQUIRED_FIELDS"),400
    try:
        if str(upload.filename or "").lower().endswith(".zip"):
            artifact=ota._save_firmware_package(upload.stream,upload.filename)
        else:
            version=str(form.get("version") or "").strip()
            if not version:return jsonify(ok=False,error="VERSION_REQUIRED"),400
            hardware=str(form.get("hardware_model") or "").strip()
            artifact=ota._save_firmware(upload.stream,upload.filename,version,hardware)
        ids=json.loads(form.get("kiosk_ids") or "[]")
        job=ota.create_job(artifact,ids)
        return jsonify(ok=True,firmware=artifact,job=job),201
    except ValueError as exc:
        code=str(exc); status=409 if code in {"VERSION_CONFLICT","OTA_ALREADY_RUNNING"} else 400
        return jsonify(ok=False,error=code),status
    except Exception as exc:return jsonify(ok=False,error="OTA_UPLOAD_FAILED",message=str(exc)),400

@app.get("/api/esp-ota/firmware")
def ota_firmware_list():
    gate=_ota_admin_required()
    if gate:return gate
    return jsonify(ok=True,items=ota.list_firmware())

@app.get("/api/esp-ota/check")
@app.get("/api/ota/check")
def ota_device_check():
    kiosk_id=_ota_device_kiosk_id()
    if not kiosk_id:return jsonify(update_available=False,reason="AUTH_REQUIRED"),401
    token=str(request.headers.get("X-Kiosk-Token") or "")
    # Validate the device token without the Agent's internal bypass first.
    # A temporary MESFlow outage must remain a safe WAITING state, not turn a
    # still-valid pending job into a terminal failure.
    token_check=_mesflow_readiness(kiosk_id, token, include_internal=False)
    if token_check.get("auth_failed"): return jsonify(update_available=False,reason="AUTH_REQUIRED"),401
    if not token_check.get("available"):
        return jsonify(update_available=False,reason="WAITING_MESFLOW_STATE"),200
    readiness=_mesflow_readiness(kiosk_id, token)
    if readiness.get("auth_failed"): return jsonify(update_available=False,reason="AUTH_REQUIRED"),401
    if not readiness.get("available"):
        return jsonify(update_available=False,reason="WAITING_MESFLOW_STATE"),200
    if not ota.readiness_allows_update(readiness):
        # Leave the job device PENDING. The next normal ESP poll will retry and
        # will claim the update as soon as the instantaneous readiness is safe.
        reason=readiness.get("reason") or "WAITING_MESFLOW_STATE"
        return jsonify(update_available=False,reason=reason,readiness={
            "online":readiness.get("online"),
            "ui_state":readiness.get("ui_state"),
            "offline_queue_count":readiness.get("offline_queue_count"),
            "health_state":readiness.get("health_state"),
            "active_session":readiness.get("active_session",False),
            "ota_ready":False,
        })
    result=ota.check_update(kiosk_id,str(request.args.get("current_version") or ""),str(request.args.get("hardware_model") or ""))
    if result.get("update_available"):
        result["url"]=_ota_download_url(result["firmware_id"], kiosk_id)
    return jsonify(result)

@app.get("/api/ota/firmware/<artifact_id>/download")
@app.get("/api/esp-ota/file/<artifact_id>")
def ota_device_download(artifact_id):
    kiosk_id=_ota_device_kiosk_id()
    if not kiosk_id:
        app.logger.warning("OTA_DOWNLOAD auth_missing artifact=%s", artifact_id)
        return jsonify(ok=False,error="AUTH_REQUIRED"),401
    if not _device_token_valid(kiosk_id,str(request.headers.get("X-Kiosk-Token") or "")):
        app.logger.warning("OTA_DOWNLOAD auth_failed kiosk_id=%s artifact=%s", kiosk_id, artifact_id)
        return jsonify(ok=False,error="AUTH_REQUIRED"),401
    row=ota.target_for_download(kiosk_id,artifact_id)
    if not row:
        app.logger.warning("OTA_DOWNLOAD not_assigned kiosk_id=%s artifact=%s", kiosk_id, artifact_id)
        return jsonify(ok=False,error="DOWNLOAD_NOT_ASSIGNED"),403
    path=Path(row["storage_path"])
    expected_size = row["file_size"] if "file_size" in row.keys() else row["size"]
    if not path.is_file() or path.stat().st_size!=expected_size:
        app.logger.error("OTA_DOWNLOAD artifact_invalid kiosk_id=%s artifact=%s path=%s", kiosk_id, artifact_id, path)
        return jsonify(ok=False,error="OTA_ARTIFACT_INVALID"),410
    app.logger.info("OTA_DOWNLOAD serve kiosk_id=%s job_id=%s artifact=%s bytes=%s", kiosk_id, row["job_id"], artifact_id, expected_size)
    return send_file(path,mimetype="application/octet-stream",as_attachment=True,download_name=row["filename"],conditional=True,etag=row["sha256"])

@app.post("/api/esp-ota/event")
@app.post("/api/ota/event")
def ota_device_event():
    kiosk_id=_ota_device_kiosk_id()
    if not kiosk_id:return jsonify(ok=False,error="AUTH_REQUIRED"),401
    if not _device_token_valid(kiosk_id,str(request.headers.get("X-Kiosk-Token") or "")): return jsonify(ok=False,error="AUTH_REQUIRED"),401
    body=request.get_json(silent=True) or {}; body["kiosk_id"]=kiosk_id
    return jsonify(ok=True,**ota.record_event(body)),201


# ---------------- Server Operations Console ----------------
def _run_capture(cmd, timeout=12, shell=False):
    try:
        r=subprocess.run(cmd, shell=shell, capture_output=True, text=True, errors="replace", timeout=timeout,
                         executable="/bin/bash" if shell and os.name!="nt" else None)
        return {"ok":r.returncode==0,"exit_code":r.returncode,"stdout":r.stdout[-200000:],"stderr":r.stderr[-50000:]}
    except subprocess.TimeoutExpired as ex:
        return {"ok":False,"exit_code":124,"stdout":str(ex.stdout or "")[-200000:],"stderr":"Command timed out"}
    except Exception as ex:
        return {"ok":False,"exit_code":-1,"stdout":"","stderr":str(ex)}

def _bytes(n):
    n=float(n or 0)
    for unit in ("B","KB","MB","GB","TB"):
        if n < 1024 or unit=="TB": return f"{n:.1f} {unit}"
        n/=1024

def ops_summary():
    vm=psutil.virtual_memory(); sw=psutil.swap_memory(); root=psutil.disk_usage('/') if os.name!='nt' else psutil.disk_usage(Path.cwd().anchor)
    loads=os.getloadavg() if hasattr(os,'getloadavg') else (0,0,0)
    boot=datetime.fromtimestamp(psutil.boot_time())
    failed=[]
    if os.name!='nt':
        x=_run_capture(["systemctl","--failed","--no-legend","--plain"],8)
        failed=[ln for ln in x.get("stdout","").splitlines() if ln.strip()]
    docker=_run_capture(["docker","ps","-q"],8) if shutil.which("docker") else {"ok":False,"stdout":""}
    unhealthy=_run_capture(["docker","ps","--filter","health=unhealthy","-q"],8) if shutil.which("docker") else {"ok":False,"stdout":""}
    score=100; alerts=[]
    cpu=psutil.cpu_percent(interval=0.15)
    if cpu>=90: score-=20; alerts.append(f"CPU cao {cpu:.0f}%")
    elif cpu>=75: score-=8; alerts.append(f"CPU {cpu:.0f}%")
    if vm.percent>=90: score-=20; alerts.append(f"RAM cao {vm.percent:.0f}%")
    elif vm.percent>=80: score-=8; alerts.append(f"RAM {vm.percent:.0f}%")
    if root.percent>=95: score-=30; alerts.append(f"Disk gần đầy {root.percent:.0f}%")
    elif root.percent>=85: score-=12; alerts.append(f"Disk {root.percent:.0f}%")
    if failed: score-=min(25,5*len(failed)); alerts.append(f"{len(failed)} systemd service failed")
    uh=len([x for x in unhealthy.get("stdout","").splitlines() if x.strip()])
    if uh: score-=min(25,8*uh); alerts.append(f"{uh} Docker container unhealthy")
    mes=mes_status(); qa=qa_status()
    if not mes.get("online"): score-=20; alerts.append("MESFlow offline")
    return {"score":max(0,score),"alerts":alerts,"hostname":socket.gethostname(),"os":platform.platform(),
            "uptime_seconds":int(time.time()-psutil.boot_time()),"boot_time":boot.strftime('%Y-%m-%d %H:%M:%S'),
            "cpu_percent":cpu,"cpu_count":psutil.cpu_count(),"load":[round(x,2) for x in loads],
            "ram":{"percent":vm.percent,"used":_bytes(vm.used),"total":_bytes(vm.total)},
            "swap":{"percent":sw.percent,"used":_bytes(sw.used),"total":_bytes(sw.total)},
            "disk":{"percent":root.percent,"used":_bytes(root.used),"total":_bytes(root.total),"free":_bytes(root.free)},
            "failed_services":len(failed),"docker_running":len([x for x in docker.get("stdout","").splitlines() if x.strip()]),
            "docker_unhealthy":uh,"mes":mes,"qa":qa}

def ops_services():
    if os.name=='nt': return []
    cmd=["systemctl","list-units","--type=service","--all","--no-legend","--plain","--no-pager"]
    r=_run_capture(cmd,10); out=[]
    for ln in r.get("stdout","").splitlines():
        parts=ln.split(None,4)
        if len(parts)>=4:
            unit,load,active,sub=parts[:4]; desc=parts[4] if len(parts)>4 else ''
            out.append({"unit":unit,"load":load,"active":active,"sub":sub,"description":desc})
    return out

def ops_docker():
    if not shutil.which('docker'): return []
    fmt='{{json .}}'
    r=_run_capture(["docker","ps","-a","--no-trunc","--format",fmt],12); out=[]
    for ln in r.get('stdout','').splitlines():
        try: out.append(json.loads(ln))
        except json.JSONDecodeError: pass
    return out

def ops_ports():
    rows=[]
    try:
        for c in psutil.net_connections(kind='inet'):
            if c.status != psutil.CONN_LISTEN or not c.laddr: continue
            pid=c.pid or 0; name=''
            if pid:
                try:name=psutil.Process(pid).name()
                except Exception: pass
            rows.append({"ip":c.laddr.ip,"port":c.laddr.port,"pid":pid,"process":name})
    except (psutil.AccessDenied,PermissionError): pass
    return sorted(rows,key=lambda x:(x['port'],x['ip']))

def _dangerous_command(cmd):
    c=' '.join((cmd or '').strip().lower().split())
    patterns=[r'(^|[;&|])\s*rm\s+-[^\n]*r[^\n]*\s+/(\s|$)',r'\bmkfs\b',r'\bdd\s+if=',r'\b(shutdown|reboot|poweroff|halt)\b',
              r'docker\s+system\s+prune',r'docker\s+volume\s+(rm|prune)',r'\b(drop\s+database|drop\s+table|truncate\s+table)\b']
    return any(re.search(p,c,re.I) for p in patterns)

def _ssh_exec(host,port,user,command,password=None,key_path=None,timeout=12):
    client=paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs={"hostname":host,"port":int(port or 22),"username":user,"timeout":timeout,"banner_timeout":timeout,"auth_timeout":timeout,"look_for_keys":True,"allow_agent":True}
    if password: kwargs['password']=password
    if key_path: kwargs['key_filename']=os.path.expanduser(key_path)
    started=time.time()
    try:
        client.connect(**kwargs)
        stdin,stdout,stderr=client.exec_command(command,timeout=max(timeout,30),get_pty=False)
        out=stdout.read().decode('utf-8',errors='replace'); err=stderr.read().decode('utf-8',errors='replace'); code=stdout.channel.recv_exit_status()
        return {"ok":code==0,"exit_code":code,"stdout":out[-200000:],"stderr":err[-50000:],"duration_ms":int((time.time()-started)*1000)}
    except Exception as ex:
        return {"ok":False,"exit_code":-1,"stdout":"","stderr":str(ex),"duration_ms":int((time.time()-started)*1000)}
    finally:
        client.close()

@app.get('/ops')
def ops_page():
    return render_template('ops.html',csrf=session.get('csrf',''),agent_version=AGENT_VERSION)

@app.get('/api/ops/summary')
def api_ops_summary(): return jsonify(ops_summary())

@app.get('/api/ops/services')
def api_ops_services(): return jsonify({"items":ops_services()})

@app.get('/api/ops/docker')
def api_ops_docker(): return jsonify({"items":ops_docker()})

@app.get('/api/ops/ports')
def api_ops_ports(): return jsonify({"items":ops_ports()})

@app.get('/api/ops/logs')
def api_ops_logs():
    source=request.args.get('source','agent'); lines=max(20,min(1000,int(request.args.get('lines','200'))))
    allowed={
        'agent': ['journalctl','-u','mesflow-deploy-agent.service','-n',str(lines),'--no-pager'],
        'mesflow': ['docker','logs','--tail',str(lines),'mesflow-app'],
        'postgres': ['docker','logs','--tail',str(lines),'mesflow-postgres'],
        'nginx': ['journalctl','-u','nginx.service','-n',str(lines),'--no-pager'],
        'qa': ['journalctl','-u','mesflow-qa-center.service','-n',str(lines),'--no-pager'],
        'kernel': ['journalctl','-k','-p','warning','-n',str(lines),'--no-pager'],
        'errors': ['journalctl','-p','err','-n',str(lines),'--no-pager'],
    }
    cmd=allowed.get(source)
    if not cmd: return jsonify({"ok":False,"error":"Unknown log source"}),400
    r=_run_capture(cmd,15); return jsonify(r)

@app.post('/api/ops/local-command')
def api_ops_local_command():
    if not HOST_OPS_ENABLED: return jsonify({"ok":False,"error":"Host-local command execution is disabled in this runtime"}),403
    verify_csrf(); d=request.get_json(silent=True) or {}; cmd=str(d.get('command','')).strip()
    if not cmd: return jsonify({"ok":False,"error":"Command is empty"}),400
    if len(cmd)>4000: return jsonify({"ok":False,"error":"Command too long"}),400
    if _dangerous_command(cmd) and not d.get('confirm_dangerous'):
        return jsonify({"ok":False,"needs_confirmation":True,"error":"Lệnh có khả năng phá dữ liệu hoặc reboot. Xác nhận để chạy."}),409
    append_log(f"OPS local command: {cmd[:500]}")
    return jsonify(_run_capture(cmd,timeout=max(5,min(120,int(d.get('timeout',30)))),shell=True))

@app.post('/api/ops/ssh-command')
def api_ops_ssh_command():
    verify_csrf(); d=request.get_json(silent=True) or {}
    host=str(d.get('host','')).strip(); user=str(d.get('user','')).strip(); command=str(d.get('command','')).strip()
    if not host or not user or not command: return jsonify({"ok":False,"error":"Host, user và command là bắt buộc"}),400
    if len(command)>4000: return jsonify({"ok":False,"error":"Command too long"}),400
    if _dangerous_command(command) and not d.get('confirm_dangerous'):
        return jsonify({"ok":False,"needs_confirmation":True,"error":"Lệnh SSH có khả năng phá dữ liệu hoặc reboot. Xác nhận để chạy."}),409
    append_log(f"OPS SSH {user}@{host}:{d.get('port',22)} command: {command[:500]}")
    result=_ssh_exec(host,d.get('port',22),user,command,password=d.get('password') or None,key_path=d.get('key_path') or None,timeout=max(5,min(60,int(d.get('timeout',15)))))
    return jsonify(result)

@app.post('/api/ops/service-action')
def api_ops_service_action():
    if not HOST_OPS_ENABLED: return jsonify({"ok":False,"error":"systemd service actions are disabled in this runtime"}),403
    verify_csrf(); d=request.get_json(silent=True) or {}; unit=str(d.get('unit','')); action=str(d.get('action',''))
    if not re.fullmatch(r'[A-Za-z0-9_.@:-]+\.service',unit) or action not in {'start','stop','restart'}: return jsonify({"ok":False,"error":"Invalid service/action"}),400
    append_log(f"OPS systemctl {action} {unit}")
    return jsonify(_run_capture(['systemctl',action,unit],30))

@app.post('/api/ops/docker-action')
def api_ops_docker_action():
    verify_csrf(); d=request.get_json(silent=True) or {}; name=str(d.get('name','')); action=str(d.get('action',''))
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',name) or action not in {'start','stop','restart'}: return jsonify({"ok":False,"error":"Invalid container/action"}),400
    append_log(f"OPS docker {action} {name}")
    return jsonify(_run_capture(['docker',action,name],60))
@app.post("/upload")
def upload_release():
    verify_csrf();u=request.files.get("release_zip")
    append_log(f"MES upload request received: filename={getattr(u,'filename','')!r}")
    if job_running():
        append_log("MES upload rejected: deployment already running")
        flash("Đang deploy","error");return redirect(url_for("index"))
    if not u or not u.filename or not u.filename.lower().endswith('.zip'):
        append_log("MES upload rejected: missing/invalid ZIP")
        flash("Chọn file ZIP","error");return redirect(url_for("index"))
    z=UPLOAD_DIR/f"{int(time.time())}_{secure_filename(u.filename)}";e=UPLOAD_DIR/f"extract_{secrets.token_hex(8)}"
    response={"ok":False,"stage":"uploading"}
    try:
        append_log(f"MES upload saving: {u.filename}")
        u.save(z)
        append_log(f"MES upload saved: bytes={z.stat().st_size}")
        if not zipfile.is_zipfile(z): raise ValueError("Uploaded file is not a valid ZIP")
        append_log("MES upload extracting")
        e.mkdir();safe_extract_zip(z,e);r=locate_root(e)
        append_log(f"MES upload root detected: {r.name}")
        v,meta=validate_image_release_contract(r) if (r/"release.json").is_file() and load_json(r/"release.json",{}).get("type")==IMAGE_RELEASE_TYPE else validate_mes_release_contract(r)
        append_log(f"MES upload version detected: {v}")
        append_log(f"MES upload validated: version={v}, root={r.name}, protocol={meta.get('deployment_protocol',1)}")
        t=RELEASES_DIR/v
        replacement=RELEASES_DIR/f".{v}.replacement-{secrets.token_hex(6)}"
        shutil.copytree(r,replacement)
        save_json(replacement/"agent_manifest.json",{"version":v,"uploaded_at":now_text(),"source_name":u.filename,"sha256":sha(z)})
        if t.exists():
            append_log(f"Version {v} already exists; replacing old release staging for retry")
            flash(f"Version {v} đã tồn tại; đang thay thế release staging cũ", "warning")
            old=RELEASES_DIR/f".{v}.previous-{secrets.token_hex(6)}"
            t.replace(old)
            try: replacement.replace(t)
            except Exception:
                old.replace(t)
                raise
            shutil.rmtree(old,ignore_errors=True)
        else:
            replacement.replace(t)
        append_log(f"MES release stored: {v}")
        append_log(f"MES deploy job started: {v}")
        start_deploy(v)
        flash(f"Đã upload và bắt đầu deploy MESFlow {v}","success")
        response={"ok":True,"stage":"queued","version":v,"message":f"Đã nhận ZIP {v}; đang xếp hàng deploy.","redirect_url":url_for("index")}
    except Exception as ex:
        append_log(f"MES upload/deploy failed before redirect: stage=upload_or_validation exception={type(ex).__name__} error={ex}")
        flash(f"Upload / deploy MESFlow thất bại: {ex}","error")
        response={"ok":False,"stage":"upload_or_validation","error_class":type(ex).__name__,"error":str(ex)}
    finally:
        z.unlink(missing_ok=True);shutil.rmtree(e,ignore_errors=True)
    if request.headers.get("X-Requested-With")=="XMLHttpRequest":
        return jsonify(response), (200 if response["ok"] else 400)
    return redirect(url_for("index"))
@app.post("/deploy/<version>")
def deploy(version):
    verify_csrf()
    try:
        version=norm(version);start_deploy(version);flash(f"Đã bắt đầu deploy {version}","success")
    except Exception as ex:
        flash(str(ex),"error")
    return redirect(url_for("index"))
@app.post("/dry-run/<version>")
def dry_run_deploy(version):
    verify_csrf()
    try:
        version=norm(version);start_deploy(version,dry_run=True);flash(f"Đã bắt đầu dry-run {version}; production không bị dừng","success")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("index"))
@app.post("/delete/<version>")
def delete(version):
    verify_csrf()
    try:v=norm(version)
    except ValueError as ex:
        flash(str(ex),"error");return redirect(url_for("index"))
    if job_running():
        flash("Không thể xóa release khi deployment đang chạy","error")
        return redirect(url_for("index"))
    target=RELEASES_DIR/v
    if not target.is_dir():
        flash(f"Không tìm thấy release {v}","error")
        return redirect(url_for("index"))
    active=current_file_version().lstrip("Vv")
    if active==v.lstrip("Vv"):
        flash(f"Không thể xóa version đang chạy {v}","error")
        return redirect(url_for("index"))
    try:
        shutil.rmtree(target)
        append_log(f"Deleted release {v}")
        flash(f"Đã xóa {v}","success")
    except OSError as ex:
        append_log(f"Delete release {v} failed: {ex}")
        flash(f"Xóa {v} thất bại: {ex}","error")
    return redirect(url_for("index"))
@app.post("/qa/upload")
def qa_upload_release():
    verify_csrf(); u=request.files.get("qa_release_zip")
    if qa_job_running(): flash("QA Center đang deploy","error"); return redirect(url_for("index"))
    if not u or not u.filename or not u.filename.lower().endswith('.zip'): flash("Chọn file QA ZIP","error"); return redirect(url_for("index"))
    z=QA_UPLOAD_DIR/f"{int(time.time())}_{secure_filename(u.filename)}"; e=QA_UPLOAD_DIR/f"extract_{secrets.token_hex(8)}"
    try:
        u.save(z); e.mkdir(); safe_extract_zip(z,e); r=locate_qa_root(e); v=norm((r/"VERSION").read_text(encoding="utf-8-sig")); t=QA_RELEASES_DIR/v
        if t.exists(): shutil.rmtree(t)
        shutil.copytree(r,t); save_json(t/"agent_manifest.json",{"version":v,"uploaded_at":now_text(),"source_name":u.filename,"sha256":sha(z),"package_type":"mesflow_qa_center"})
        start_qa_deploy(v)
        flash(f"Đã upload và bắt đầu deploy QA Center {v}","success")
    except Exception as ex: flash(f"Upload / deploy QA thất bại: {ex}","error")
    finally: z.unlink(missing_ok=True); shutil.rmtree(e,ignore_errors=True)
    return redirect(url_for("index"))

@app.post("/qa/deploy/<version>")
def qa_deploy(version):
    verify_csrf()
    try: version=norm(version); start_qa_deploy(version); flash(f"Đã bắt đầu deploy QA {version}","success")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("index"))

@app.post("/qa/restart")
def qa_restart():
    if not HOST_OPS_ENABLED: flash("QA host restart is disabled in this container runtime","error"); return redirect(url_for("index"))
    verify_csrf()
    try:
        if qa_job_running(): raise RuntimeError("QA Center đang deploy")
        if qa_current_version()=="Chưa cài": raise RuntimeError("QA Center chưa được cài")
        _stop_qa()
        pid=_start_qa(); _wait_qa(qa_current_version(),pid=pid); flash("Đã restart QA Center","success")
    except Exception as ex: flash(f"Restart QA thất bại: {ex}","error")
    return redirect(url_for("index"))

@app.post("/qa/stop")
def qa_stop():
    if not HOST_OPS_ENABLED: flash("QA host stop is disabled in this container runtime","error"); return redirect(url_for("index"))
    verify_csrf()
    try:
        _stop_qa()
        flash("Đã dừng QA Center","success")
    except Exception as ex: flash(f"Dừng QA thất bại: {ex}","error")
    return redirect(url_for("index"))

@app.post("/qa/delete/<version>")
def qa_delete(version):
    verify_csrf()
    try:
        v=norm(version)
        if qa_job_running(): raise RuntimeError("QA Center đang deploy")
        if qa_current_version().lstrip("Vv")==v.lstrip("Vv"): raise RuntimeError("Không thể xóa QA version đang chạy")
        t=QA_RELEASES_DIR/v
        if not t.is_dir(): raise RuntimeError("Không tìm thấy QA release")
        shutil.rmtree(t); flash(f"Đã xóa QA release {v}","success")
    except Exception as ex: flash(str(ex),"error")
    return redirect(url_for("index"))

@app.post("/change-password")
def change_password():
    verify_csrf();c=ensure_config();old=request.form.get("old_password","");new=request.form.get("new_password","")
    if not check_password_hash(c["password_hash"],old):flash("Mật khẩu hiện tại không đúng","error")
    elif len(new)<10:flash("Mật khẩu mới tối thiểu 10 ký tự","error")
    else:c["password_hash"]=generate_password_hash(new);save_json(CONFIG_FILE,c);(CONFIG_DIR/"FIRST_LOGIN.txt").unlink(missing_ok=True);flash("Đã đổi mật khẩu","success")
    return redirect(url_for("index"))
@app.get("/live")
def live():return {"ok":True,"agent_version":AGENT_VERSION,"runtime_mode":RUNTIME_MODE}
@app.get("/health")
def health():return {"ok":True,"agent_version":AGENT_VERSION,"runtime_mode":RUNTIME_MODE,"server_role":SERVER_ROLE,"build_enabled":MESFLOW_BUILD_ENABLED,"source_dir":str(MESFLOW_SOURCE_DIR),"deploy_enabled":DEPLOY_ENABLED,"host_ops_enabled":HOST_OPS_ENABLED,"mes":mes_status(),"qa":qa_status(),"job_running":job_running(),"qa_job_running":qa_job_running(),"tutorial_job_running":tutorial_job_running(),"esp_tutorial_job_running":_esp_tutorial_job_lock.locked(),"esp_tutorial":esp_tutorial_status()}
def main():
    recover_interrupted_deploy(); job_running()
    append_log(f"Agent {AGENT_VERSION} start {HOST}:{PORT}, MES={MES_URL}, QA={QA_URL}")
    serve(app,host=HOST,port=PORT,threads=8)
if __name__=="__main__":main()
