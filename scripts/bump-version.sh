#!/usr/bin/env bash
# Transactional Deploy Agent version bump. VERSION.txt is the source of truth.
# Existing declarations may be stale; they are normalized by pattern, not by
# exact old-version string. Critical files are rolled back on ANY failure.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
die(){ echo "ERROR: $*" >&2; exit 1; }
SUFFIX="-docker-runtime"
[[ -f VERSION.txt ]] || die "VERSION.txt not found"
current="$(tr -d '[:space:]' < VERSION.txt)"
[[ "$current" =~ ^[0-9]+\.[0-9]+\.[0-9]+${SUFFIX}$ ]] || die "VERSION_INVALID: $current"
current_core="${current%${SUFFIX}}"
if [[ "${1:-}" == "--to" ]]; then target="${2:-}"; else target="${1:-}"; fi
if [[ -z "$target" ]]; then
  target_core="$(python3 - "$current_core" <<'PY'
import sys
p=[int(x) for x in sys.argv[1].split('.')]
p[-1]+=1
print('.'.join(map(str,p)))
PY
)"
  target="${target_core}${SUFFIX}"
else
  [[ "$target" == *"$SUFFIX" ]] || target="${target}${SUFFIX}"
fi
[[ "$target" =~ ^[0-9]+\.[0-9]+\.[0-9]+${SUFFIX}$ ]] || die "VERSION_INVALID target: $target"
[[ "$target" != "$current" ]] || die "VERSION_UNCHANGED: $target"
target_core="${target%${SUFFIX}}"
python3 - "$current_core" "$target_core" <<'PY' || die "VERSION_NOT_NEWER: target must be greater than current"
import sys
sys.exit(0 if tuple(map(int,sys.argv[2].split('.'))) > tuple(map(int,sys.argv[1].split('.'))) else 1)
PY
frozen="$ROOT/../artifacts/deploy-agent/releases/$target/agent-release.json"
[[ ! -f "$frozen" ]] || die "VERSION_ALREADY_RELEASED: $target"
# Preflight every declaration this transaction promises to keep synchronized.
# This must happen before version-doctor --repair or any other write so a
# malformed/incomplete release tree fails atomically without partial edits.
critical=(VERSION.txt agent.py docker/Dockerfile docker/compose.linux.yml docker/compose.windows.yml README.md docs/DEPLOY_DOCKER.md)
for f in "${critical[@]}"; do [[ -f "$f" ]] || die "Expected file missing: $f"; done

[[ -x "$ROOT/scripts/version-doctor.sh" ]] || die "VERSION_DOCTOR_MISSING: scripts/version-doctor.sh must exist and be executable"

backup="$(mktemp -d)"
files=(VERSION.txt agent.py docker/Dockerfile docker/compose.linux.yml docker/compose.windows.yml README.md docs/DEPLOY_DOCKER.md)
restore(){
  for f in "${files[@]}"; do
    [[ -f "$backup/$f" ]] || continue
    mkdir -p "$(dirname "$f")"; cp -a "$backup/$f" "$f"
  done
}
cleanup(){ rm -rf "$backup"; }
trap 'rc=$?; if [[ $rc -ne 0 ]]; then echo "VERSION BUMP FAILED — rolling back every changed declaration" >&2; restore; fi; cleanup; exit $rc' EXIT
for f in "${files[@]}"; do
  [[ -f "$f" ]] || continue
  mkdir -p "$backup/$(dirname "$f")"; cp -a "$f" "$backup/$f"
done

# Normalize stale declarations first. This repairs old compose/image tags safely.
printf '%s\n' "$current" > VERSION.txt
"$ROOT/scripts/version-doctor.sh" --repair >/dev/null

python3 - "$target" <<'PY'
from pathlib import Path
import re, sys
full=sys.argv[1]; core=full.removesuffix('-docker-runtime')
Path('VERSION.txt').write_text(full+'\n',encoding='utf-8')

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
# Documentation is informative only. Never fail a release because wording changed.
opt('README.md',r'Current version: \*\*[^*]+\*\*',f'Current version: **{full}**')
opt('docs/DEPLOY_DOCKER.md',r'Version: \*\*[^*]+\*\*',f'Version: **{full}**')
PY

"$ROOT/scripts/version-doctor.sh" --check >/dev/null || die "POST_BUMP_VERSION_CHECK_FAILED"
echo "VERSION BUMP PASS"
echo "Old version: $current"
echo "New version: $target"
echo "Critical declarations updated transactionally. README/docs are best-effort and never block the bump."
