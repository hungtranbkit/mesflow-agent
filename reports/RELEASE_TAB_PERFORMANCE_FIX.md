# Release Tab Performance Fix — Deploy Agent 2.23.15

## Problem
Opening Release & Deploy while a build/deploy/promotion job was active caused a full page reload every 3 seconds. Each full reload rebuilt expensive release/health/deployment context and also tailed local log files. On a busy DEV host this could make the Agent UI sluggish or appear frozen.

## Fix
- Removed routine log tail reads from Release page context.
- Added `/api/release-manager/live-status`, a lightweight persisted-state-only polling endpoint.
- Release tab now polls status every 5 seconds only while a job is active.
- Polling pauses while the tab is hidden.
- The page reloads once, after the active job completes, to refresh immutable release/promotion evidence.
- No release/build/deploy business semantics changed.

## Verification
- Python compile: PASS.
- New regression tests: 3 passed.
- Wider focused tests could not collect in this inspection environment because system Python lacks `paramiko`; this is an environment dependency issue, not a failing assertion.
