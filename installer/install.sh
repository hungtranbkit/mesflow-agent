#!/usr/bin/env bash
set -euo pipefail

# This installer is intentionally local-only: it never changes nginx, firewall,
# systemd, PostgreSQL data or Docker volumes.  Run it only after human review.
ROOT="$(cd "$(dirname "$0")" && pwd)"
PAYLOAD="$ROOT/payload/mesflow-deploy-agent"
AGENT_HOME="${MESFLOW_AGENT_HOME:-/opt/mesflow-deploy-agent}"
MES_HOME="${WORKSHOP_TARGET_HOME:-/opt/mesflow}"
AGENT_BIND_IP="${AGENT_BIND_IP:-127.0.0.1}"
AGENT_PORT="${AGENT_PORT:-8090}"
AGENT_VERSION="$(tr -d '\r\n' < "$PAYLOAD/VERSION.txt")"
AGENT_IMAGE_TAG="${AGENT_VERSION%-docker-runtime}"
AGENT_IMAGE="mesflow-deploy-agent:${AGENT_IMAGE_TAG}"

command -v docker >/dev/null || { echo "Docker is required" >&2; exit 1; }
docker compose version >/dev/null || { echo "Docker Compose plugin is required" >&2; exit 1; }
test -f "$MES_HOME/.env" || { echo "Missing persistent $MES_HOME/.env; it will not be created or copied from this installer" >&2; exit 1; }
if ss -lntp "sport = :${AGENT_PORT}" | grep -q LISTEN; then
  echo "Port ${AGENT_PORT} is already listening; stop only the reviewed local Agent before retrying" >&2
  exit 1
fi

install -d -m 0755 "$AGENT_HOME"
cp -a "$PAYLOAD/." "$AGENT_HOME/"
mkdir -p "$AGENT_HOME/docker/runtime/agent-data"
cd "$AGENT_HOME/docker"
AGENT_BIND_IP="$AGENT_BIND_IP" AGENT_PORT="$AGENT_PORT" AGENT_IMAGE="$AGENT_IMAGE" \
  docker compose -f compose.linux.yml -f compose.bootstrap.override.yml build mesflow-deploy-agent
AGENT_BIND_IP="$AGENT_BIND_IP" AGENT_PORT="$AGENT_PORT" AGENT_IMAGE="$AGENT_IMAGE" \
  docker compose -f compose.linux.yml -f compose.bootstrap.override.yml up -d --force-recreate mesflow-deploy-agent
curl -fsS "http://${AGENT_BIND_IP}:${AGENT_PORT}/agent/health"
echo "Installed local Deploy Agent ${AGENT_VERSION}; nginx/firewall/PostgreSQL were not modified."
