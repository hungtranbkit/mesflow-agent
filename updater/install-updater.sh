#!/usr/bin/env bash
# One-time bootstrap of the host-side Agent updater. This is the ONLY step
# in the Agent-update lifecycle that uses SSH -- per policy, normal updates
# afterward go over HTTP only (DEV pushes the update artifact to
# /updater/update with a bearer token).
#
# Usage: sudo bash install-updater.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo bash install-updater.sh" >&2
  exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
UPDATER_HOME="${MESFLOW_AGENT_UPDATER_HOME:-/opt/mesflow-agent-updater}"
PORT="${MESFLOW_AGENT_UPDATER_PORT:-8099}"
BIND="${MESFLOW_AGENT_UPDATER_HOST:-127.0.0.1}"
TARGET_DIR="${MESFLOW_AGENT_TARGET_DIR:-/opt/mesflow-deploy-agent}"

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }

# Detect layout: new (single compose.yml + .env directly under TARGET_DIR)
# vs the older layered docker/compose.linux.yml(+overrides). Either way,
# this installer never edits the compose files themselves or restarts the
# Agent -- it only decides which files the updater will pass to `docker
# compose -f ...` on a *future* update, and captures the currently running
# container's actual environment into a fresh, updater-owned env file so
# the updater doesn't depend on a human re-supplying env vars by hand.
if [ -f "$TARGET_DIR/compose.yml" ]; then
  COMPOSE_FILES="compose.yml"
  ENV_FILE="$UPDATER_HOME/target.env"
  echo "Detected layout: single $TARGET_DIR/compose.yml"
elif [ -f "$TARGET_DIR/docker/compose.linux.yml" ]; then
  COMPOSE_FILES="docker/compose.linux.yml"
  [ -f "$TARGET_DIR/docker/compose.bootstrap.override.yml" ] && COMPOSE_FILES="$COMPOSE_FILES:docker/compose.bootstrap.override.yml"
  ROLE_NOW="$(docker inspect mesflow-deploy-agent --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n 's/^SERVER_ROLE=//p')"
  case "$ROLE_NOW" in
    PRODUCTION_TEST) [ -f "$TARGET_DIR/docker/compose.production-test.override.yml" ] && COMPOSE_FILES="$COMPOSE_FILES:docker/compose.production-test.override.yml" ;;
    PRODUCTION)      [ -f "$TARGET_DIR/docker/compose.production.override.yml" ] && COMPOSE_FILES="$COMPOSE_FILES:docker/compose.production.override.yml" ;;
  esac
  ENV_FILE="$UPDATER_HOME/target.env"
  echo "Detected layout: legacy layered compose under $TARGET_DIR/docker/ (role=$ROLE_NOW)"
  echo "Files: $COMPOSE_FILES"
else
  echo "Cannot find $TARGET_DIR/compose.yml or $TARGET_DIR/docker/compose.linux.yml -- install the Deploy Agent itself first" >&2
  exit 1
fi

# Bootstrap the env file the updater will maintain, from the currently
# running container's real environment (read-only inspection; nothing is
# changed on the running container by this step).
if [ ! -f "$ENV_FILE" ]; then
  docker inspect mesflow-deploy-agent --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | grep -E '^(AGENT_IMAGE|SERVER_ROLE|MESFLOW_BUILD_ENABLED|AGENT_BIND_IP|AGENT_PORT|MESFLOW_AGENT_DATA_DIR|MESFLOW_WORKSPACE_ROOT)=' \
    > "$ENV_FILE" || true
  # AGENT_IMAGE may not be an explicit env var (it can come purely from the
  # compose file's ${AGENT_IMAGE:-default}) -- if missing, record the
  # currently running image by id so rollback always has a real target.
  if ! grep -q '^AGENT_IMAGE=' "$ENV_FILE" 2>/dev/null; then
    CUR_IMAGE_ID="$(docker inspect mesflow-deploy-agent --format '{{.Image}}' 2>/dev/null || true)"
    [ -n "$CUR_IMAGE_ID" ] && echo "AGENT_IMAGE=$CUR_IMAGE_ID" >> "$ENV_FILE"
  fi
  echo "Bootstrapped $ENV_FILE from the currently running Agent container:"
  cat "$ENV_FILE"
else
  echo "Existing $ENV_FILE preserved (not regenerated)."
fi

install -d -m 0755 "$UPDATER_HOME" "$UPDATER_HOME/state"
cp -a "$SRC/updater.py" "$UPDATER_HOME/updater.py"

if [ -f "$UPDATER_HOME/.env" ] && grep -q '^MESFLOW_AGENT_UPDATER_TOKEN=' "$UPDATER_HOME/.env"; then
  echo "Existing updater token preserved (not regenerated)."
else
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  cat > "$UPDATER_HOME/.env" <<EOF
MESFLOW_AGENT_UPDATER_TOKEN=$TOKEN
MESFLOW_AGENT_TARGET_DIR=$TARGET_DIR
MESFLOW_AGENT_UPDATER_PORT=$PORT
MESFLOW_AGENT_UPDATER_HOST=$BIND
MESFLOW_AGENT_COMPOSE_FILES=$COMPOSE_FILES
MESFLOW_AGENT_ENV_FILE=$ENV_FILE
EOF
  chmod 600 "$UPDATER_HOME/.env"
  cat > "$UPDATER_HOME/FIRST_TOKEN.txt" <<EOF
mesflow-agent-updater bearer token (configure this as
MESFLOW_PRODUCTION_TEST_AGENT_UPDATER_TOKEN / MESFLOW_PRODUCTION_AGENT_UPDATER_TOKEN
on the DEV Agent, matching this target's role):

$TOKEN

Store this securely and delete this file after recording the token.
EOF
  chmod 600 "$UPDATER_HOME/FIRST_TOKEN.txt"
  echo "Generated new updater token; see $UPDATER_HOME/FIRST_TOKEN.txt"
fi

cat > /etc/systemd/system/mesflow-agent-updater.service <<EOF
[Unit]
Description=MESFlow Deploy Agent host-side updater
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=$UPDATER_HOME
EnvironmentFile=$UPDATER_HOME/.env
ExecStart=/usr/bin/python3 $UPDATER_HOME/updater.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable mesflow-agent-updater.service
systemctl restart mesflow-agent-updater.service
sleep 1
curl -fsS "http://$BIND:$PORT/updater/health"
echo
echo "mesflow-agent-updater installed and running on $BIND:$PORT (target: $TARGET_DIR)."
echo "Normal Agent updates from now on use this endpoint over HTTP -- no SSH required."
