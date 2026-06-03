#!/usr/bin/env bash
# Post a message to Slack via Incoming Webhook (one-way: you → Slack).
# Cannot receive @mentions — use for posting test alerts or investigation output.
#
# Usage:
#   ./scripts/post-slack-webhook.sh "hello"
#   ./scripts/post-slack-webhook.sh --sample-alert
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT}/.env.local}"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

WEBHOOK_URL="${SLACK_INCOMING_WEBHOOK_URL:-}"

if [[ -z "${WEBHOOK_URL}" ]]; then
  echo "Set SLACK_INCOMING_WEBHOOK_URL in .env.local" >&2
  exit 1
fi

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

if [[ "${1:-}" == "--sample-alert" ]]; then
  cp "${ROOT}/master/samples/kube_pod_crashloop_slack.txt" "$TMP"
elif [[ -n "${1:-}" ]]; then
  printf '%s' "$*" >"$TMP"
else
  echo "Usage: $0 \"message\" | $0 --sample-alert" >&2
  exit 1
fi

export WEBHOOK_URL
export TMP
python3 <<'PY'
import json, os, urllib.request

with open(os.environ["TMP"]) as f:
    text = f.read()
payload = json.dumps({"text": text}).encode()
req = urllib.request.Request(
    os.environ["WEBHOOK_URL"],
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
urllib.request.urlopen(req, timeout=30)
print("Posted to Slack via incoming webhook.")
PY
