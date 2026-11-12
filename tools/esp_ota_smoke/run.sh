#!/usr/bin/env bash
# Local process smoke (NOT staging, NOT hardware): real agent.py (branch worktree) + fake MESFlow. Secrets are random per run, never printed.
set -u
S=$(cd "$(dirname "$0")" && pwd); A=$(cd "$S/../.." && pwd)
PKG=${1:?usage: run.sh <release.ota.zip from mesflow-esp32-kiosk scripts/build-ota-release.sh>}
PY=${PYTHON:-$A/.venv/bin/python}
L=$(mktemp -d); H="$L/home"
rnd(){ python3 -c 'import secrets;print(secrets.token_urlsafe(24))'; }
export SMOKE_INTERNAL=$(rnd) SMOKE_ADMIN_TOKEN=$(rnd) SMOKE_TOK_K1=$(rnd) SMOKE_TOK_K2=$(rnd) SMOKE_TOK_K3=$(rnd) SMOKE_TOK_DUP=$(rnd) SMOKE_TOK_DUPB=$(rnd)
export SMOKE_RELEASE_VERSION=$(python3 -c "import json,sys,zipfile;print(json.loads(zipfile.ZipFile(sys.argv[1]).read('release.json'))['version'])" "$PKG")
python3 "$S/fake_mesflow.py" 18181 > "$L/fake.log" 2>&1 & FP=$!
( cd "$A" && ulimit -Sn 1024 && WORKSHOP_AGENT_HOME="$H" WORKSHOP_AGENT_HOST=127.0.0.1 WORKSHOP_AGENT_PORT=18191 WORKSHOP_MES_URL=http://127.0.0.1:18181 \
  MESFLOW_INTERNAL_API_TOKEN="$SMOKE_INTERNAL" MESFLOW_DEPLOY_AGENT_TOKEN="$SMOKE_ADMIN_TOKEN" MESFLOW_AGENT_ADMIN_PASSWORD="$(rnd)" \
  WORKSHOP_AGENT_PUBLIC_URL=https://ota.smoke.invalid/agent exec "$PY" agent.py ) > "$L/agent.log" 2>&1 & AP=$!
for i in $(seq 60); do curl -fs http://127.0.0.1:18191/agent/live >/dev/null 2>&1 && break; sleep 0.5; done
"$PY" "$S/drive.py" http://127.0.0.1:18191/agent http://127.0.0.1:18181 "$PKG"; rc=$?
kill $AP $FP 2>/dev/null; wait 2>/dev/null
echo "nofile soft used: 1024; agent commit $(git -C "$A" rev-parse --short HEAD)"
if grep -F -e "$SMOKE_INTERNAL" -e "$SMOKE_ADMIN_TOKEN" -e "$SMOKE_TOK_K1" -e "$SMOKE_TOK_DUP" -e "$SMOKE_TOK_DUPB" "$L/agent.log" "$L/fake.log" >/dev/null; then echo "FAIL secret value found in logs"; rc=1; else echo "PASS no token/secret values in agent/fake logs"; fi
echo "logs: $L"
exit $rc
