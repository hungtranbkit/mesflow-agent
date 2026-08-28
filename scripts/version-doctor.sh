#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
die(){ echo "ERROR: $*" >&2; exit 1; }
SUFFIX="-docker-runtime"
mode="${1:---check}"
[[ "$mode" == "--check" || "$mode" == "--repair" ]] || die "Usage: scripts/version-doctor.sh [--check|--repair]"
[[ -f VERSION.txt ]] || die "VERSION.txt not found"
canonical="$(tr -d '[:space:]' < VERSION.txt)"
[[ "$canonical" =~ ^[0-9]+\.[0-9]+\.[0-9]+${SUFFIX}$ ]] || die "VERSION_INVALID: $canonical"
core="${canonical%${SUFFIX}}"
critical=(agent.py docker/Dockerfile docker/compose.linux.yml docker/compose.windows.yml)
for f in "${critical[@]}"; do [[ -f "$f" ]] || die "Expected file missing: $f"; done

read_decl(){
  case "$1" in
    agent.py) grep -Eo '^AGENT_VERSION="[0-9]+\.[0-9]+\.[0-9]+-docker-runtime"' "$1" | head -1 | sed -E 's/^AGENT_VERSION="(.*)"$/\1/' ;;
    docker/Dockerfile) grep -Eo '^ARG AGENT_VERSION=[0-9]+\.[0-9]+\.[0-9]+-docker-runtime' "$1" | head -1 | cut -d= -f2 ;;
    docker/compose.linux.yml|docker/compose.windows.yml) grep -Eo 'mesflow-deploy-agent:[0-9]+\.[0-9]+\.[0-9]+' "$1" | head -1 | cut -d: -f2 ;;
  esac
}

show(){
  echo "VERSION SOURCE OF TRUTH: $canonical"
  printf '%-30s %s\n' 'VERSION.txt' "$canonical"
  for f in "${critical[@]}"; do
    v="$(read_decl "$f" || true)"
    expect="$canonical"; [[ "$f" == docker/compose.* ]] && expect="$core"
    if [[ "$v" == "$expect" ]]; then mark='OK'; else mark='MISMATCH'; fi
    printf '%-30s %-24s %s\n' "$f" "${v:-MISSING}" "$mark"
  done
}

is_consistent(){
  [[ "$(read_decl agent.py || true)" == "$canonical" ]] &&
  [[ "$(read_decl docker/Dockerfile || true)" == "$canonical" ]] &&
  [[ "$(read_decl docker/compose.linux.yml || true)" == "$core" ]] &&
  [[ "$(read_decl docker/compose.windows.yml || true)" == "$core" ]]
}

if [[ "$mode" == "--check" ]]; then
  show
  is_consistent || die "VERSION_INCONSISTENT: run scripts/version-doctor.sh --repair"
  echo "VERSION CONSISTENCY PASS"
  exit 0
fi

backup="$(mktemp -d)"
files=(VERSION.txt agent.py docker/Dockerfile docker/compose.linux.yml docker/compose.windows.yml README.md docs/DEPLOY_DOCKER.md)
restore(){
  for f in "${files[@]}"; do
    [[ -f "$backup/$f" ]] || continue
    mkdir -p "$(dirname "$f")"
    cp -a "$backup/$f" "$f"
  done
}
cleanup(){ rm -rf "$backup"; }
trap 'rc=$?; if [[ $rc -ne 0 ]]; then echo "REPAIR FAILED — restoring original files" >&2; restore; fi; cleanup; exit $rc' EXIT
for f in "${files[@]}"; do
  [[ -f "$f" ]] || continue
  mkdir -p "$backup/$(dirname "$f")"
  cp -a "$f" "$backup/$f"
done

python3 - "$canonical" <<'PY'
from pathlib import Path
import re, sys
full=sys.argv[1]; core=full.removesuffix('-docker-runtime')

def req(path, pattern, repl, count=1):
    p=Path(path); s=p.read_text(encoding='utf-8')
    ns,n=re.subn(pattern,repl,s,count=count,flags=re.M)
    if n < 1: raise SystemExit(f'PATTERN_NOT_FOUND: {path}')
    p.write_text(ns,encoding='utf-8')

def opt(path, pattern, repl):
    p=Path(path)
    if not p.is_file(): return
    s=p.read_text(encoding='utf-8')
    ns,n=re.subn(pattern,repl,s,count=1,flags=re.M)
    if n: p.write_text(ns,encoding='utf-8')

req('agent.py',r'^AGENT_VERSION="[0-9]+\.[0-9]+\.[0-9]+-docker-runtime"$',f'AGENT_VERSION="{full}"')
req('docker/Dockerfile',r'^ARG AGENT_VERSION=[0-9]+\.[0-9]+\.[0-9]+-docker-runtime$',f'ARG AGENT_VERSION={full}')
for f in ('docker/compose.linux.yml','docker/compose.windows.yml'):
    req(f,r'mesflow-deploy-agent:[0-9]+\.[0-9]+\.[0-9]+',f'mesflow-deploy-agent:{core}')
opt('README.md',r'Current version: \*\*[^*]+\*\*',f'Current version: **{full}**')
opt('docs/DEPLOY_DOCKER.md',r'Version: \*\*[^*]+\*\*',f'Version: **{full}**')
PY

show
is_consistent || die "REPAIR_VERIFY_FAILED"
echo "VERSION REPAIR PASS"
