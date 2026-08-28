# Deploy Agent 2.24.37 Source Reconciliation

## Provenance

- Workspace before: `2.24.8-docker-runtime`, commit `94312df`
- Requested deployed snapshot: `2.24.37-docker-runtime`
- Exact image: `mesflow-deploy-agent:2.24.37`
- Image ID/digest: `sha256:98a48cb314a1526fa7092f359296b3020fad9c6e4d39c0d12772b10222564ff2`
- Image created: `2026-08-21T09:11:44.720642231+07:00`
- OCI label: `org.opencontainers.image.version=2.24.37-docker-runtime`
- Recovery package: `artifacts/deploy-agent/MESFlow_Server_Bootstrap_Agent_First_v2.24.37-docker-runtime.zip`
- Recovery package SHA256: `02667f53ed741d5b5a0f26a82ebef8a12a5e3537bb1a723a321aad59a0af57af`
- Temporary recovered image root: `/tmp/deploy-agent-2.24.37-image`
- Recovery branch: `agent/codex/ui-quality-audit`

The package's runtime source hashes match the corresponding files extracted
from the immutable image. The image and package therefore provide independent
evidence for the recovered application source.

At reconciliation time, the running local container had already advanced to
`2.24.48-docker-runtime` (`sha256:416698aa03ff5ac6f70c923542fe8ec6223faa8979522a29395d9c07bfa0c9d6`).
It was inspected read-only and was not restarted or modified.

## Classification

- `ADDED`: source installer, diagnostics/version-doctor scripts, tutorial
  monitor template, chunked QA promotion tests, and supporting reports.
- `MODIFIED`: Agent application, release/deploy UI templates, build/version
  tooling, compose/image version declarations, documentation, and regression
  tests.
- `DELETED`: none. Workspace-only legacy installer/tests and recovery metadata
  were preserved because package omission did not prove intentional deletion.
- `VERSION-ONLY`: `VERSION.txt`, README, Dockerfile, compose files, and Docker
  deployment documentation were synchronized to `2.24.37-docker-runtime`.
- `GENERATED`: `.version-hardening-backup-*`, `BUILD_REPORT.txt`, Python caches,
  pytest caches, and runtime logs/state were not restored.
- `CONFIG-ONLY`: `.env`, runtime state, promotion state, release-target runtime
  configuration, and `/var/lib/mesflow-deploy-agent` content were not restored.

## Relevant recovered behavior

- Chunked QA release transfer for proxy-safe large artifact promotion.
- QA promotion preflight and actionable error explanations.
- Tutorial build monitoring and diagnostics.
- Agent source installation/version-doctor hardening.
- Release/Deploy UI visibility improvements present in the recovered build.

## Verification

- Version doctor: PASS.
- Recovery-focused tests: `33 passed, 1 skipped`.
- Broad project tests excluding four stale cross-project contract files:
  `435 passed, 3 skipped`.
- Isolated Docker build: PASS, image
  `mesflow-deploy-agent:reconcile-2.24.37-codex`, ID
  `sha256:9a4a034bfd02ec86db827b11813c9a07def8be347d971bd7700858c8448f5bb4`.
- Declared preflight: unavailable because `scripts/preflight.sh` is absent from
  both the original workspace and recovered package.

The four excluded cross-project files assert superseded MESFlow version-bump
internals, require a missing workspace-level promotion template fixture, or
hard-code the canonical sibling layout. They are audit findings and were not
weakened to manufacture a full-suite PASS.

## Runtime safety

No running container, TEST system, or PRODUCTION system was modified.
