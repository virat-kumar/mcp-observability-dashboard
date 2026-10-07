# MCP Observatory

A unified analytics, health, request-inspection, and telemetry dashboard for MCP servers connected through the OpenAI tunnel client.

The observability layer does not modify MCP server source code. It consumes the tunnel client's existing metrics/status/health/log surfaces and can optionally route MCP transport through a narrow capture adapter to make tool input/output inspectable.

## What it shows

- Request volume over time
- Average, P50, P95, P99, and maximum end-to-end latency
- Latency by every configured MCP, including MCPs with no calls in the selected range
- Labeled X/Y latency axes and Average/P95 legends
- Clickable MCP latency rows that jump directly to that MCP's Requests view
- Per-MCP request and latency comparisons
- Request table defaulting to newest first, with newest/oldest/slowest/fastest toggles
- Per-request lifecycle with input/enqueue time, MCP response time, OpenAI delivery time, end-to-end duration, reply-to-delivery overhead, status, tool name, and correlation IDs
- Click any request to inspect captured MCP tool input and output
- 15 minute, 1 hour, 6 hour, 24 hour, 7 day, and custom date-time windows
- Live health/readiness, uptime, queue/worker utilization, poll errors, RSS, and network counters
- Searchable lifecycle telemetry
- Newest/oldest telemetry ordering
- Horizontal telemetry scrolling and adjustable 80%-140% table scale
- Clickable telemetry rows with full metadata drill-down
- Raw tunnel Status JSON and Prometheus exposition
- Observer Prometheus metrics

## E2E meaning

E2E means **end-to-end latency**.

In MCP Observatory it is the elapsed time from the tunnel request/enqueue point until the completed response is delivered back to the OpenAI control plane. The Requests tab also shows the narrower MCP reply -> delivery segment separately.

## Safety and privacy model

MCP Observatory does **not** enable the OpenAI tunnel client's unsafe raw HTTP logger.

That raw logger can contain authentication headers and other control-plane information. Instead, request inspection is performed at the MCP transport boundary:

- HTTP MCPs can be routed through the local capture adapter.
- stdio MCPs can be launched through scripts/stdio_capture.py.
- Only tools/call JSON-RPC data is retained for request inspection.
- Control-plane headers, OpenAI tunnel headers, authentication headers, and transport headers are never stored by the capture adapter.
- Common secret fields and inline credentials are redacted before storage, including password/token/secret/API-key/authorization-style values and common token formats.
- Captures are size bounded. Oversized payloads are stored as a truncated preview.

Tool input/output can still contain sensitive application data by its nature. Protect the Observatory database and Tailscale endpoint accordingly.

## Request timing

The tunnel exports cumulative end-to-end latency measurements plus safe lifecycle events. MCP Observatory samples both.

When exactly one tool request completes between samples, the counter/sum delta identifies that request's end-to-end duration and the observer reconstructs the input/enqueue timestamp. These rows are labeled exact-single.

If several calls complete inside one sampling interval, only their aggregate duration is externally observable. Those rows are explicitly labeled as a batch estimate. MCP-response and OpenAI-delivery timestamps remain directly sourced from lifecycle events.

## Architecture

~~~
ChatGPT
   |
OpenAI MCP control plane
   |
OpenAI tunnel client
   |
   +----------------------- transport metrics / lifecycle events
   |
optional capture boundary
  | HTTP adapter or stdio wrapper
  | strips transport/control-plane metadata
  | retains redacted tools/call input + output
   |
Existing MCP server
(no MCP source changes)

                  metrics + events + captures
                              |
                       MCP Observatory
                       FastAPI + SQLite
                              |
                       Unified dashboard
~~~

## Availability design

The underlying MCP server processes are not modified or restarted when deploying the dashboard.

If request-body inspection is enabled by routing a tunnel through the HTTP capture adapter or stdio wrapper, that adapter becomes part of that tunnel's transport path. Deploy it with process supervision and verify tunnel readiness after changes. The supplied Compose service uses restart: unless-stopped.

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

## Default HTTP capture listeners

The app starts localhost-only HTTP capture adapters for these defaults:

| MCP | Capture listener | Default upstream |
| --- | ---: | --- |
| terminal | 127.0.0.1:18900 | 127.0.0.1:8900 |
| excel | 127.0.0.1:18017 | 127.0.0.1:8017 |
| personal | 127.0.0.1:18765 | 127.0.0.1:8765 |
| background | 127.0.0.1:17874 | 127.0.0.1:17873 |

To collect request input/output, point the corresponding tunnel client's MCP URL at the capture listener instead of the upstream port.

For stdio MCPs, launch the existing command through:

~~~
python3 scripts/stdio_capture.py \
  --mcp playwright \
  --capture-file /path/to/observatory-data/captures/playwright.jsonl \
  -- /path/to/original-mcp-command
~~~

The capture file directory should be the same directory mounted as /data/captures in the dashboard container.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| MCP_OBS_DB | /data/observability.db | SQLite database |
| MCP_OBS_POLL_SECONDS | 1.0 | Collection interval |
| MCP_OBS_DEBUG_LOGS | true | Enable safe lifecycle metadata |
| MCP_OBS_TUNNELS_JSON | built-in six-tunnel map | Custom tunnel definitions |
| MCP_OBS_CAPTURE_DIR | /data/captures | stdio capture ingest directory |
| MCP_OBS_MAX_CAPTURE_BYTES | 5242880 | Maximum stored tool request/response capture size |
| MCP_OBS_HTTP_CAPTURE_JSON | built-in HTTP capture map | Custom capture listener/upstream definitions |

High-frequency metric snapshots are retained for 48 hours. Request lifecycle records and telemetry metadata are retained in SQLite.

## Dashboard tabs

### Overview

KPIs, request/latency timeline, latency by all configured MCPs, and a newest-first request list with a sort toggle.

### Requests

Sortable lifecycle table. Click a row for request metadata plus captured input/output when available.

### Health

Live/ready status, uptime, call counters, poll errors, queue/worker utilization, memory, and network counters.

### Telemetry

Search structured lifecycle events by level, message, request ID, or metadata. Sort newest/oldest, scale the table, scroll horizontally, click events for full metadata, and inspect current raw tunnel status or Prometheus exposition.

## API

- GET /healthz
- GET /metrics
- GET /api/config
- GET /api/summary
- GET /api/requests
- GET /api/request/{mcp}/{request_id}
- GET /api/timeseries
- GET /api/health
- GET /api/telemetry
- GET /api/snapshots
- GET /api/raw/status/{mcp}
- GET /api/raw/metrics/{mcp}

Analytics endpoints accept from, to, and mcp where applicable. Times are ISO 8601.

## Historical payload limitation

Input/output payloads can only be displayed for tool calls observed after capture routing is enabled. Historical requests retain their timing and telemetry but cannot have their old tool arguments/results reconstructed.

## License

MIT
