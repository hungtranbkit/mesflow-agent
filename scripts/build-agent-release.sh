#!/usr/bin/env bash
# Build the Deploy Agent itself once on DEV, producing an immutable Agent
# update artifact: image tag/digest/id, checksum, manifest. This is what
# scripts/update-target-agent.sh (or the Release Manager's "Update Agent"
# button) sends to a target's host-side updater -- never the source tree.
#
# Pipeline: source preflight -> version validation -> tests -> package
# build -> SHA256 -> manifest -> immutable artifact.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
die(){ echo "ERROR: $*" >&2; exit 1; }

# --- 1. source preflight ---
command -v docker >/dev/null || die "DOCKER_NOT_FOUND"
for f in agent.py VERSION.txt docker/Dockerfile; do
  [[ -f "$ROOT/$f" ]] || die "SOURCE_PREFLIGHT_FAILED: missing $f"
done

# One source of truth: refuse to build an inconsistent source tree. The
# doctor is read-only in --check mode; it never mutates source during build.
if [[ -x "$ROOT/scripts/version-doctor.sh" ]]; then
  "$ROOT/scripts/version-doctor.sh" --check || die "VERSION_INCONSISTENT: run scripts/version-doctor.sh --repair before building"
fi

# --- 2. version validation ---
agent_version="$(tr -d '[:space:]' < VERSION.txt)"
[[ "$agent_version" == *-docker-runtime ]] || die "VERSION_INVALID: expected a *-docker-runtime version, got $agent_version"
image_tag="${agent_version%-docker-runtime}"
image="mesflow-deploy-agent:${image_tag}"
dist="$ROOT/../artifacts/deploy-agent/releases/${agent_version}"

# Immutable release guard, same policy as mesflow/scripts/build-release.sh --
# checked before running the (slower) test suite so a re-run of an
# already-frozen version fails fast.
if [[ -f "$dist/agent-release.json" ]]; then
  die "VERSION_ALREADY_RELEASED: artifacts/deploy-agent/releases/${agent_version}/agent-release.json already exists (frozen). A version may be built only once. Bump VERSION.txt to a new version and rebuild."
fi

# --- 3. tests ---
# Isolated WORKSHOP_AGENT_HOME (never the real workspace state) -- same
# convention every test file in this project already uses. Prefer the
# project's own .venv (has playwright etc.) when it's actually usable from
# here; fall back to a bare python3 -- real bug found+fixed live: this
# script also runs *inside* the Deploy Agent's own minimal Alpine/musl
# runtime container (that's how the "Build Agent Package" UI button
# invokes it), where a host-built .venv is visible via the bind mount but
# not executable (glibc binaries, wrong libc) -- `[[ -x ... ]]` on it
# already correctly falls through to plain python3 in that case. pytest is
# intentionally NOT in requirements.txt (it would bloat every deployed
# Agent, including TEST/PRODUCTION roles that never build), so install it
# on demand, into whichever interpreter this run picked -- this build
# script already assumes docker/git/bash are available for DEV-only
# workflows exactly like this one.
PYBIN="python3"
[[ -x "$ROOT/.venv/bin/python" ]] && "$ROOT/.venv/bin/python" -c "import sys" >/dev/null 2>&1 && PYBIN="$ROOT/.venv/bin/python"
"$PYBIN" -c "import pytest" >/dev/null 2>&1 || "$PYBIN" -m pip install --quiet pytest || die "PYTEST_INSTALL_FAILED: could not install pytest into $PYBIN"
test_home="$(mktemp -d)"
cleanup_test_home(){ rm -rf "$test_home"; }
trap cleanup_test_home EXIT
mkdir -p "$ROOT/../artifacts"
if ! WORKSHOP_AGENT_HOME="$test_home" timeout 480 "$PYBIN" -m pytest "$ROOT/tests" -q 2>&1 | tee "$ROOT/../artifacts/.agent-package-build-tests.log"; then
  die "TESTS_FAILED: deploy-agent test suite did not pass (or timed out after 480s) -- see the log above. No image was built."
fi
rm -f "$ROOT/../artifacts/.agent-package-build-tests.log"
trap - EXIT
cleanup_test_home

# --- 4. package build ---
# $dist is NOT created yet -- only once the artifact actually exists (right
# before writing agent-release.json below). A failed/interrupted build
# (IMAGE_TAG_CONTAMINATED, killed mid-build, etc.) must not leave behind an
# empty releases/<version>/ directory that reads as "already attempted"
# without being either a real artifact or a clean absence.
build_tag="${image}__building_$$"
cleanup_build_tag(){ docker rmi "$build_tag" >/dev/null 2>&1 || true; }
trap cleanup_build_tag EXIT
# --provenance=false --sbom=false: modern BuildKit embeds a build
# attestation (timestamps, build info) in the image manifest by default,
# which makes the final image id differ between separate `docker build`
# invocations even when every layer is a 100% cache hit -- discovered live
# when this guard correctly refused a rebuild it should have recognized as
# idempotent. Disabling attestations restores byte-identical image ids for
# byte-identical source, which the immutable-once guard depends on.
#
# `docker buildx build ... --load` (not plain `docker build`): those two
# flags are buildx-only. The host's docker CLI has BuildKit integrated
# into plain `docker build` directly, but this script also runs *inside*
# the Deploy Agent's own container (the "Build Agent Package" UI button),
# whose leaner docker-cli package needs the explicit buildx subcommand --
# real bug found+fixed live: `docker build --provenance=false` there failed
# with "unknown flag: --provenance" on the legacy (non-BuildKit) builder.
# `docker buildx build` works identically in both places.
docker buildx build --provenance=false --sbom=false --load -f docker/Dockerfile -t "$build_tag" .
new_id="$(docker image inspect "$build_tag" --format '{{.Id}}')"
[[ "$new_id" == sha256:* ]] || die "IMAGE_ID_UNAVAILABLE"
if docker image inspect "$image" >/dev/null 2>&1; then
  existing_id="$(docker image inspect "$image" --format '{{.Id}}')"
  if [[ "$existing_id" != "$new_id" ]]; then
    die "IMAGE_TAG_CONTAMINATED: $image already exists (id=$existing_id) and differs from the image just built (id=$new_id). Bump VERSION.txt and rebuild -- never retag over an existing Agent version."
  fi
  echo "NOTE: $image already exists and matches the freshly built image exactly (idempotent rebuild) — proceeding."
fi
docker tag "$build_tag" "$image"
image_id="$new_id"
digest="$image_id"
source_commit="$(git -c safe.directory='*' rev-parse HEAD 2>/dev/null || echo unknown)"

# Pull API_PROTOCOL_VERSION / AGENT_CAPABILITIES straight out of the built
# image's own agent.py, not the workspace copy -- what's declared here must
# be exactly what the shipped image will report at runtime.
protocol="$(docker run --rm --entrypoint python "$image" -c "import agent; print(agent.API_PROTOCOL_VERSION)")"
capabilities_json="$(docker run --rm --entrypoint python "$image" -c "import agent, json; print(json.dumps(agent.AGENT_CAPABILITIES))")"

tmp="$(mktemp -d)"
cleanup_all(){ cleanup_build_tag; rm -rf "$tmp"; }
trap cleanup_all EXIT
root="$tmp/mesflow-deploy-agent-release"; mkdir -p "$root"
docker save "$image" -o "$root/mesflow-deploy-agent_${image_tag}.tar"
built_at="$(date -Is)"
cat > "$root/agent-release.json" <<JSON
{"type":"mesflow-agent-release","version":"$agent_version","image":"$image","image_digest":"$digest","image_id":"$image_id","source_commit":"$source_commit","built_at":"$built_at","api_protocol_version":$protocol,"capabilities":$capabilities_json,"bundle":"mesflow-deploy-agent_${image_tag}.tar"}
JSON

# --- 5. SHA256 ---
(cd "$root" && sha256sum "mesflow-deploy-agent_${image_tag}.tar" agent-release.json > checksums.txt)
mkdir -p "$dist"
cp "$root/agent-release.json" "$dist/agent-release.json"
cp "$root/checksums.txt" "$dist/checksums.txt"
package="$dist/AGENT_UPDATE_${agent_version}.zip"
if command -v zip >/dev/null 2>&1; then
  (cd "$tmp" && zip -qr "$package" mesflow-deploy-agent-release)
else
  python3 - "$tmp" "$package" <<'PY'
import pathlib, sys, zipfile
root = pathlib.Path(sys.argv[1]); out = pathlib.Path(sys.argv[2])
with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
    for path in (root / "mesflow-deploy-agent-release").rglob("*"):
        if path.is_file():
            z.write(path, path.relative_to(root).as_posix())
PY
fi
sha256sum "$package" > "$package.sha256"
zip_sha256="$(cut -d' ' -f1 < "$package.sha256")"
zip_size="$(stat -c%s "$package" 2>/dev/null || stat -f%z "$package")"

# --- 6. manifest (canonical directory-level copy; the wire-contract
# filename Bootstrap's updater.py extracts FROM the ZIP stays exactly
# agent-release.json -- never renamed) ---
cp "$dist/agent-release.json" "$dist/manifest.json"

# --- 7. BUILD_REPORT.md ---
cat > "$dist/BUILD_REPORT.md" <<MD
# Deploy Agent Package Build Report

- **Version:** $agent_version
- **Built at:** $built_at
- **Source commit:** $source_commit
- **Image:** $image
- **Image digest (id):** $digest
- **API protocol version:** $protocol
- **Package:** $(basename "$package")
- **Package size:** ${zip_size} bytes
- **Package SHA256:** $zip_sha256

## Pipeline

1. Source preflight: PASS
2. Version validation: PASS ($agent_version)
3. Tests: PASS (deploy-agent test suite, isolated WORKSHOP_AGENT_HOME)
4. Package build: PASS (Docker image, \`--provenance=false --sbom=false\`)
5. SHA256: PASS
6. Manifest: PASS
7. Immutable artifact: $dist/

This artifact uses the canonical AGENT_UPDATE_<version>.zip format
Bootstrap's /updater/update already verifies and installs
(deploy-agent/updater/updater.py, unmodified). It is never overwritten:
rerunning this script against the same VERSION.txt fails with
VERSION_ALREADY_RELEASED.
MD

# --- 8. immutable artifact ---
echo "AGENT RELEASE PASS"; echo "Version: $agent_version"; echo "Image: $image"; echo "Digest: $digest"
echo "Protocol: $protocol"; echo "Capabilities: $capabilities_json"; echo "Package: $package"
