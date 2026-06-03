#!/usr/bin/env bash
# Simulate processing a Slack alert file through the kubernetes agent (no Slack).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SAMPLE="${1:-${ROOT}/master/samples/kube_pod_crashloop_slack.txt}"

QUERY="$(python3 <<PY
from master.alert_parser import parse_alert_message, build_investigation_query
with open("${SAMPLE}") as f:
    alert = parse_alert_message(f.read())
if not alert:
    raise SystemExit("Not a valid alert message")
print(build_investigation_query(alert))
PY
)"

echo "=== Simulated investigation query ==="
echo "${QUERY}"
echo ""
exec "${ROOT}/scripts/ask-agent.sh" "${QUERY}"
