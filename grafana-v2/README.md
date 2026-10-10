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
| Prometheus | 127.0.0.1:9191 | Scrapes six existing MCP tunnel metric endpoints |
| Capture gateway | 18900, 18017, 18765, 17874 | Original HTTP listener ports for four MCP tunnels |
| HTTPS | Tailnet-only 8470 | Forwards to inspector, with Grafana at `/grafana/` |

No MCP server or MCP tunnel service source/config is modified. The capture gateway
is an independent process; the dashboard can be restarted without restarting the
MCP forwarding path. FarmToGo's Grafana, databases, and containers are untouched.

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

- `node grafana-v2/tests/full_ui_regression.cjs` — headless Chrome Playwright
  regression suite for all original UI controls, request input/output, time
  filtering, telemetry, health, raw metrics, auto-refresh, and Grafana.
- `python3 grafana-v2/tests/storage_budget_test.py` — isolated retention and
  capture-rotation tests; never touch production data.
- `bash grafana-v2/tests/original-smoke.sh https://desktop-ubuntu.tailac2e85.ts.net:8470` — full live smoke check.

No reboot was performed to test startup, because rebooting would restart MCP
servers contrary to the owner's requirement. Boot is managed by
`mcp-observatory-v2.service` and the new storage timer, both enabled in systemd.
