#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(tr -d '\r\n' < "$ROOT/VERSION.txt")"
NAME="MESFlow_Server_Bootstrap_Agent_First_v${VERSION}"
OUT="${1:-$ROOT/../artifacts/deploy-agent/${NAME}.zip}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/$NAME/payload"
cp "$ROOT/installer/install.sh" "$ROOT/installer/update-gateway.sh" "$ROOT/installer/diagnose-gateway.sh" "$ROOT/installer/README.md" "$WORK/$NAME/"
cp -a "$ROOT" "$WORK/$NAME/payload/mesflow-deploy-agent"
rm -rf "$WORK/$NAME/payload/mesflow-deploy-agent/installer" "$WORK/$NAME/payload/mesflow-deploy-agent/__pycache__"
rm -rf "$WORK/$NAME/payload/mesflow-deploy-agent/docker/runtime" "$WORK/$NAME/payload/mesflow-deploy-agent/.pytest_cache"
find "$WORK/$NAME" -type d \( -name '__pycache__' -o -name '.pytest_cache' \) -prune -exec rm -rf {} +
find "$WORK/$NAME" -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '.DS_Store' -o -name '.env' -o -name '.env.*' \) -delete
chmod +x "$WORK/$NAME/install.sh" "$WORK/$NAME/update-gateway.sh" "$WORK/$NAME/diagnose-gateway.sh"
mkdir -p "$(dirname "$OUT")"
(cd "$WORK" && zip -qr "$OUT" "$NAME")
echo "$OUT"
