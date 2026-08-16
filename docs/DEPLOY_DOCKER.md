# MESFlow Deploy Agent — Docker deployment

Version: **2.24.3-docker-runtime**

## Runtime model

The same Agent image can run on Docker Desktop (Windows) and Docker Engine (Linux), but the safety scope is intentionally different:

- **Windows / Docker Desktop:** UI, health, upload, release validation, Docker diagnostics and dry-run. Real MESFlow deployment is disabled by default because a Linux container cannot safely preserve Windows host bind-path semantics for a source-replacing deployment.
- **Linux:** full deployment is supported when `/opt/mesflow` is bind-mounted at the **same absolute path** inside the Agent container and the Docker socket is mounted.
- Host `systemd` operations are disabled in container mode. Keep gateway, PostgreSQL, systemd and boot-recovery lifecycle outside the containerized Agent.

The Agent still follows Phase 2B ownership: application deployment may control the MESFlow app tier; it must not own the independent nginx gateway. PostgreSQL lifecycle remains protected separately.

## Windows / Docker Desktop

PowerShell from the Agent repository:

```powershell
$env:MESFLOW_WORKSPACE="D:\MESFlowWorkspace\mesflow"
docker compose -f docker\compose.windows.yml build
docker compose -f docker\compose.windows.yml up -d
docker compose -f docker\compose.windows.yml ps
```

Open:

```text
http://127.0.0.1:8090/agent/
```

Health:

```powershell
Invoke-RestMethod http://127.0.0.1:8090/agent/health
```

Expected health metadata:

```text
runtime_mode = docker-windows
deploy_enabled = false
host_ops_enabled = false
```

To stop without deleting Agent data:

```powershell
docker compose -f docker\compose.windows.yml stop
```

To remove/recreate only the Agent container while preserving its named data volume:

```powershell
docker compose -f docker\compose.windows.yml rm -sf
docker compose -f docker\compose.windows.yml up -d
```

Do **not** use `down -v` if you want to preserve Agent credentials/history/uploads.

## Linux — full deploy mode

Prerequisites:

1. Docker Engine + Compose plugin.
2. MESFlow application source/runtime at `/opt/mesflow`.
3. External network `mesflow-edge` exists.
4. The application is reachable on the edge network as `mesflow-app:8080`.
5. Gateway and PostgreSQL lifecycle are independently protected.

Run:

```bash
cd /path/to/mesflow-deploy-agent
mkdir -p docker/runtime/agent-data
docker network inspect mesflow-edge >/dev/null 2>&1 || docker network create mesflow-edge
docker compose -f docker/compose.linux.yml build
docker compose -f docker/compose.linux.yml up -d
docker compose -f docker/compose.linux.yml ps
curl -fsS http://127.0.0.1:8090/agent/health
```

The production reverse proxy should route `agent.mesflow.net` to host port `8090`; the Agent container itself does not own/reload nginx.

Release builds must use `./scripts/build-agent-release.sh` on DEV. Promotion
targets consume that frozen image/ZIP and must use `--no-build`; they never
rebuild the source independently.

## Linux — safe inspection/dry-run mode

Use the normal Linux compose file but override deploy permission:

```bash
MESFLOW_AGENT_DEPLOY_ENABLED=0 docker compose -f docker/compose.linux.yml up -d --build
```

or edit the environment value before startup. `/agent/health` must report `deploy_enabled: false`.

## Why Windows real deploy is disabled

The current MESFlow release Compose uses host bind paths. When `docker compose` runs *inside another Linux container* on Docker Desktop, client-side absolute path resolution does not reliably represent a Windows host path to the Docker daemon. Allowing source replacement in that configuration can create wrong bind mounts or target the wrong filesystem. Therefore Windows container mode is deliberately dry-run/diagnostic-only.

For a full Windows deployment workflow, run the Agent natively on Windows or convert MESFlow runtime/source ownership to a Docker-volume/immutable-image deployment model first.

## Security notes

Mounting `/var/run/docker.sock` gives the Agent effective control of the Docker host. Treat access to the Agent UI as privileged:

- bind port 8090 to loopback on Linux and expose it only through the authenticated gateway;
- retain Agent authentication;
- never mount `/etc`, `/`, PostgreSQL PGDATA, certificate directories, or the gateway configuration into this container;
- never put secrets in the image or Compose file; use an env file/secret store at runtime;
- back up the Agent data directory/volume before replacing an existing installation.
