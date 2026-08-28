# Deploy Agent 2.24.10 — Server Tutorial Generation Re-enabled

## Changes
- Re-enabled POST /tutorial/check-login and POST /tutorial/rebuild on DEV Agents.
- Production/Test agents with MESFLOW_BUILD_ENABLED=0 return 403 SERVER_VIDEO_GENERATION_DEV_ONLY.
- Tutorial builder now reads MESFlow source from MESFLOW_SOURCE_DIR instead of assuming /opt/mesflow contains source.
- Published videos remain under /opt/mesflow/runtime/tutorials.
- Restored Release & Deploy UI card with login preflight, temporary tutorial data option, background build button, status and recent history.
- Password is passed only via child process environment and is not persisted in Agent state/logs.
- Agent version bumped from 2.24.9-docker-runtime to 2.24.10-docker-runtime.

## Validation
- python3 -m py_compile agent.py: PASS
- tests/test_tutorial_server_generation_v22410.py: 4/4 PASS
- Jinja parse templates/release/_deploy_tab.html: PASS
- Relevant static/lightweight tests that do not import missing baseline dependencies: PASS

## Baseline test limitations
- The supplied v2.24.9 source package does not contain docker/Dockerfile, so tests/test_p0_hygiene.py cannot complete from this package alone.
- The current test environment does not have paramiko installed, so tests importing agent.py through that dependency fail at collection.
