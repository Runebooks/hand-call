#!/usr/bin/env bash
# Create/update K8s secret for Slack bot (reads .env.local).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-a2a-ops}"
ENV_FILE="${ENV_FILE:-${ROOT}/.env.local}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Create ${ENV_FILE} with SLACK_BOT_TOKEN and SLACK_APP_TOKEN" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

if [[ -z "${SLACK_BOT_TOKEN:-}" ]]; then
  echo "SLACK_BOT_TOKEN is empty in ${ENV_FILE}" >&2
  echo "Get it from Slack app → OAuth & Permissions → Install to Workspace" >&2
  exit 1
fi

kubectl create secret generic slack-bot-secrets \
  --namespace="${NAMESPACE}" \
  --from-literal=SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN}" \
  --from-literal=SLACK_APP_TOKEN="${SLACK_APP_TOKEN:-}" \
  --from-literal=SLACK_SIGNING_SECRET="${SLACK_SIGNING_SECRET:-}" \
  --from-literal=SLACK_ALERT_CHANNEL_IDS="${SLACK_ALERT_CHANNEL_IDS:-}" \
  --from-literal=SLACK_TRIGGER_MODE="${SLACK_TRIGGER_MODE:-mention}" \
  --from-literal=K8S_ALERT_NAMES="${K8S_ALERT_NAMES:-KubePodCrashLooping}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Secret slack-bot-secrets updated in namespace ${NAMESPACE}"
