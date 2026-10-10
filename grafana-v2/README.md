# MCP Observatory v2 - Grafana-backed, identical inspector UI

This is a standalone alternative to MCP Observatory v1 in the `feature/grafana-observatory-parity` branch.

- Existing four-tab UI is preserved from v1, including charts, filters, request detail, tool input/output, historical events, health and raw metrics. The fifth tab embeds 12 Grafana metric charts.
- Private Grafana and Prometheus run independently of FarmToGo. They scrape the existing six tunnel `/metrics` endpoints every 15 seconds; no MCP server code or systemd units change.
- A separate lightweight `gateway` container runs only MCP HTTP forwarding and redacted request capture. This gateway remains available even when Grafana/inspector is down.
- A new inspector runs the original compatibility API and request-correlation logic, with 15-second polling and an hourly retention cleanup on an indexed timestamp rather than a full table scan every second.
- Original database/history is left untouched; migrated request/event/capture history is in `grafana-v2/data/inspector/observability.db` (ignored from Git). Legacy 1.4 GB DB remains on disk for rollback/audit, including historical raw snapshots.

## Ports

| Component | Staging/production local port | Exposure |
| --- | --- | --- |
| Inspector + original UI + `/grafana/` | `127.0.0.1:9120` | Tailnet HTTPS 8470 after cutover |
| Grafana (native) | `127.0.0.1:3120` | Only via inspector reverse proxy |
| Prometheus | `127.0.0.1:9191` | Loopback only |
| Gateway | staging 28900/28017/28765/27874; production 18900/18017/18765/17874 | Local MCP transport only |

## Development and tests

- `docker compose up -d --build` starts new Prometheus, Grafana and inspector without binding the live MCP ports.
- `docker compose --profile capture up -d gateway` starts the gateway on staging ports (unless `.env` config says otherwise).
- `python3 tests/parity.py` checks frozen historical API equality while v1 is running.
- `node tests/ui_parity.cjs` runs actual headless Chrome UI interaction tests.
- `node tests/grafana_diag.cjs` verifies Grafana panels render data.
- `bash tests/original-smoke.sh http://127.0.0.1:9120` runs the inherited production smoke test.
- `python3 scripts/sync_history.py` imports newer legacy requests, telemetry and captures before cutover, preserving existing v2 values.
- `bash scripts/cutover.sh` performs a guarded reversible production cutover to HTTPS 8470, disables the old boot service and container restart, installs the v2 systemd unit and verifies every MCP tunnel PID remains unchanged. It reverts to the old dashboard if any mandatory check fails.

## Rollback

Keep the old code, service file and database; do not delete them. To roll back manually, stop `mcp-observatory-v2.service` and its capture gateway, set `docker update --restart=unless-stopped mcp-observability-dashboard`, enable/start `mcp-observability-dashboard.service`, and point Tailscale HTTPS 8470 to `http://127.0.0.1:9111` again. Never restart the MCP servers or tunnel processes as part of this rollback.

## Safety

This stack persists user tool input/output, which can contain sensitive material after best-effort redaction. Keep tailnet access controlled and never publish the SQLite database, captures or Grafana writable configuration to Git. Grafana anonymous access is only exposed behind the existing tailnet-only URL. Do not modify or restart any MCP server to install or operate this stack.

## Live deployment (October 9, 2026)

The production cutover succeeded using the rollback-safe `scripts/cutover.sh`.
The old `mcp-observability-dashboard.service` is disabled and inactive, its Docker container restart policy is `no`, and the new `mcp-observatory-v2.service` is enabled and active. Tailnet-only HTTPS port 8470 now fronts the new UI, Grafana at `/grafana/`, and the API. The staging 8471 Serve route was removed. All monitored MCP/tunnel process PIDs were verified unchanged. No reboot was performed, because rebooting would restart MCP servers against the user's restrictions.

A successful live MCP `tools/call` request was captured by the replacement standalone gateway and linked to its request input/output in the new database. The legacy 1.4 GB database was retained without deletion. All 17 FarmToGo containers remained running and were not modified.
