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

command -v docker >/dev/null || { echo "Docker is required" >&2; exit 1; }
docker compose version >/dev/null || { echo "Docker Compose plugin is required" >&2; exit 1; }
test -f "$MES_HOME/.env" || { echo "Missing persistent $MES_HOME/.env; it will not be created or copied from this installer" >&2; exit 1; }

echo "Resolved MESFLOW_WORKSPACE_ROOT=$MESFLOW_WORKSPACE_ROOT (override with MESFLOW_WORKSPACE_ROOT=/path if wrong)"
echo "Persistent Agent data dir: $AGENT_DATA_DIR (never recreated/wiped by this installer)"

# --- DEV vs PRODUCTION_TEST/PRODUCTION role -------------------------------
# Every `docker compose` call below used to hardcode
# `-f compose.linux.yml -f compose.bootstrap.override.yml` unconditionally.
# compose.linux.yml defaults SERVER_ROLE to PRODUCTION_TEST and
# MESFLOW_BUILD_ENABLED to 0 when nothing else sets them, and only
# compose.dev.override.yml (never included here) carries SERVER_ROLE=DEV,
# MESFLOW_BUILD_ENABLED=1 and the workspace bind-mount -- so re-running this
# installer to repair/update an already-DEV host (e.g. via Bootstrap's
# /install-agent) silently downgraded it to a PRODUCTION_TEST-shaped
# install every time, even though MESFLOW_WORKSPACE_ROOT was already being
# resolved above for exactly this purpose. Real incident: a local DEV
# machine's Deploy Agent lost SERVER_ROLE=DEV, MESFLOW_BUILD_ENABLED and its
# workspace mount this way.
#
# Explicit SERVER_ROLE=... always wins (same variable name compose.linux.yml
# itself reads). Otherwise auto-detect: DEV only when the resolved
# MESFLOW_WORKSPACE_ROOT is a real checkout of this monorepo on THIS host
# (concrete, verifiable signal -- not a guess), which is exactly the
# condition compose.dev.override.yml's own bind-mount already assumes.
# PRODUCTION_TEST/PRODUCTION hosts never have a workspace checkout, so they
# are unaffected and keep today's exact behavior.
COMPOSE_ARGS=(-f compose.linux.yml -f compose.bootstrap.override.yml)
DETECTED_ROLE="PRODUCTION_TEST (default)"
if [[ -n "${SERVER_ROLE:-}" ]]; then
  DETECTED_ROLE="$SERVER_ROLE (explicit SERVER_ROLE)"
  [[ "$SERVER_ROLE" == "DEV" ]] && COMPOSE_ARGS+=(-f compose.dev.override.yml)
elif [[ -f "$MESFLOW_WORKSPACE_ROOT/deploy-agent/docker/compose.dev.override.yml" ]]; then
  DETECTED_ROLE="DEV (auto-detected: $MESFLOW_WORKSPACE_ROOT is a real workspace checkout)"
  COMPOSE_ARGS+=(-f compose.dev.override.yml)
fi
echo "Server role: $DETECTED_ROLE -> compose files: ${COMPOSE_ARGS[*]}"

# --- detect an existing Agent container -------------------------------------
# IMPORTANT: `docker compose up --force-recreate` identifies "the container
# for this service" by Compose project/service *labels*, not by container
# name — so renaming the old container beforehand does NOT protect it from
# being removed by the recreate (verified empirically: a renamed container
# was still torn down by the next `up --force-recreate`). The only thing
# that reliably survives is the *image*, referenced by its immutable id, and
# the external data bind mount. Roll back by redeploying that exact image
# id, not by trying to resurrect the old container object.
EXISTED=0
PREV_IMAGE_ID=""
if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  EXISTED=1
  PREV_IMAGE_ID="$(docker inspect "$CONTAINER_NAME" --format '{{.Image}}')"
  echo "Existing Agent container '$CONTAINER_NAME' detected, running image id $PREV_IMAGE_ID."
  echo "That image id is not removed by this installer; rollback redeploys it by id if needed."
else
  if ss -lntp "sport = :${AGENT_PORT}" | grep -q LISTEN; then
    echo "Port ${AGENT_PORT} is already listening by something other than '$CONTAINER_NAME'; stop it before retrying" >&2
    exit 1
  fi
fi

rollback() {
  echo "ROLLBACK: new Agent failed to become healthy." >&2
  if [[ "$EXISTED" -eq 1 && -n "$PREV_IMAGE_ID" ]]; then
    echo "Redeploying the previous image ($PREV_IMAGE_ID) via compose (--no-build, same data dir)." >&2
    AGENT_BIND_IP="$AGENT_BIND_IP" AGENT_PORT="$AGENT_PORT" AGENT_IMAGE="$PREV_IMAGE_ID" \
      MESFLOW_AGENT_DATA_DIR="$AGENT_DATA_DIR" MESFLOW_WORKSPACE_ROOT="$MESFLOW_WORKSPACE_ROOT" \
      docker compose "${COMPOSE_ARGS[@]}" up -d --force-recreate mesflow-deploy-agent \
      && echo "ROLLBACK complete: previous Agent image restored and started." >&2 \
      || echo "ROLLBACK FAILED: could not restart the previous image ($PREV_IMAGE_ID). Manual intervention required." >&2
  else
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
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
  docker compose "${COMPOSE_ARGS[@]}" build mesflow-deploy-agent
BUILD_RC=$?
set -e
if [[ "$BUILD_RC" -ne 0 ]]; then rollback; fi

set +e
AGENT_BIND_IP="$AGENT_BIND_IP" AGENT_PORT="$AGENT_PORT" AGENT_IMAGE="$AGENT_IMAGE" \
  MESFLOW_AGENT_DATA_DIR="$AGENT_DATA_DIR" MESFLOW_WORKSPACE_ROOT="$MESFLOW_WORKSPACE_ROOT" \
  docker compose "${COMPOSE_ARGS[@]}" up -d --force-recreate mesflow-deploy-agent
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
  echo "Previous Agent image id kept for manual rollback if needed later:"
  echo "  AGENT_IMAGE=$PREV_IMAGE_ID docker compose ${COMPOSE_ARGS[*]} up -d --force-recreate mesflow-deploy-agent"
fi
