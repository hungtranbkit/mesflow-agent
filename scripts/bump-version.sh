#!/usr/bin/env bash
# Bump the Deploy Agent's OWN version consistently across every declared
# location (see reports/DEPLOY_AGENT_REFRESH_PERFORMANCE_FIX.md and
# tests/test_p0_hygiene.py::test_authoritative_versions_are_synchronized):
#   - VERSION.txt
#   - agent.py (AGENT_VERSION="...")
#   - docker/Dockerfile (ARG AGENT_VERSION=...)
#   - docker/compose.linux.yml (mesflow-deploy-agent:<image-tag>)
#   - docker/compose.windows.yml (mesflow-deploy-agent:<image-tag>)
#   - README.md (Current version: **...**)
#   - docs/DEPLOY_DOCKER.md (Version: **...**)
#
# This ONLY edits source text. It never builds, tags, or pushes a Docker
# image, and never touches package_installer.sh's output -- run
# scripts/build-agent-release.sh / package_installer.sh separately, when
# ready, to build/package the new version.
#
# Usage:
#   scripts/bump-version.sh                        # increment the last numeric segment (X.Y.Z -> X.Y.(Z+1))
#   scripts/bump-version.sh 2.24.0                  # bump to an explicit version (suffix optional)
#   scripts/bump-version.sh --to 2.24.0-docker-runtime  # same, explicit flag form
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
die(){ echo "ERROR: $*" >&2; exit 1; }
SUFFIX="-docker-runtime"

[[ -f VERSION.txt ]] || die "VERSION.txt not found in $ROOT"
current="$(tr -d '[:space:]' < VERSION.txt)"
[[ "$current" =~ ^[0-9]+\.[0-9]+\.[0-9]+${SUFFIX}$ ]] || die "VERSION_INVALID: current VERSION.txt is not X.Y.Z${SUFFIX}: '$current'"
current_core="${current%${SUFFIX}}"

if [[ "${1:-}" == "--to" ]]; then
  target="${2:-}"
else
  target="${1:-}"
fi
if [[ -z "$target" ]]; then
  target_core="$(python3 -c "print('.'.join('$current_core'.split('.')[:2] + [str(int('$current_core'.split('.')[2]) + 1)]))")"
  target="${target_core}${SUFFIX}"
else
  # Accept the target with or without the -docker-runtime suffix.
  [[ "$target" == *"$SUFFIX" ]] || target="${target}${SUFFIX}"
fi
[[ "$target" =~ ^[0-9]+\.[0-9]+\.[0-9]+${SUFFIX}$ ]] || die "VERSION_INVALID: target version is not X.Y.Z${SUFFIX}: '$target'"
[[ "$target" != "$current" ]] || die "VERSION_UNCHANGED: target equals current ($current)"
target_core="${target%${SUFFIX}}"

# Never move backward -- compare the 3 numeric segments, numerically.
python3 -c "
import sys
cur=[int(x) for x in '$current_core'.split('.')]
tgt=[int(x) for x in '$target_core'.split('.')]
sys.exit(0 if tgt>cur else 1)
" || die "VERSION_NOT_NEWER: target $target is not greater than current $current"

# Refuse to bump onto a version number that's already frozen -- a version
# number is never reused, matching scripts/build-agent-release.sh's own
# immutable-once guard (artifacts/deploy-agent/releases/<version>/agent-release.json).
frozen="$ROOT/../artifacts/deploy-agent/releases/$target/agent-release.json"
[[ ! -f "$frozen" ]] || die "VERSION_ALREADY_RELEASED: artifacts/deploy-agent/releases/$target/agent-release.json already exists (frozen). Choose a different target version."

for f in VERSION.txt agent.py docker/Dockerfile docker/compose.linux.yml docker/compose.windows.yml README.md docs/DEPLOY_DOCKER.md; do
  [[ -f "$f" ]] || die "Expected file missing: $f"
done

printf '%s' "$target" > VERSION.txt
sed -i "s/AGENT_VERSION=\"${current}\"/AGENT_VERSION=\"${target}\"/" agent.py
sed -i "s/ARG AGENT_VERSION=${current}/ARG AGENT_VERSION=${target}/" docker/Dockerfile
sed -i "s/mesflow-deploy-agent:${current_core}/mesflow-deploy-agent:${target_core}/" docker/compose.linux.yml
sed -i "s/mesflow-deploy-agent:${current_core}/mesflow-deploy-agent:${target_core}/" docker/compose.windows.yml
sed -i "s/Current version: \*\*${current}\*\*/Current version: **${target}**/" README.md
sed -i "s/Version: \*\*${current}\*\*/Version: **${target}**/" docs/DEPLOY_DOCKER.md

# Verify every declaration actually landed on the new version -- fail loud
# rather than leaving a partially-bumped, inconsistent source tree.
fail=0
[[ "$(tr -d '[:space:]' < VERSION.txt)" == "$target" ]] || { echo "VERIFY_FAILED: VERSION.txt" >&2; fail=1; }
grep -qF "AGENT_VERSION=\"${target}\"" agent.py || { echo "VERIFY_FAILED: agent.py" >&2; fail=1; }
grep -qF "ARG AGENT_VERSION=${target}" docker/Dockerfile || { echo "VERIFY_FAILED: docker/Dockerfile" >&2; fail=1; }
grep -qF "mesflow-deploy-agent:${target_core}" docker/compose.linux.yml || { echo "VERIFY_FAILED: docker/compose.linux.yml" >&2; fail=1; }
grep -qF "mesflow-deploy-agent:${target_core}" docker/compose.windows.yml || { echo "VERIFY_FAILED: docker/compose.windows.yml" >&2; fail=1; }
grep -qF "Current version: **${target}**" README.md || { echo "VERIFY_FAILED: README.md" >&2; fail=1; }
grep -qF "Version: **${target}**" docs/DEPLOY_DOCKER.md || { echo "VERIFY_FAILED: docs/DEPLOY_DOCKER.md" >&2; fail=1; }
[[ "$fail" -eq 0 ]] || die "One or more version declarations failed to update; inspect working tree before proceeding."

echo "VERSION BUMP PASS"
echo "Old version: $current"
echo "New version: $target"
echo "Files updated: VERSION.txt, agent.py, docker/Dockerfile, docker/compose.linux.yml, docker/compose.windows.yml, README.md, docs/DEPLOY_DOCKER.md"
echo "NOTE: no build was run. Package with package_installer.sh / build with scripts/build-agent-release.sh when ready."
