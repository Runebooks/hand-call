#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SAMPLE="${ROOT}/master/samples/kube_pod_crashloop_slack.txt"

python3 <<PY
from master.alert_parser import parse_alert_message, build_investigation_query, format_slack_reply

with open("${SAMPLE}") as f:
    text = f.read()

alert = parse_alert_message(text)
assert alert, "Failed to parse sample alert"
print("alertname:", alert.alertname)
print("namespace:", alert.namespace)
print("pod_hint:", alert.pod_hint)
print()
print("=== Query ===")
print(build_investigation_query(alert))
PY
