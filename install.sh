#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="mesflow-deploy-agent"
APP_VERSION="2.24.13-docker-runtime"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${MESFLOW_DEPLOY_AGENT_WORKSPACE_USER:-${SUDO_USER:-$(id -un)}}"

log(){ printf '\n[DEPLOY AGENT SOURCE INSTALL %s] %s\n' "$APP_VERSION" "$*"; }
fail(){ printf '\n[DEPLOY AGENT SOURCE INSTALL %s] ERROR: %s\n' "$APP_VERSION" "$*" >&2; exit 1; }

id "$RUN_USER" >/dev/null 2>&1 || fail "Không tìm thấy user: $RUN_USER"
if command -v getent >/dev/null 2>&1; then
  USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
else
  USER_HOME="$(eval echo "~$RUN_USER")"
fi
[[ -n "$USER_HOME" && "$USER_HOME" != "~$RUN_USER" ]] || fail "Không xác định được HOME của $RUN_USER"

WORKSPACE_ROOT="${MESFLOW_WORKSPACE_ROOT:-$USER_HOME/workspace/mesflow}"
TARGET_DIR="${MESFLOW_DEPLOY_AGENT_WORKSPACE_DIR:-$WORKSPACE_ROOT/deploy-agent}"
RUN_GROUP="$(id -gn "$RUN_USER")"

# Safety: source installer may only target the deploy-agent child directory.
WORKSPACE_REAL="$(realpath -m "$WORKSPACE_ROOT")"
TARGET_REAL="$(realpath -m "$TARGET_DIR")"
EXPECTED_REAL="$(realpath -m "$WORKSPACE_ROOT/deploy-agent")"
[[ "$TARGET_REAL" == "$EXPECTED_REAL" ]] || fail "REFUSE unsafe target: $TARGET_DIR (expected $WORKSPACE_ROOT/deploy-agent)"
[[ "$(basename "$TARGET_REAL")" == "deploy-agent" ]] || fail "REFUSE target basename must be deploy-agent: $TARGET_DIR"
[[ "$TARGET_REAL" != "$WORKSPACE_REAL" ]] || fail "REFUSE target cannot equal workspace root"

[[ -f "$SOURCE_DIR/VERSION.txt" ]] || fail "Gói thiếu VERSION.txt"
PACKAGE_VERSION="$(tr -d '[:space:]' < "$SOURCE_DIR/VERSION.txt")"
[[ "$PACKAGE_VERSION" == "$APP_VERSION" ]] || fail "Version gói $PACKAGE_VERSION không khớp installer $APP_VERSION"
[[ -f "$SOURCE_DIR/PROJECT.yaml" ]] || fail "Gói thiếu PROJECT.yaml"
[[ -f "$SOURCE_DIR/agent.py" ]] || fail "Gói thiếu agent.py"

# Installer này CHỈ đưa source vào workspace. Không build/deploy/restart.
if [[ "$(realpath -m "$SOURCE_DIR")" == "$(realpath -m "$TARGET_DIR")" ]]; then
  log "Source đã nằm đúng workspace: $TARGET_DIR"
  echo "Không copy và không deploy gì thêm."
  exit 0
fi

command -v rsync >/dev/null 2>&1 || fail "Thiếu rsync. Installer copy-source không tự cài package hệ thống."

log "Cài source vào workspace chuẩn"
echo "Source : $SOURCE_DIR"
echo "Target : $TARGET_DIR"
echo "User   : $RUN_USER"

# Preserve host-local config/secrets if the workspace already exists.
TMP_DIR="$(mktemp -d)"
cleanup(){ rm -rf "$TMP_DIR"; }
trap cleanup EXIT
for keep in .env .env.local config.local.json; do
  if [[ -f "$TARGET_DIR/$keep" ]]; then
    cp -a "$TARGET_DIR/$keep" "$TMP_DIR/$keep"
  fi
done

mkdir -p "$TARGET_DIR"
rsync -a \
  --exclude '.git/' \
  --exclude '.env' \
  --exclude '.env.local' \
  --exclude 'config.local.json' \
  --exclude 'docker/runtime/' \
  --exclude 'runtime/' \
  --exclude 'artifacts/' \
  --exclude 'logs/' \
  --exclude 'backups/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.pytest_cache/' \
  --exclude 'test-results/' \
  --exclude 'playwright-report/' \
  "$SOURCE_DIR/" "$TARGET_DIR/"

for keep in .env .env.local config.local.json; do
  if [[ -f "$TMP_DIR/$keep" ]]; then
    cp -a "$TMP_DIR/$keep" "$TARGET_DIR/$keep"
  fi
done

# If run under sudo, return workspace source ownership to the real user.
if [[ "$(id -u)" -eq 0 ]]; then
  chown -R "$RUN_USER:$RUN_GROUP" "$TARGET_DIR"
fi

INSTALLED_VERSION="$(tr -d '[:space:]' < "$TARGET_DIR/VERSION.txt")"
[[ "$INSTALLED_VERSION" == "$APP_VERSION" ]] || fail "Copy xong nhưng VERSION.txt=$INSTALLED_VERSION"
[[ -f "$TARGET_DIR/PROJECT.yaml" && -f "$TARGET_DIR/agent.py" ]] || fail "Workspace thiếu PROJECT.yaml/agent.py sau khi copy"

log "HOÀN TẤT - CHỈ COPY SOURCE"
echo "Workspace : $TARGET_DIR"
echo "Version   : $INSTALLED_VERSION"
echo
printf '%s\n' \
  "Installer KHÔNG thực hiện:" \
  "  - docker build" \
  "  - docker compose up/restart" \
  "  - sửa /opt/mesflow-deploy-agent" \
  "  - deploy Local/Test/Production" \
  "  - thay đổi database/runtime" \
  "" \
  "Build/deploy tiếp theo do Deploy Agent / ProjectFlow thực hiện từ workspace này."
