# Deploy Agent 2.24.13 — Dedicated Tutorial Monitor

- Added `/tutorial/monitor`, a separate progress/log screen similar to QA Center monitoring.
- `Xuất lại video hướng dẫn` now redirects straight to that monitor.
- Release & Deploy itself does not poll tutorial progress.
- Monitor uses one Server-Sent Events connection (`/tutorial/events`) and follows only `state.json` + the active `tutorial_*.log`; it does not call Docker health, release summaries, deployment scans, or other expensive endpoints.
- SSE closes automatically after terminal job status. Manual reload remains available if the connection drops.
- Added stage/updated_at fields for tutorial jobs.
- Docker builder failures now include a short tail diagnostic in the final message; the full log remains on the monitor.
- Log path is constrained to Agent `LOG_DIR/tutorial_*.log`; browser input can never choose an arbitrary path.
- No password is stored in state/log, and monitor does not expose credentials.

Observed user failure that motivated this change:
`Tutorial builder exited with code 125` — the new monitor exposes the Docker CLI/daemon lines immediately preceding that exit so the actual mount/network/runtime cause can be diagnosed.
