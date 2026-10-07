#!/usr/bin/env bash
set -eo pipefail
HTTPS_PORT="$1"
APP_PORT="$2"
if [ -z "$HTTPS_PORT" ]; then HTTPS_PORT=8470; fi
if [ -z "$APP_PORT" ]; then APP_PORT=9111; fi
tailscale serve --bg --https="$HTTPS_PORT" "http://127.0.0.1:$APP_PORT"
tailscale serve status
