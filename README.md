# MESFlow Deploy Agent

Current version: **2.24.8-docker-runtime**

The Agent manages MESFlow release upload, validation, dry-run, application deployment/rollback, health diagnostics, QA integration and server operations. Phase 2B keeps the production nginx gateway independent from application deployment.

## Recommended runtimes

- **Production Linux, existing installation:** native systemd remains supported and is still the lowest-risk production option.
- **Docker on Linux:** supported for full application deployment when `/opt/mesflow` is mounted at the same absolute path and the Docker socket is available.
- **Docker Desktop on Windows:** supported for Agent UI, health, diagnostics, release validation and dry-run. Real deployment is disabled by default.

Docker instructions: [`docs/DEPLOY_DOCKER.md`](docs/DEPLOY_DOCKER.md)

Repository layout: [`docs/STRUCTURE.md`](docs/STRUCTURE.md)

## Developer baseline

From this directory, with the dependencies in `requirements.txt` plus
`pytest` installed:

```bash
./scripts/test-baseline.sh
```

This runs Python compilation, the isolated fast test baseline, and source
ZIP verification. Long-running QA/soak tests remain separate. Build a clean
source archive explicitly with:

```bash
./scripts/build-source-package.sh
```

Build the immutable deployable Agent image/release on DEV only with:

```bash
./scripts/build-agent-release.sh
```

The application entry point remains `python agent.py` for compatibility.

## Native Linux install

```bash
chmod +x install_agent.sh
sudo ./install_agent.sh
```

Default URL: `http://SERVER-IP:8090/agent/`

Linux paths:

- Agent: `/opt/mesflow-agent`
- MESFlow: `/opt/mesflow`
- QA Center: `/opt/mesflow-qa-center`

MESFlow Docker updates preserve `.env`, `certs`, and `runtime`. Application deployment must not own the independent nginx gateway.

## Historical release notes

Historical update notes are retained under `docs/history/`.
