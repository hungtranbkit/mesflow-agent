#!/usr/bin/env bash
# Build the bootstrap installer package (MESFlow_Server_Bootstrap_Agent_First_v<VERSION>.zip).
#
# By default this ALSO bumps the Deploy Agent's own version first (via
# scripts/bump-version.sh, auto-incrementing the last segment) -- every
# packaged deployable is meant to ship something new, and running
# bump-version.sh + package_installer.sh as two separate manual steps
# was easy to forget/get out of sync (see AGENTS.md: "Every modified
# deployable MESFlow ZIP must use a new version").
#
# Usage:
#   package_installer.sh                  # bump version (+1), then package
#   package_installer.sh --to 2.24.0      # bump to an explicit version, then package
#   package_installer.sh --no-bump        # package VERSION.txt exactly as it is now
#   package_installer.sh --out /path.zip  # explicit output path (combine with any of the above)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
die(){ echo "ERROR: $*" >&2; exit 1; }

BUMP=1
TARGET=""
OUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-bump) BUMP=0; shift ;;
    --to) TARGET="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    *) die "Unknown argument: $1 (usage: package_installer.sh [--no-bump] [--to VERSION] [--out PATH])" ;;
  esac
done
[[ "$BUMP" -eq 1 || -z "$TARGET" ]] || die "--to and --no-bump are mutually exclusive"

if [[ "$BUMP" -eq 1 ]]; then
  bash "$ROOT/scripts/bump-version.sh" ${TARGET:+"$TARGET"}
fi

VERSION="$(tr -d '\r\n' < "$ROOT/VERSION.txt")"
NAME="MESFlow_Server_Bootstrap_Agent_First_v${VERSION}"
OUT="${OUT:-$ROOT/../artifacts/deploy-agent/${NAME}.zip}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/$NAME/payload"
cp "$ROOT/installer/install.sh" "$ROOT/installer/update-gateway.sh" "$ROOT/installer/diagnose-gateway.sh" "$ROOT/installer/README.md" "$WORK/$NAME/"
# Reuse the same allow/exclude and secret gate as the source package.  The
# former cp -a copied .git/.venv and then tried to clean a small subset,
# which was both large and easy to get wrong.
SOURCE_ZIP="$WORK/source.zip"
python3 "$ROOT/tools/build_source_package.py" "$SOURCE_ZIP" >/dev/null
unzip -q "$SOURCE_ZIP" -d "$WORK/source"
SOURCE_ROOT="$WORK/source/mesflow-deploy-agent-source-${VERSION}"
test -f "$SOURCE_ROOT/agent.py" -a -f "$SOURCE_ROOT/VERSION.txt"
cp -a "$SOURCE_ROOT" "$WORK/$NAME/payload/mesflow-deploy-agent"
rm -rf "$WORK/$NAME/payload/mesflow-deploy-agent/installer"
chmod +x "$WORK/$NAME/install.sh" "$WORK/$NAME/update-gateway.sh" "$WORK/$NAME/diagnose-gateway.sh"
mkdir -p "$(dirname "$OUT")"
(cd "$WORK" && zip -qr "$OUT" "$NAME")
echo "$OUT"
