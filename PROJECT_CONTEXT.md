# Deploy Agent OTA release qualification handoff

Repo: `hungtranbkit/mesflow-agent`, branch `feat/esp-ota-force-scoped-20261111`, PR #1. Task code/test commits are `fbc9db61a2dad9e288cd81f5744af7a2e6ae63ef` and `fad98e97f47760cbd12492454c630a2f6706c245`.

## 2026-11-18 isolated qualification

- No Agent runtime/product source changed. Commit `fbc9db6` isolates the fresh-install release UI test from shared `WORKSHOP_AGENT_HOME` state: an earlier orphan-job test writes `release_build_job=SUCCESS`; the later UI test must render an empty install. The exact order failed before the fix and passed after it.
- Commit `fad98e9` makes the P4 vertical-slice PostgreSQL fixture require two successful `pg_isready` probes one second apart. The official Postgres image briefly starts a temporary init server, then shuts it down; a single readiness probe could falsely pass in that window and skip the DB tests. The four vertical slices pass after the fixture change.
- Full Agent suite from a clean sibling-workspace checkout at `fad98e9`: **544 passed, 3 skipped, 8 subtests passed**. Log SHA256 `e1f7971435c7d9831b4ad3843807be90457d579172f784112b27fee6e959fdef`. The three skips are explicit environment gates: Playwright package unavailable for one advanced UI test, frozen QA 1.19.12 artifact absent, and no real built MESFlow release artifact in the clean checkout. P4 vertical module separately: **4 passed**, log SHA256 `36d3c032661310a2bca08ed33752e1b2a3a72e71d0af871b03c25aa80726eb50`.
- Scratch package built with `scripts/build-agent-release.sh` from product source `0759e8c` and temporary version `2.24.51-docker-runtime`; build image `mesflow-deploy-agent:2.24.51`, image ID `sha256:7f27efc602af7401c77277cdf01f4f136edd7183093a728d75cca1188775e4c9`, ZIP SHA256 `b04053283de0ea4580250ada4d6f0604b80fcd14ec537eb8620ac5c7882e5cfd`. Local qualification only; artifact was not uploaded or deployed.
- Actual `updater/updater.py` received that ZIP on loopback in an isolated private Compose project/network. It verified the archive, loaded the image, ran `compose up -d --no-build --force-recreate mesflow-deploy-agent`, observed a new container ID, and polled `/agent/health`: version `2.24.51-docker-runtime`, role `PRODUCTION_TEST`, deploy and host operations disabled. No published ports, bind mounts, production volumes, or Docker socket were used. Logs/result live in `/home/dell/workspace/ota-release-e2e-20261117/test-results/`.
- CI for PR #1 at `fad98e9`: both ESP OTA gate jobs and both full workspace suite jobs passed. No product source changed in the qualification commits.

## Release and safety status

- PR #1 remains open/Ready; PR #50 remains Draft; firmware PR #2 remains open/Ready. No PR was merged and no deployment or fleet OTA action occurred.
- No board or production service was contacted or modified. Production release decision remains NO-GO until organization-level automation is reviewed and a separately authorized canary is evidenced.
- Repository workflows and webhook lists were inspected; no repository webhooks were visible. Current GitHub token lacks `admin:org`, so organization Apps/hooks and downstream server-side jobs remain unverified. An organization owner/admin with read-only access must inspect those systems.
- Full cross-repository evidence, hashes, gate ledger, blockers and next actions are in `/home/dell/workspace/ota-release-e2e-20261117/RELEASE_GATES_FINAL.md`.
