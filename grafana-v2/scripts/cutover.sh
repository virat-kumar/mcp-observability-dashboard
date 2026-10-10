#!/usr/bin/env bash
# Run from the existing Ubuntu Terminal MCP session. All MCP server/tunnel services
# stay running; only the obsolete dashboard container is stopped.
set -Eeuo pipefail
ROOT=/home/virat/Projects/mcp-observability-dashboard
NEW="$ROOT/grafana-v2"
OLD_UNIT=mcp-observability-dashboard.service
NEW_UNIT=mcp-observatory-v2.service
SUCCESS="$NEW/data/CUTOVER_SUCCESS"
cd "$NEW"
log(){ printf '%s %s\n' "$(date -Is)" "$*"; }
rollback(){
  rc=$?
  trap - ERR
  if [[ -f "$SUCCESS" ]]; then exit "$rc"; fi
  log "CUTOVER FAILED (code $rc); initiating automatic rollback"
  /usr/bin/docker compose --profile capture stop gateway >/dev/null 2>&1 || true
  /usr/bin/docker update --restart=unless-stopped mcp-observability-dashboard >/dev/null 2>&1 || true
  sudo -n systemctl disable "$NEW_UNIT" >/dev/null 2>&1 || true
  sudo -n systemctl enable --now "$OLD_UNIT" >/dev/null 2>&1 || true
  /usr/bin/tailscale serve --bg --yes --https=8470 http://127.0.0.1:9111 >/dev/null 2>&1 || true
  rm -f "$NEW/.env"
  log 'Rollback attempted; verify old route and endpoints'
  exit "$rc"
}
trap rollback ERR
log 'Preflight: stage and legacy are healthy'
curl -fsS --max-time 4 http://127.0.0.1:9111/healthz >/dev/null
curl -fsS --max-time 4 http://127.0.0.1:9120/healthz >/dev/null
curl -fsS --max-time 4 http://127.0.0.1:9120/grafana/api/health >/dev/null
curl -fsS --max-time 4 http://127.0.0.1:28900/api/sessions >/dev/null
/usr/bin/python3 "$NEW/scripts/sync_history.py"
log 'Keeping legacy database and code intact for rollback/audit'
cp -a /etc/systemd/system/$OLD_UNIT "$NEW/data/legacy-systemd-unit.backup"
install -m 600 "$NEW/.env.production.example" "$NEW/.env"
# Validate the production listener map without touching an MCP process.
/usr/bin/docker compose config --quiet
log 'Stopping ONLY legacy dashboard container; existing MCP servers remain alive'
sudo -n systemctl stop "$OLD_UNIT"
log 'Starting independent capture gateway on existing tunnel listener ports'
/usr/bin/docker compose --profile capture up -d --no-deps --force-recreate gateway
for i in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:18900/api/sessions >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS --max-time 3 http://127.0.0.1:18900/api/sessions >/dev/null
for p in 18017 18765 17874; do
  /usr/bin/ss -lnt | grep -q "127.0.0.1:${p} "
done
log 'MCP HTTP proxy listener sockets and terminal endpoint verified'
sudo -n install -m 0644 "$NEW/deploy/systemd/$NEW_UNIT" "/etc/systemd/system/$NEW_UNIT"
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now "$NEW_UNIT"
log 'Switching original tailnet-only port 8470 to replacement UI'
/usr/bin/tailscale serve --bg --yes --https=8470 http://127.0.0.1:9120
curl -fsS --max-time 12 https://desktop-ubuntu.tailac2e85.ts.net:8470/healthz >/dev/null
curl -fsS --max-time 12 https://desktop-ubuntu.tailac2e85.ts.net:8470/grafana/api/health >/dev/null
log 'Disabling old dashboard autostart and old Docker restart policy'
sudo -n systemctl disable "$OLD_UNIT"
/usr/bin/docker update --restart=no mcp-observability-dashboard >/dev/null
# Remove temporary staging Serve route. No other Serve mappings are touched.
/usr/bin/tailscale serve --https=8471 off || true
for s in terminal-mcp.service openai-tunnel-client.service openai-tunnel-browser.service openai-tunnel-computer-use-linux.service openai-tunnel-excel-mcp.service openai-tunnel-personal-mcp-server.service openai-tunnel-background-agent.service; do
  pid="$(systemctl show "$s" -p MainPID --value)"
  baseline="$(awk -v s="$s" '$1==s{print $2}' /tmp/mcp-cutover-process-baseline.txt)"
  if [[ -z "$baseline" || "$pid" != "$baseline" ]]; then
    log "MCP process changed unexpectedly: $s before=$baseline now=$pid"
    false
  fi
done
log 'All seven MCP server/tunnel PIDs unchanged'
curl -fsS --max-time 4 http://127.0.0.1:9120/api/health >/dev/null
/usr/bin/docker compose --profile capture ps
printf 'OK %s\n' "$(date -Is)" > "$SUCCESS"
log 'CUTOVER SUCCESS: v2 online at original Tailscale port 8470; legacy database retained; old autostart disabled'
