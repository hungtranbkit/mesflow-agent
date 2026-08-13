#!/usr/bin/env bash
set -euo pipefail

# This installer is intentionally local-only: it never changes nginx, firewall,
# systemd, PostgreSQL data or Docker volumes.  Run it only after human review.
#
# Usage: sudo bash install.sh
#
# Workspace/source resolution (used only to record where this Agent's
# optional DEV bind-mount should point; PRODUCTION_TEST/PRODUCTION installs
# never need workspace source at all):
#   MESFLOW_WORKSPACE_ROOT=/path   explicit override, always wins
#   otherwise: /home/$SUDO_USER/workspace/mesflow when this script was
#   invoked with `sudo` by a real (non-root) user
#   otherwise: /home/<login-owner-of-controlling-tty>/workspace/mesflow
#   otherwise: $HOME/workspace/mesflow (may be /root/... — a warning is
#   printed so this is never silently wrong)
resolve_workspace_root() {
  if [[ -n "${MESFLOW_WORKSPACE_ROOT:-}" ]]; then
    printf '%s\n' "$MESFLOW_WORKSPACE_ROOT"
    return
  fi
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    printf '/home/%s/workspace/mesflow\n' "$SUDO_USER"
    return
  fi
  local login
  login="$(logname 2>/dev/null || true)"
  if [[ -n "$login" && "$login" != "root" ]]; then
    printf '/home/%s/workspace/mesflow\n' "$login"
    return
  fi
  echo "WARNING: could not detect the invoking non-root user (no SUDO_USER/logname);" >&2
  echo "         falling back to \$HOME/workspace/mesflow ($HOME/workspace/mesflow)." >&2
  echo "         Set MESFLOW_WORKSPACE_ROOT=/home/<user>/workspace/mesflow explicitly" >&2
  echo "         if this is wrong." >&2
  printf '%s/workspace/mesflow\n' "$HOME"
}

ROOT="$(cd "$(dirname "$0")" && pwd)"
PAYLOAD="$ROOT/payload/mesflow-deploy-agent"
AGENT_HOME="${MESFLOW_AGENT_HOME:-/opt/mesflow-deploy-agent}"
AGENT_DATA_DIR="${MESFLOW_AGENT_DATA_DIR:-/var/lib/mesflow-deploy-agent}"
MES_HOME="${WORKSHOP_TARGET_HOME:-/opt/mesflow}"
AGENT_BIND_IP="${AGENT_BIND_IP:-127.0.0.1}"
AGENT_PORT="${AGENT_PORT:-8090}"
AGENT_VERSION="$(tr -d '\r\n' < "$PAYLOAD/VERSION.txt")"
AGENT_IMAGE_TAG="${AGENT_VERSION%-docker-runtime}"
AGENT_IMAGE="mesflow-deploy-agent:${AGENT_IMAGE_TAG}"
MESFLOW_WORKSPACE_ROOT="$(resolve_workspace_root)"
CONTAINER_NAME="mesflow-deploy-agent"
TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_NAME="${CONTAINER_NAME}-preinstall-${TS}"

command -v docker >/dev/null || { echo "Docker is required" >&2; exit 1; }
docker compose version >/dev/null || { echo "Docker Compose plugin is required" >&2; exit 1; }
test -f "$MES_HOME/.env" || { echo "Missing persistent $MES_HOME/.env; it will not be created or copied from this installer" >&2; exit 1; }

echo "Resolved MESFLOW_WORKSPACE_ROOT=$MESFLOW_WORKSPACE_ROOT (override with MESFLOW_WORKSPACE_ROOT=/path if wrong)"
echo "Persistent Agent data dir: $AGENT_DATA_DIR (never recreated/wiped by this installer)"

# --- detect an existing Agent container -------------------------------------
EXISTED=0
if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  EXISTED=1
  echo "Existing Agent container '$CONTAINER_NAME' detected; it will be preserved as a rollback target."
  # Free the port/name without discarding it: rename, then stop. Its data
  # lives in the external bind mount ($AGENT_DATA_DIR), not in the
  # container, so nothing persistent is touched by rename/stop.
  docker rename "$CONTAINER_NAME" "$BACKUP_NAME"
  docker stop "$BACKUP_NAME" >/dev/null
else
  if ss -lntp "sport = :${AGENT_PORT}" | grep -q LISTEN; then
    echo "Port ${AGENT_PORT} is already listening by something other than '$CONTAINER_NAME'; stop it before retrying" >&2
    exit 1
  fi
fi

rollback() {
  echo "ROLLBACK: new Agent failed to become healthy; restoring previous Agent." >&2
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  if [[ "$EXISTED" -eq 1 ]]; then
    docker rename "$BACKUP_NAME" "$CONTAINER_NAME"
    docker start "$CONTAINER_NAME" >/dev/null
    echo "ROLLBACK complete: previous Agent container restored and started." >&2
  else
    echo "ROLLBACK complete: no previous Agent existed; nothing to restore." >&2
  fi
  exit 1
}

install -d -m 0755 "$AGENT_HOME"
install -d -m 0755 "$AGENT_DATA_DIR"
cp -a "$PAYLOAD/." "$AGENT_HOME/"
rm -rf "$AGENT_HOME/docker/runtime"   # legacy in-tree runtime path; data lives in $AGENT_DATA_DIR now
cd "$AGENT_HOME/docker"

set +e
AGENT_BIND_IP="$AGENT_BIND_IP" AGENT_PORT="$AGENT_PORT" AGENT_IMAGE="$AGENT_IMAGE" \
  MESFLOW_AGENT_DATA_DIR="$AGENT_DATA_DIR" MESFLOW_WORKSPACE_ROOT="$MESFLOW_WORKSPACE_ROOT" \
  docker compose -f compose.linux.yml -f compose.bootstrap.override.yml build mesflow-deploy-agent
BUILD_RC=$?
set -e
if [[ "$BUILD_RC" -ne 0 ]]; then rollback; fi

set +e
AGENT_BIND_IP="$AGENT_BIND_IP" AGENT_PORT="$AGENT_PORT" AGENT_IMAGE="$AGENT_IMAGE" \
  MESFLOW_AGENT_DATA_DIR="$AGENT_DATA_DIR" MESFLOW_WORKSPACE_ROOT="$MESFLOW_WORKSPACE_ROOT" \
  docker compose -f compose.linux.yml -f compose.bootstrap.override.yml up -d --force-recreate mesflow-deploy-agent
UP_RC=$?
set -e
if [[ "$UP_RC" -ne 0 ]]; then rollback; fi

# --- health check with retries, then rollback on failure --------------------
HEALTHY=0
for _ in $(seq 1 15); do
  if curl -fsS "http://${AGENT_BIND_IP}:${AGENT_PORT}/agent/health" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 2
done
if [[ "$HEALTHY" -ne 1 ]]; then
  rollback
fi

curl -fsS "http://${AGENT_BIND_IP}:${AGENT_PORT}/agent/health"
echo
echo "Installed local Deploy Agent ${AGENT_VERSION}; nginx/firewall/PostgreSQL were not modified."
if [[ "$EXISTED" -eq 1 ]]; then
  echo "Previous Agent preserved (stopped) as container '$BACKUP_NAME' for manual rollback:"
  echo "  docker rm -f $CONTAINER_NAME && docker rename $BACKUP_NAME $CONTAINER_NAME && docker start $CONTAINER_NAME"
fi
