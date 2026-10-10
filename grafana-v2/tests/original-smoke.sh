#!/usr/bin/env bash
set -eo pipefail
BASE="$1"
if [ -z "$BASE" ]; then BASE="http://127.0.0.1:9111"; fi
TMPDIR_LOCAL="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_LOCAL"' EXIT
echo "Checking $BASE"
curl -fsS "$BASE/healthz" | python3 -m json.tool
curl -fsS "$BASE/api/config" -o "$TMPDIR_LOCAL/config.json"
curl -fsS "$BASE/api/summary?mcp=all" -o "$TMPDIR_LOCAL/summary.json"
curl -fsS "$BASE/api/requests?mcp=all&sort=slowest&limit=5" -o "$TMPDIR_LOCAL/requests.json"
curl -fsS "$BASE/api/health" -o "$TMPDIR_LOCAL/health.json"
curl -fsS "$BASE/api/telemetry?mcp=all&limit=5" -o "$TMPDIR_LOCAL/telemetry.json"
curl -fsS "$BASE/api/raw/metrics/terminal" -o "$TMPDIR_LOCAL/metrics.txt"
curl -fsS "$BASE/" -o "$TMPDIR_LOCAL/index.html"
grep -q 'commands_poll' "$TMPDIR_LOCAL/metrics.txt"
grep -q 'MCP Observatory' "$TMPDIR_LOCAL/index.html"
python3 - "$TMPDIR_LOCAL" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1])
cfg=json.loads((p/'config.json').read_text())
health=json.loads((p/'health.json').read_text())
summary=json.loads((p/'summary.json').read_text())
assert cfg['tunnels'], 'no tunnels configured'
assert len(health['rows']) == len(cfg['tunnels']), 'health row count mismatch'
assert 'total_requests' in summary and 'p95_ms' in summary, 'summary incomplete'
print(f"Validated {len(cfg['tunnels'])} tunnels; requests={summary['total_requests']}")
PY
echo "Smoke test passed."
