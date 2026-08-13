#!/usr/bin/env bash
set -euo pipefail
curl -fsS --max-time 10 http://127.0.0.1:8090/agent/health
docker ps --filter name=mesflow-deploy-agent
