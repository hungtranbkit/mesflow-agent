# Deploy Agent OTA release qualification handoff

Current repo: `hungtranbkit/mesflow-agent`, branch `feat/esp-ota-force-scoped-20261111`, PR #1. Source baseline before this test-only update: `0759e8ca6dcfe0eb00f62e06851a5b2cb1f0c53d`.

## 2026-11-18 isolated qualification

- Product runtime files were not changed. `tests/test_it_operations_ui.py` now gives the fresh-install release-tab scenario an explicit empty state. A preceding orphan-job test persists `release_build_job=SUCCESS`; without isolation, the later test incorrectly inherited that state and failed. The exact two-test order reproduced the issue; the focused pair passes after the fixture correction.
- Agent release package was built in an isolated copy of source `0759e8c`, scratch version `2.24.51-docker-runtime`, with `scripts/build-agent-release.sh`. Builder suite: 531 passed, 16 skipped, 8 subtests passed; image `mesflow-deploy-agent:2.24.51`, image ID `sha256:7f27efc602af7401c77277cdf01f4f136edd7183093a728d75cca1188775e4c9`; ZIP SHA256 `b04053283de0ea4580250ada4d6f0604b80fcd14ec537eb8620ac5c7882e5cfd`. Artifact is local evidence only, stored outside this repo under `/home/dell/workspace/ota-release-e2e-20261117/artifacts/agent-2.24.51-docker-runtime/`; it was not uploaded or deployed.
- Actual `updater/updater.py` was exercised over loopback with the package against a private Compose project/network. It verified/extracted the ZIP, loaded the image by ID, ran `compose up -d --no-build --force-recreate mesflow-deploy-agent`, observed a new container ID, and polled `/agent/health` successfully at version `2.24.51-docker-runtime`, role `PRODUCTION_TEST`, deploy/host ops disabled. No published ports, bind mounts, production volumes, or Docker socket in the Agent container. Logs/result are under `/home/dell/workspace/ota-release-e2e-20261117/test-results/`.
- A prior full-suite run on the clean source checkout exposed test-order state leakage in the fresh-install UI test; isolated order reproduction passed after the test-only fix. Full suite on the updated clean commit and matching sibling-workspace layout is the remaining verification before merging this PR update.
- Earlier suite skips were affected by isolated Docker-network availability and workspace state. Read `RELEASE_GATES_FINAL.md` in the sibling OTA evidence directory for the complete gate ledger, log hashes, current CI/PR states, and exact limitations.

## Release and safety status

- PR #1 remains open/Ready; PR #50 remains Draft; firmware PR #2 remains open/Ready. No PR was merged, no deployment was performed, and fleet OTA remains disabled.
- No board or production service was contacted or modified. The production decision remains NO-GO until an organization-level automation review and an authorized canary are evidenced.
- Repository CI workflows and repo webhook lists were inspected. No repository webhooks were visible. The current GitHub token lacks `admin:org`; organization Apps/hooks and downstream server-side jobs remain unverified. An organization owner/admin with read-only access must inspect those systems before any release decision.
