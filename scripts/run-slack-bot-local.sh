#!/usr/bin/env bash
# Run Slack bot locally (Socket Mode). Requires .env.local with tokens.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT}/.env.local}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  echo "Copy from .env.example: cp .env.example .env.local" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

missing=()
[[ -z "${SLACK_BOT_TOKEN:-}" ]] && missing+=("SLACK_BOT_TOKEN")
[[ -z "${SLACK_APP_TOKEN:-}" ]] && missing+=("SLACK_APP_TOKEN")

if [[ ${#missing[@]} -gt 0 ]]; then
  echo "Missing in ${ENV_FILE}:" >&2
  for v in "${missing[@]}"; do
    echo "  - ${v}" >&2
  done
  echo "" >&2
  echo "These are NOT on the Basic Information page. Get them here:" >&2
  echo "  1. https://api.slack.com/apps → your app (NOC handover bot)" >&2
  echo "  2. Socket Mode → ON → generate App-Level Token (xapp-...) → SLACK_APP_TOKEN" >&2
  echo "  3. OAuth & Permissions → Bot Token Scopes (PRIVATE channel):" >&2
  echo "       app_mentions:read, chat:write, groups:history, groups:read" >&2
  echo "     Event Subscriptions → bot events: app_mention, message.groups" >&2
  echo "  4. Install to Workspace → copy Bot User OAuth Token (xoxb-...) → SLACK_BOT_TOKEN" >&2
  echo "" >&2
  echo "You already have SLACK_SIGNING_SECRET set. Example .env.local lines:" >&2
  echo '  SLACK_BOT_TOKEN=xoxb-...' >&2
  echo '  SLACK_APP_TOKEN=xapp-...' >&2
  exit 1
fi

# README: master discovers all agents via AGENT_URLS
export AGENT_URLS="${AGENT_URLS:-http://127.0.0.1:8080,http://127.0.0.1:8081,http://127.0.0.1:8082}"
export KUBERNETES_AGENT_URL="http://127.0.0.1:8082"
# README: Slack → master-agent → specialists. Run ./scripts/run-master-agent-local.sh first.
export MASTER_AGENT_URL="${MASTER_AGENT_URL:-http://127.0.0.1:8095}"

if ! curl -sf --connect-timeout 2 "${MASTER_AGENT_URL}/health" >/dev/null 2>&1; then
  echo "WARN: master-agent not reachable at ${MASTER_AGENT_URL}" >&2
  echo "  Run: ./scripts/run-master-agent-local.sh" >&2
  echo "  Or full stack: ./scripts/run-full-stack-local.sh" >&2
fi

if ! curl -sf --connect-timeout 2 "${KUBERNETES_AGENT_URL}/health" >/dev/null 2>&1; then
  echo "WARN: kubernetes-agent not reachable at ${KUBERNETES_AGENT_URL}" >&2
  echo "  Run: ./scripts/run-all-agents-local.sh" >&2
  echo "  Or port-forward: kubectl port-forward -n a2a-ops svc/kubernetes-agent 8082:8082" >&2
fi

cd "${ROOT}"
.venv/bin/pip install -q slack-bolt slack-sdk 2>/dev/null || pip install -q slack-bolt slack-sdk
echo "Starting Slack bot (Socket Mode)..." >&2
exec .venv/bin/python -m master.slack_bot
