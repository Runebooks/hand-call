#!/usr/bin/env bash
# Run Slack bot in HTTP mode (Slack sends events to your machine via ngrok).
# Use this instead of Socket Mode if you prefer a "webhook URL" setup.
#
# Setup:
#   1. ./scripts/run-slack-bot-http.sh          (listens on :3000)
#   2. ngrok http 3000                          (another terminal)
#   3. Slack app → Event Subscriptions → ON
#      Request URL: https://<ngrok-id>.ngrok-free.app/slack/events
#   4. Subscribe to bot events: app_mention, message.channels
#   5. Reinstall app if prompted
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT}/.env.local}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

if [[ -z "${SLACK_BOT_TOKEN:-}" ]] || [[ -z "${SLACK_SIGNING_SECRET:-}" ]]; then
  echo "Need SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET in ${ENV_FILE}" >&2
  exit 1
fi

unset SLACK_APP_TOKEN
export KUBERNETES_AGENT_URL="http://127.0.0.1:8082"
export SLACK_PORT="${SLACK_PORT:-3000}"

if ! curl -sf --connect-timeout 2 "${KUBERNETES_AGENT_URL}/health" >/dev/null 2>&1; then
  echo "WARN: Start port-forward first:" >&2
  echo "  kubectl port-forward -n a2a-ops svc/kubernetes-agent 8082:8082" >&2
fi

echo "=== HTTP mode (webhook from Slack → you) ===" >&2
echo "1. Keep this running (port ${SLACK_PORT})" >&2
echo "2. In another terminal: ngrok http ${SLACK_PORT}" >&2
echo "3. Event Subscriptions → Request URL:" >&2
echo "     https://<your-ngrok-host>/slack/events" >&2
echo "4. Bot events: app_mention, message.channels" >&2
echo "" >&2

cd "${ROOT}"
.venv/bin/pip install -q slack-bolt slack-sdk 2>/dev/null || true
exec .venv/bin/python -m master.slack_bot
