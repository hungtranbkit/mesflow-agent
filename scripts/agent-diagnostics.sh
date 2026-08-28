#!/usr/bin/env bash
set -u

SCOPE="${1:-all}"
AGENT_CONTAINER="${DEPLOY_AGENT_CONTAINER:-mesflow-deploy-agent}"
AGENT_HOME_CONTAINER="${WORKSHOP_AGENT_HOME:-/data}"
MESFLOW_HOST="${MESFLOW_HOST_DIR:-/opt/mesflow}"
QA_HOST="${MESFLOW_QA_HOST_DIR:-/opt/mesflow-qa-center}"
WORKSPACE_HOST="${MESFLOW_WORKSPACE_HOST_DIR:-/home/dell/workspace/mesflow}"
NETWORK="${MESFLOW_DOCKER_NETWORK:-mesflow-edge}"

section(){ printf '\n===== %s =====\n' "$1"; }
run(){ printf '\n$ %s\n' "$*"; "$@" 2>&1 || true; }
shrun(){ printf '\n$ %s\n' "$1"; sh -lc "$1" 2>&1 || true; }

echo "MESFLOW DEPLOY AGENT DIAGNOSTICS"
echo "scope=$SCOPE"
echo "generated=$(date '+%Y-%m-%d %H:%M:%S %z')"
echo "read_only=true"

section "HOST"
run hostname
run id
run uname -a
run uptime

section "DOCKER"
run docker version
run docker info --format 'Server={{.ServerVersion}} Driver={{.Driver}} Root={{.DockerRootDir}}'
run docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

section "DEPLOY AGENT CONTAINER"
run docker inspect "$AGENT_CONTAINER" --format 'Name={{.Name}} Image={{.Config.Image}} Status={{.State.Status}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}'
run docker inspect "$AGENT_CONTAINER" --format '{{range .Mounts}}{{println .Type .Source "->" .Destination "RW=" .RW}}{{end}}'
run docker inspect "$AGENT_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' | \
  sed -E 's/(PASSWORD|PASS|TOKEN|SECRET|KEY)=.*/\1=***MASKED***/I' || true

section "NETWORK"
if docker network inspect "$NETWORK" >/dev/null 2>&1; then
  echo "$NETWORK: OK"
  run docker network inspect "$NETWORK" --format '{{range $id,$c := .Containers}}{{println $c.Name $c.IPv4Address}}{{end}}'
else
  echo "$NETWORK: MISSING"
fi

section "DISK"
run df -h /
run df -h "$MESFLOW_HOST"
run df -h "$QA_HOST"
run df -h "$WORKSPACE_HOST"

section "PATH ACCESS"
for p in "$MESFLOW_HOST" "$MESFLOW_HOST/runtime" "$MESFLOW_HOST/runtime/tutorials" "$QA_HOST" "$WORKSPACE_HOST"; do
  echo "-- $p"
  if [ -e "$p" ]; then
    ls -ld "$p" 2>&1 || true
    [ -r "$p" ] && echo "readable=yes" || echo "readable=no"
    [ -w "$p" ] && echo "writable=yes" || echo "writable=no"
  else
    echo "exists=no"
  fi
done

section "VERSIONS"
for f in \
  "$WORKSPACE_HOST/deploy-agent/VERSION.txt" \
  "$WORKSPACE_HOST/qa-center/VERSION.txt" \
  "$WORKSPACE_HOST/mesflow/VERSION.txt"
do
  if [ -f "$f" ]; then echo "$f: $(tr -d '[:space:]' < "$f")"; fi
done
run docker exec "$AGENT_CONTAINER" sh -lc 'python3 - <<'"'"'PY'"'"'
try:
 import agent
 print("runtime_agent_version="+str(getattr(agent,"AGENT_VERSION","unknown")))
except Exception as e:
 print("runtime_agent_version_error="+repr(e))
PY'

section "AGENT STATE / RECENT ERRORS"
run docker exec "$AGENT_CONTAINER" sh -lc '
for f in /data/state.json /data/data/state.json /opt/mesflow-agent/data/state.json; do
  if [ -f "$f" ]; then
    echo "STATE=$f"
    python3 - "$f" <<'"'"'PY'"'"'
import json,sys,re
p=sys.argv[1]
try:
 d=json.load(open(p))
except Exception as e:
 print("state_read_error",repr(e)); raise SystemExit
keys=["job","qa_local_deploy_job","qa_promote_test_job","promote_test_job",
      "qa_release_build_job","agent_package_build_job","tutorial_job"]
for k in keys:
 v=d.get(k)
 if not v: continue
 print("\\n["+k+"]")
 for f in ("status","stage","message","error_code","error","updated_at","finished_at","log_path"):
  if f in v and v[f] not in (None,""):
   s=str(v[f])
   s=re.sub(r"(?i)(password|token|secret|key)=\\S+",r"\\1=***MASKED***",s)
   print(f"{f}={s[:4000]}")
PY
    break
  fi
done
'

if [ "$SCOPE" = "tutorial" ] || [ "$SCOPE" = "all" ]; then
  section "TUTORIAL LATEST LOG"
  run docker exec "$AGENT_CONTAINER" sh -lc '
D=""
for x in /data/data/logs /data/logs /opt/mesflow-agent/data/logs; do
  [ -d "$x" ] && D="$x" && break
done
echo "LOG_DIR=$D"
F="$(ls -t "$D"/tutorial_*.log 2>/dev/null | head -1)"
echo "LOG=$F"
[ -n "$F" ] && tail -n 220 "$F"
'

  section "TUTORIAL BUILDER / HELPER"
  run docker ps -a --filter name=mesflow-tutorial-builder \
    --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
  shrun 'docker ps -a --filter name=mesflow-tutorial-builder -q | head -1 | xargs -r docker inspect --format "{{json .HostConfig.Binds}}"'
fi

if [ "$SCOPE" = "agent-package" ] || [ "$SCOPE" = "all" ]; then
  section "AGENT PACKAGE / TEST ENVIRONMENT"
  run docker exec "$AGENT_CONTAINER" sh -lc '
cd /workspace/mesflow/deploy-agent 2>/dev/null || exit 0
echo "PWD=$PWD"
[ -f VERSION.txt ] && { printf "VERSION="; cat VERSION.txt; echo; }
[ -x scripts/version-doctor.sh ] && scripts/version-doctor.sh --check || true
python3 - <<'"'"'PY'"'"'
mods=["flask","paramiko","pytest"]
for m in mods:
 try:
  x=__import__(m); print(m+"=OK "+str(getattr(x,"__version__","")))
 except Exception as e: print(m+"=MISSING "+repr(e))
PY
'
fi

if [ "$SCOPE" = "qa" ] || [ "$SCOPE" = "all" ]; then
  section "QA CENTER"
  run docker ps -a --filter name=mesflow-qa-center \
    --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
  run docker logs --tail 120 mesflow-qa-center
fi

if [ "$SCOPE" = "mesflow" ] || [ "$SCOPE" = "all" ]; then
  section "MESFLOW"
  run docker ps -a --filter name=mesflow-app \
    --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
  run docker logs --tail 120 mesflow-app
fi

section "FAST ROOT-CAUSE HINTS"

# Exact tutorial nested read-only mount signature.
if docker exec mesflow-deploy-agent sh -lc '
D=""; for x in /data/data/logs /data/logs /opt/mesflow-agent/data/logs; do [ -d "$x" ] && D="$x" && break; done
F="$(ls -t "$D"/tutorial_*.log 2>/dev/null | head -1)"
[ -n "$F" ] && grep -q "error mounting.*runtime/tutorials.*read-only file system" "$F"
' 2>/dev/null; then
  echo "ROOT_CAUSE: TUTORIAL_NESTED_READONLY_MOUNT"
  echo "FIX: source mount must be /src/mesflow:ro; publish stays /opt/mesflow/runtime/tutorials"
fi

# Tutorial nested bind under read-only parent.
if docker inspect "$AGENT_CONTAINER" --format '{{range .Mounts}}{{println .Destination .RW}}{{end}}' 2>/dev/null | \
   grep -Eq '^/opt/mesflow false$'; then
  echo "HINT: /opt/mesflow is mounted read-only inside Deploy Agent."
  echo "If a nested docker run also mounts /opt/mesflow/runtime/... as writable,"
  echo "Docker may fail with OCI/runc read-only mountpoint errors."
fi

if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  echo "HINT: required Docker network '$NETWORK' is missing."
fi

if ! docker inspect "$AGENT_CONTAINER" >/dev/null 2>&1; then
  echo "HINT: Deploy Agent container '$AGENT_CONTAINER' is missing."
fi

echo
echo "DIAGNOSTICS COMPLETE"
