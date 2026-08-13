# Docker quick start

See [`../docs/DEPLOY_DOCKER.md`](../docs/DEPLOY_DOCKER.md).

Windows Docker Desktop:

```powershell
$env:MESFLOW_WORKSPACE="D:\MESFlowWorkspace\mesflow"
docker compose -f docker\compose.windows.yml up -d --build
```

Linux Docker Engine:

```bash
docker compose -f docker/compose.linux.yml up -d --build
```
