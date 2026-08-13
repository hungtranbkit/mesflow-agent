#!/usr/bin/env bash
# Build the Deploy Agent itself once on DEV, producing an immutable Agent
# update artifact: image tag/digest/id, checksum, manifest. This is what
# scripts/update-target-agent.sh (or the Release Manager's "Update Agent"
# button) sends to a target's host-side updater -- never the source tree.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
die(){ echo "ERROR: $*" >&2; exit 1; }
command -v docker >/dev/null || die "DOCKER_NOT_FOUND"

agent_version="$(tr -d '[:space:]' < VERSION.txt)"
[[ "$agent_version" == *-docker-runtime ]] || die "VERSION_INVALID: expected a *-docker-runtime version, got $agent_version"
image_tag="${agent_version%-docker-runtime}"
image="mesflow-deploy-agent:${image_tag}"
dist="$ROOT/../artifacts/deploy-agent/releases/${agent_version}"

# --- immutable release guard, same policy as mesflow/scripts/build-release.sh ---
if [[ -f "$dist/agent-release.json" ]]; then
  die "VERSION_ALREADY_RELEASED: artifacts/deploy-agent/releases/${agent_version}/agent-release.json already exists (frozen). A version may be built only once. Bump VERSION.txt to a new version and rebuild."
fi
mkdir -p "$dist"

build_tag="${image}__building_$$"
cleanup_build_tag(){ docker rmi "$build_tag" >/dev/null 2>&1 || true; }
trap cleanup_build_tag EXIT
docker build -f docker/Dockerfile -t "$build_tag" .
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
cat > "$root/agent-release.json" <<JSON
{"type":"mesflow-agent-release","version":"$agent_version","image":"$image","image_digest":"$digest","image_id":"$image_id","source_commit":"$source_commit","built_at":"$(date -Is)","api_protocol_version":$protocol,"capabilities":$capabilities_json,"bundle":"mesflow-deploy-agent_${image_tag}.tar"}
JSON
(cd "$root" && sha256sum "mesflow-deploy-agent_${image_tag}.tar" agent-release.json > checksums.txt)
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
echo "AGENT RELEASE PASS"; echo "Version: $agent_version"; echo "Image: $image"; echo "Digest: $digest"
echo "Protocol: $protocol"; echo "Capabilities: $capabilities_json"; echo "Package: $package"
