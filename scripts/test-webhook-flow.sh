#!/usr/bin/env bash
# End-to-end test WITHOUT Slack @mention events:
#   1. Post sample alert to Slack (incoming webhook)
#   2. Run investigation locally (kubernetes-agent)
#   3. Post result back to Slack (incoming webhook)
#
# You still need SLACK_INCOMING_WEBHOOK_URL. For @mention flow use Socket/HTTP bot.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> 1. Post sample alert to Slack"
"${ROOT}/scripts/post-slack-webhook.sh" --sample-alert

echo ""
echo "==> 2. Investigate via kubernetes-agent"
RESULT="$("${ROOT}/scripts/simulate-slack-alert.sh" 2>/dev/null | sed '/^--- task state/d' | head -80)"

echo ""
echo "==> 3. Post investigation result to Slack"
"${ROOT}/scripts/post-slack-webhook.sh" "**NOC handover bot (test)** — investigation result:

${RESULT}"

echo ""
echo "Done. For interactive @mention in thread, use Socket Mode or HTTP bot + Event Subscriptions."
