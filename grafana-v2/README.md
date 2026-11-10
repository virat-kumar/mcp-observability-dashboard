# MCP Observatory v2 — Production Grafana + low-CPU inspector

The **production** dashboard is available through the original tailnet-only address:
`https://desktop-ubuntu.tailac2e85.ts.net:8470/`.

The original four tabs are preserved: Overview, Requests, Health, and Telemetry.
A fifth tab embeds twelve native Grafana/Prometheus panels. The legacy
frontend `app.js` is byte-for-byte unchanged; inspector API compatibility and
full Playwright regression tests are included in this folder.

## Independent services, unchanged MCP tunnels

| Component | Local port(s) | Description |
| --- | --- | --- |
| Inspector | 127.0.0.1:9120 | Original UI/APIs, 15-second collector, Grafana reverse proxy |
| Grafana | 127.0.0.1:3120 | Twelve MCP metric charts |
| Prometheus | 127.0.0.1:9191 | Scrapes twelve active MCP tunnels and inspector metrics |
| Capture gateway | 18900, 18017, 18765, 17874 | Original HTTP listener ports for four MCP tunnels |
| HTTPS | Tailnet-only 8470 | Forwards to inspector, with Grafana at `/grafana/` |

No MCP server or MCP tunnel service source/config is modified. The capture gateway
is an independent process; the dashboard can be restarted without restarting the
MCP forwarding path. FarmToGo's Grafana, databases, and containers are untouched.

## MCP inventory and retired versions

### OAuth startup readiness repair (2026-11-09)

Tunnel-client 0.0.13 was observed retaining boot-time DNS, tunnel metadata and
OAuth discovery failures after its gateway recovered. The five affected tunnels
were Terminal, Plans, Jupyter, Tailnet File Share and Craft Anything. Their local
gateways were healthy while tunnel `/readyz` continued returning 503.

The source-controlled drop-ins in `deploy/systemd/tunnel-oauth-startup/` add a
bounded pre-start DNS/local-gateway metadata check and a post-start check of
`/readyz`, `/api/oauth` and `/api/status`. Failed checks fail service startup so
the existing restart policy retries discovery. The installed helper is
`~/.local/lib/mcp-tunnel-guards/wait_mcp_startup.py`, copied from
`scripts/wait_mcp_startup.py`; installed drop-ins are named
`oauth-startup-readiness.conf` under each affected system service's `.d` directory.
They do not change credentials, gateway policies, DNS settings or MCP servers.

All five clients were reconnected one at a time after checking worker occupancy;
readiness returned 200 and both metadata errors cleared. Terminal's 200 response
may explain that initialization needs authentication; that is expected and does
not authorize anonymous tools. No full reboot or fresh authenticated ChatGPT tool
execution was performed. Empty/null startup log lists are handled by the inspector.

Preserve these checks during future tunnel upgrades, run `test_tunnel_startup.py`,
and compare installed helper/drop-ins with their source. Rollback removes only
these five drop-ins and reloads systemd; original unit/profile files remain intact.
Do not re-enable retired unauthenticated profiles as recovery.

`app/mcp_inventory.json` is the shared inventory for the inspector and generated
Prometheus targets. The verified local inventory has fourteen services: Terminal,
Playwright, Computer Use Linux, Personal, Excel, Background Agent, Desktop Live,
Chrome Live, Plans, Jupyter, Tailnet File Share, Craft Anything, Neo4j and Job Queue.
The first twelve use tunnel telemetry. Neo4j and Job Queue are explicitly marked
health-only; authenticated tool execution and latency are not inferred from health.

Historical keys for renamed MCPs stay unchanged, preserving request history.
Deprecated unauthenticated plugin versions are listed under their OAuth replacement
in Health, rather than polled as separate broken services. Terminal's retired
legacy transport `:8080`, English variant `:8901` and development variant `:8912`
are grouped under active Terminal `:18880`. Other active tunnel ports such as
`:8081` are still used by OAuth versions and must not be retired based on age.
This inventory covers this Ubuntu machine; remote MCP hosts and cloud-only apps
are not silently probed or counted as locally hosted services.

When adding or moving an MCP, update the inventory, then run:

```bash
python3 scripts/generate_prometheus_config.py
python3 scripts/generate_prometheus_config.py --check
python3 scripts/generate_grafana_dashboard.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Read-only observation is the default: automatic tunnel log-level writes are
disabled. Readiness warnings such as OAuth discovery failures remain visible;
being listed in the dashboard does not mean a service is ready or tool-tested.
No MCP server, tunnel, authentication policy or retired listener is changed by
updating this inventory. Existing capture routes are retained for compatibility.

## Terminal availability and capture storage

First fixed source: [`capture-lock-fix-2026-11-09`](https://github.com/virat-kumar/mcp-observability-dashboard/tree/capture-lock-fix-2026-11-09)
(`580f448f380956cf1d1e47c0cabbdc7ff4f9ddd1`). Before upgrading or rebuilding,
follow [UPGRADE.md](UPGRADE.md) to preserve this remedy and test the new image.

Capture storage must not block MCP traffic. The standalone gateway bounds SQLite
write contention to 50 ms and preserves upstream responses if capture persistence
fails. Capture-offset ingestion commits even when no new records are found;
collector failures roll back. Command lookup indexes and per-pass candidate
caching keep historical capture backlogs from repeatedly scanning all requests.

These safeguards were deployed locally on 2026-11-09 as image
`sha256:1957854e2052d1cdb9fee4a078ad4df47e17d9ddebc6368bd7fc5ce52df7b950`.
Gateway and inspector retain `unless-stopped` restart policies, with boot handled
by `mcp-observatory-v2.service`. Original terminal sessions and the OAuth tunnel
do not need restarting to recover capture storage.

Before deployment, run the current tests against the candidate image, including
the locked-database responsiveness, empty-ingestion transaction and historical
backlog query regressions in `tests/test_terminal_monitor_overlay.py`. Use the
current candidate image ID and mount the current tests directory; do not use old
smoke scripts or historical screenshots as release evidence. After deployment,
compare running image IDs and source checksums, then verify live localhost
gateway initialization, tool discovery, a disposable PTY command with exit zero,
monitor resource retrieval and session cleanup. Confirm unauthenticated OAuth
gateway access still returns 401. Health endpoints alone do not prove execution.

## Read-only ChatGPT Terminal monitor height (gateway-only override)

The existing Terminal MCP server emits its embedded ChatGPT monitor HTML from a
compiled Go constant. The capture gateway may **optionally** append only the
viewport-height CSS from `deploy/terminal-monitor-viewport.css` to resources
`ui://terminal-mcp/monitor-v1.html`, `v2.html`, and `v3.html`. The rest of MCP
(including terminal command execution, sessions, Follow/Refresh, HTTP routes,
authentication and captures) remains proxied unchanged. It never restarts the
long-lived `terminal-mcp.service` or `openai-tunnel-client.service`.

Activation: copy the reviewed CSS into the existing gateway volume at
`grafana-v2/data/inspector/terminal-monitor-viewport.css` and recreate **only**
the independent `mcp-observatory-v2-gateway` container with this release. The
optional env `MCP_OBS_TERMINAL_MONITOR_CSS_FILE` points to the copy inside the
existing `/data` mount. Source: `app/capture.py`, with isolated tests in
`tests/test_terminal_monitor_overlay.py`. A missing/invalid stylesheet is a
no-op; the proxy refuses external CSS imports, HTML and oversized stylesheets.
The CSS is read afresh on each matching request. **Rollback without restart:**
rename that CSS file to `.inactive`, then reread the monitor resource. **Full
rollback:** restore the preserved previous gateway Docker image, without
changing the original Terminal MCP service. Do not commit live SQLite data or
credentials; only `deploy/terminal-monitor-viewport.css` is source-controlled.

## Disk budget: 3 GiB active data, best-effort management

The dedicated cleanup timer `mcp-observatory-v2-storage.timer` runs every five
minutes, starting two minutes after boot. It counts **both** `grafana-v2/data/`
and the active `data/captures/` raw capture log directory, then:

- Removes snapshots older than **48 hours**, telemetry events and captures older
  than **14 days**, and requests older than **30 days**.
- Rotates a raw Playwright/Computer-Use `.jsonl` capture file once it exceeds
  **32 MiB**, but **only after the inspector confirms all bytes were ingested**.
  It truncates in place to preserve writer file descriptors and MCP connectivity.
- Starts size-pressure cleanup at **2.2 GiB** instead of waiting until 3 GiB;
  retains a minimum set of recent records and reports inability to meet budget.
- Prometheus additionally caps its TSDB via `--storage.tsdb.retention.size=256MB`
  and time retention at seven days. Grafana uses console-only logs.

**Important:** This is a preventive **operating budget, not a filesystem hard
quota**. Because the root ext4 volume lacks a dedicated project quota, growth
can temporarily exceed 3 GiB during unusually large bursts or database WAL
activity. Retention reclaims logical SQLite pages for reuse. A manual database
compaction may be needed to physically shrink an already-overgrown file.
Docker image layers and Docker's own logs outside the data folders are not part
of this 3 GiB data budget. The controller exits nonzero and reports
`OVER_BUDGET` when it cannot meet the limit. Never fill or unmount storage used
by the capture gateway merely to force a hard quota.

Monitor: `systemctl list-timers mcp-observatory-v2-storage.timer` and
`journalctl -u mcp-observatory-v2-storage.service --no-pager -n 20`.
Manual check: `python3 grafana-v2/scripts/enforce_storage_budget.py`.

## No legacy archive

The obsolete v1 SQLite database and v1 Docker container have been removed by
request, and the legacy DB is no longer mounted in the inspector. Old one-second
snapshots from v1 are **not retained**. The migrated v2 request and telemetry
history and new health snapshots remain available, subject to retention.

**Keep** `data/captures/` at its original absolute path: existing MCP wrappers
continue to append to those files. It is not a v1 database or an archive.

## Testing

- `python3 grafana-v2/scripts/generate_prometheus_config.py --check` — detect drift between inventory and scrape targets.
- `BASE_URL=http://127.0.0.1:29120 node grafana-v2/tests/inventory_ui.cjs` — verify current inventory, combined retired versions, health-only labels and chart layout against an isolated inspector.
- Run `python3 -m unittest discover -s tests -p 'test_*.py' -v` inside the candidate image with current tests mounted, as described in `UPGRADE.md`.
- `node grafana-v2/tests/full_ui_regression.cjs` — headless Chrome Playwright
  regression suite for all original UI controls, request input/output, time
  filtering, telemetry, health, raw metrics, auto-refresh, and Grafana.
- `python3 grafana-v2/tests/storage_budget_test.py` — isolated retention and
  capture-rotation tests; never touch production data.
- `bash grafana-v2/tests/original-smoke.sh https://desktop-ubuntu.tailac2e85.ts.net:8470` — full live smoke check.

No reboot was performed to test startup, because rebooting would restart MCP
servers contrary to the owner's requirement. Boot is managed by
`mcp-observatory-v2.service` and the new storage timer, both enabled in systemd.
