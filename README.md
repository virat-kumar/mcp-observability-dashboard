# MCP Observatory

A zero-MCP-code-change analytics, health, and telemetry dashboard for MCP servers connected through the OpenAI tunnel client.

MCP Observatory sits beside an existing MCP deployment. It reads the tunnel client's existing /metrics, /api/status, /healthz, /readyz, and safe structured log APIs. It never modifies MCP application source code and does not insert a proxy in the request path.

## What it shows

- Request volume over time
- Average, P50, P95, P99, and maximum end-to-end latency
- Slowest-request ranking
- Per-MCP request and latency comparisons
- Per-request lifecycle with reconstructed input/enqueue time, exact MCP reply time, exact OpenAI delivery time, end-to-end duration, reply-to-delivery overhead, status, error state, and correlation IDs
- Global MCP filtering
- 15 minute, 1 hour, 6 hour, 24 hour, 7 day, and custom date-time windows
- Live health and readiness
- Tunnel uptime
- Queue depth and worker occupancy
- Poll errors
- Process RSS and network counters
- Searchable safe DEBUG lifecycle telemetry
- Raw tunnel status JSON
- Raw Prometheus exposition
- A Prometheus endpoint for the observer itself

## Safety and privacy model

MCP Observatory deliberately does not enable the tunnel client's raw HTTP payload logger.

Safe DEBUG lifecycle events contain metadata such as request IDs, RPC method, status, and timestamps, but not MCP arguments or response bodies. This avoids turning an analytics database into a copy of shell commands, prompts, file contents, credentials, or other sensitive payloads.

## Request timing

The tunnel exports cumulative end-to-end latency measurements and safe DEBUG lifecycle events. MCP Observatory samples both.

When exactly one tool request completes between samples, the counter/sum delta identifies that request's exact end-to-end duration. The observer reconstructs the input/enqueue timestamp from the exact response-delivery timestamp. These rows are labeled exact-single.

If several calls complete inside one sampling interval, only their aggregate duration is externally observable. Those rows are explicitly labeled as a batch estimate rather than presented as exact. MCP reply and OpenAI delivery timestamps remain exact because they come from lifecycle events.

## Architecture

~~~
ChatGPT
   |
OpenAI MCP control plane
   |
OpenAI tunnel client ----------------------+
   |                                       |
Existing MCP server                        | existing observer surfaces
(no code changes)                          | metrics/status/health/logs
                                           v
                                   MCP Observatory
                                   FastAPI + SQLite
                                           |
                                   Unified dashboard
~~~

The observer is not in the MCP request path. If it stops, MCP traffic continues normally.

## Quick start

Requirements:

- Linux
- Docker + Docker Compose
- OpenAI tunnel clients reachable from the observer host

Start:

~~~
docker compose up -d --build
~~~

Local dashboard:

~~~
http://127.0.0.1:9111/
~~~

Run the smoke test:

~~~
./scripts/smoke.sh http://127.0.0.1:9111
~~~

## Tailscale HTTPS

If Tailscale is installed, publish the dashboard to your tailnet without exposing it to the public Internet:

~~~
./scripts/tailscale-serve.sh 8470 9111
~~~

Tailscale prints the tailnet-only HTTPS URL.

## Default tunnel layout

| Key | Name | Tunnel admin port |
| --- | --- | ---: |
| terminal | Ubuntu Terminal | 8080 |
| playwright | Chrome Playwright | 8081 |
| computer | Computer Use Linux | 8082 |
| personal | Personal MCP | 8083 |
| excel | Excel MCP | 8084 |
| background | Background Agent | 8085 |

Override these defaults with MCP_OBS_TUNNELS_JSON. Each entry can provide either a port or an explicit base URL.

Example:

~~~
export MCP_OBS_TUNNELS_JSON='{"my-mcp":{"name":"My MCP","base":"http://127.0.0.1:9099"}}'
docker compose up -d --build
~~~

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| MCP_OBS_DB | /data/observability.db | SQLite database |
| MCP_OBS_POLL_SECONDS | 1.0 | Collection interval |
| MCP_OBS_DEBUG_LOGS | true | Enable safe DEBUG lifecycle metadata |
| MCP_OBS_TUNNELS_JSON | built-in six-tunnel map | Custom tunnel definitions |

High-frequency metric snapshots are retained for 48 hours. Request lifecycle records and event metadata are retained in SQLite.

## Dashboard tabs

### Overview

KPIs, request/latency timeline, latency by MCP, and slowest requests.

### Requests

Sortable request lifecycle table. Sort by newest, oldest, slowest, fastest, or largest reply-to-delivery overhead.

### Health

Live/ready status, uptime, call counters, poll errors, queue/worker utilization, memory, and network counters.

### Telemetry

Search structured lifecycle events by level, message, request ID, or metadata. Inspect current raw tunnel status and Prometheus exposition.

## API

- GET /healthz
- GET /metrics
- GET /api/config
- GET /api/summary
- GET /api/requests
- GET /api/timeseries
- GET /api/health
- GET /api/telemetry
- GET /api/snapshots
- GET /api/raw/status/{mcp}
- GET /api/raw/metrics/{mcp}

Analytics endpoints accept from, to, and mcp where applicable. Times are ISO 8601.

## Limitation

Safe tunnel telemetry identifies tools/call but does not expose individual MCP tool names such as browser_click or terminal_send. Getting tool names without MCP code changes requires observing request payload metadata in a proxy or another source. MCP Observatory intentionally avoids capturing payloads by default.

The dashboard therefore provides detailed per-request timing and operational telemetry without storing MCP arguments or responses.

## License

MIT
