#!/usr/bin/env bash
# Start specialists + master-agent + slack bot for local demo.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

echo "==> Specialist agents (8080-8082)"
"${ROOT}/scripts/run-all-agents-local.sh"

echo ""
echo "==> Master agent (8095)"
if lsof -ti :8095 >/dev/null 2>&1; then
  lsof -ti :8095 | xargs kill 2>/dev/null || true
  sleep 1
fi
mkdir -p "${ROOT}/.local-logs"
A2A_HOST=127.0.0.1 AGENT_URLS="http://127.0.0.1:8080,http://127.0.0.1:8081,http://127.0.0.1:8082" \
  .venv/bin/python -m master.server >>"${ROOT}/.local-logs/master.server.log" 2>&1 &
sleep 2

for url in http://127.0.0.1:8082/health http://127.0.0.1:8095/health; do
  if curl -sf --connect-timeout 3 "${url}" >/dev/null; then
    echo "OK ${url}"
  else
    echo "FAIL ${url} — check .local-logs/" >&2
    exit 1
  fi
done

echo ""
echo "==> Slack bot (restart)"
exec "${ROOT}/scripts/restart-slack-bot.sh"
