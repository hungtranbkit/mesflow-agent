#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
test_root="$(mktemp -d)"
[[ -n "$test_root" && "$test_root" == /tmp/tmp.* ]] || { echo "Invalid test temp directory" >&2; exit 97; }
cleanup(){ [[ -n "$test_root" && "$test_root" == /tmp/tmp.* ]] && rm -rf -- "$test_root"; }
trap cleanup EXIT
export WORKSHOP_AGENT_HOME="$test_root/agent"
export WORKSHOP_TARGET_HOME="$test_root/target"
export MESFLOW_QA_HOME="$test_root/qa"
export PYTHONPATH="$ROOT"
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  [[ -x "$ROOT/.venv/bin/python" ]] && PYTHON="$ROOT/.venv/bin/python" || PYTHON=python3
fi
"$PYTHON" -m py_compile agent.py ota_control.py predictive.py release_gate.py agent_backend/*.py
"$PYTHON" -m pytest -q
"$PYTHON" tools/build_source_package.py
