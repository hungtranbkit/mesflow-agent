# Deploy Agent 2.24.11 — Workspace Source Installer

Added a root `install.sh` matching the QA Center source-install workflow.

Behavior:
- Copies this source package to `~/workspace/mesflow/deploy-agent` by default.
- Supports `MESFLOW_WORKSPACE_ROOT`, `MESFLOW_DEPLOY_AGENT_WORKSPACE_DIR`, and `MESFLOW_DEPLOY_AGENT_WORKSPACE_USER` overrides.
- Refuses targets outside the exact `deploy-agent` workspace child.
- Preserves `.env`, `.env.local`, and `config.local.json` already present in the target.
- Excludes runtime/generated state such as `docker/runtime`, `runtime`, `artifacts`, logs, caches, and virtualenvs.
- Does not build, restart, deploy, or mutate `/opt` runtime.
- Version bumped from 2.24.10 to 2.24.11.
