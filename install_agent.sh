#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo ./install_agent.sh" >&2
  exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
AGENT_HOME="${WORKSHOP_AGENT_HOME:-/opt/mesflow-agent}"
MES_HOME="${WORKSHOP_TARGET_HOME:-/opt/mesflow}"
QA_HOME="${MESFLOW_QA_HOME:-/opt/mesflow-qa-center}"
PORT="${WORKSHOP_AGENT_PORT:-8090}"
MES_URL="${WORKSHOP_MES_URL:-http://127.0.0.1:8080}"
QA_URL="${MESFLOW_QA_URL:-http://127.0.0.1:8095}"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
if ! python3 -m venv --help >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3-venv python3-pip
fi
if ! command -v docker >/dev/null; then
  echo "WARNING: Docker is not installed. Agent can run, but MESFlow Docker deploy will not work until Docker + Compose are installed."
fi

systemctl stop mesflow-deploy-agent.service 2>/dev/null || true
mkdir -p "$AGENT_HOME"
for f in agent.py reset_password.py requirements.txt VERSION.txt README.md UPDATE_NOTES_V2.6.2.txt UPDATE_NOTES_V2.6.3.txt UPDATE_NOTES_V2.6.4.txt UPDATE_NOTES_V2.6.5.txt UPDATE_NOTES_V2.6.6.txt UPDATE_NOTES_V2.7.0.txt UPDATE_NOTES_V2.8.0_LINUX.txt UPDATE_NOTES_V2.8.1_LINUX_STATUS_FIX.txt UPDATE_NOTES_V2.8.2_QA_SYSTEMD_FIX.txt UPDATE_NOTES_V2.9.0_SERVER_OPS.txt UPDATE_NOTES_V2.9.1_BOOT_RECOVERY.txt UPDATE_NOTES_V2.10.0_SAFE_DEPLOY.txt; do
  [ -e "$SRC/$f" ] && cp -a "$SRC/$f" "$AGENT_HOME/"
done
rm -rf "$AGENT_HOME/templates"
cp -a "$SRC/templates" "$AGENT_HOME/templates"

python3 -m venv "$AGENT_HOME/.venv"
"$AGENT_HOME/.venv/bin/python" -m pip install --disable-pip-version-check -r "$AGENT_HOME/requirements.txt"

cat > /etc/mesflow-deploy-agent.env <<EOF
WORKSHOP_AGENT_HOME=$AGENT_HOME
WORKSHOP_TARGET_HOME=$MES_HOME
WORKSHOP_AGENT_HOST=0.0.0.0
WORKSHOP_AGENT_PORT=$PORT
WORKSHOP_MES_URL=$MES_URL
WORKSHOP_AGENT_URL_PREFIX=/agent
MESFLOW_QA_HOME=$QA_HOME
MESFLOW_QA_URL=$QA_URL
MESFLOW_QA_PORT=8095
PYTHONUTF8=1
EOF
chmod 600 /etc/mesflow-deploy-agent.env

cat > /etc/systemd/system/mesflow-deploy-agent.service <<EOF
[Unit]
Description=MESFlow Deploy Agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$AGENT_HOME
EnvironmentFile=/etc/mesflow-deploy-agent.env
ExecStart=$AGENT_HOME/.venv/bin/python $AGENT_HOME/agent.py
Restart=always
RestartSec=3
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/bin/mesflow-agent <<'EOF'
#!/usr/bin/env bash
case "${1:-status}" in
  start|stop|restart|status) exec systemctl "$1" mesflow-deploy-agent.service ;;
  logs) exec journalctl -u mesflow-deploy-agent.service -f ;;
  version) curl -fsS http://127.0.0.1:8090/agent/health; echo ;;
  boot-status) exec systemctl status mesflow-boot-recovery.service --no-pager ;;
  boot-logs) exec journalctl -u mesflow-boot-recovery.service -b --no-pager ;;
  recover) exec systemctl restart mesflow-boot-recovery.service ;;
  *) echo "Usage: mesflow-agent {start|stop|restart|status|logs|version|boot-status|boot-logs|recover}"; exit 2 ;;
esac
EOF
chmod +x /usr/local/bin/mesflow-agent

# Ensure Docker itself starts after every reboot.
if systemctl list-unit-files docker.service >/dev/null 2>&1; then
  systemctl enable docker.service >/dev/null 2>&1 || true
  systemctl start docker.service >/dev/null 2>&1 || true
fi

# Restore the complete MESFlow runtime after boot.  This is intentionally a
# separate oneshot unit: the monitor agent must remain available even if the
# application stack cannot be recovered.
cat > /usr/local/sbin/mesflow-boot-recovery <<EOF
#!/usr/bin/env bash
set -u
MES_HOME="$MES_HOME"
QA_SERVICE="mesflow-qa-center.service"
LOG_TAG="mesflow-boot-recovery"
log(){ logger -t "\$LOG_TAG" -- "\$*" 2>/dev/null || true; echo "\$*"; }

log "boot recovery started"

# Docker daemon
systemctl start docker.service 2>/dev/null || true
for i in \$(seq 1 30); do
  docker info >/dev/null 2>&1 && break
  sleep 2
done

# MESFlow application stack only. The production gateway is an independent
# Compose project and is recovered by its own systemd/Compose lifecycle.
if [ -f "\$MES_HOME/compose.yml" ] && command -v docker >/dev/null 2>&1; then
  if docker compose -f "\$MES_HOME/compose.yml" up -d --no-build postgres mesflow; then
    log "MESFlow application stack restored; independent gateway untouched"
  else
    log "WARNING: MESFlow Docker stack restore failed"
  fi
else
  log "MESFlow compose.yml not found at \$MES_HOME; skipped"
fi

# QA Center is a systemd service in current MESFlow deployments.
if systemctl cat "\$QA_SERVICE" >/dev/null 2>&1; then
  systemctl enable "\$QA_SERVICE" >/dev/null 2>&1 || true
  systemctl restart "\$QA_SERVICE" >/dev/null 2>&1 || true
  log "QA Center recovery requested"
fi

log "boot recovery finished"
EOF
chmod 755 /usr/local/sbin/mesflow-boot-recovery

cat > /etc/systemd/system/mesflow-boot-recovery.service <<EOF
[Unit]
Description=MESFlow Boot Recovery
After=network-online.target docker.service
Wants=network-online.target docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/mesflow-boot-recovery
RemainAfterExit=yes
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF

# Open the Agent port as requested. If UFW exists but is inactive, this rule is
# still recorded and becomes effective when UFW is enabled later.
if command -v ufw >/dev/null 2>&1; then
  ufw allow "$PORT/tcp" comment 'MESFlow Server Agent' >/dev/null 2>&1 || ufw allow "$PORT/tcp" >/dev/null 2>&1 || true
fi

systemctl daemon-reload
systemctl enable mesflow-deploy-agent.service
systemctl enable mesflow-boot-recovery.service
systemctl start mesflow-deploy-agent.service
# Run recovery now as well, so installation leaves the machine in the same
# state expected after a future reboot.
systemctl restart mesflow-boot-recovery.service || true
sleep 2

echo
echo "MESFlow Deploy Agent installed."
echo "URL: http://SERVER-IP:$PORT/agent/"
echo "Status: mesflow-agent status"
echo "Logs:   mesflow-agent logs"
echo "Boot recovery: mesflow-agent boot-status"
echo "Manual recovery: mesflow-agent recover"
echo "Initial login file: $AGENT_HOME/config/FIRST_LOGIN.txt"
if command -v ufw >/dev/null 2>&1; then
  echo "UFW rule ensured: $PORT/tcp"
fi
