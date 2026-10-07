#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/virat/Projects/mcp-observability-dashboard"
APP_URL="http://127.0.0.1:9111"
TAILSCALE_HTTPS_PORT="8470"

cd "$REPO_DIR"

/usr/bin/docker compose up -d --remove-orphans

for _ in $(seq 1 60); do
  if /usr/bin/curl -fsS --max-time 2 "$APP_URL/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

/usr/bin/curl -fsS --max-time 3 "$APP_URL/healthz" >/dev/null

# Restore the tailnet-only HTTPS endpoint after boot. Tailscale Serve is
# persistent, and this command is intentionally idempotent.
/usr/bin/tailscale serve --bg --https="$TAILSCALE_HTTPS_PORT" "$APP_URL"
