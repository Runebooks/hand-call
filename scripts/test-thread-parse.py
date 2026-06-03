#!/usr/bin/env python3
"""Offline test: thread text extraction + alert parse (no Slack API)."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from master.alert_parser import parse_alert_message
from master.slack_message_text import extract_message_text

SAMPLE = ROOT / "master/samples/kube_pod_crashloop_slack.txt"
body = SAMPLE.read_text()

# Simulate NOC-Automator: alert only in blocks, empty text
msg = {
    "text": "",
    "blocks": [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body.replace("\n", "\n")},
        }
    ],
}
extracted = extract_message_text(msg)
alert = parse_alert_message(extracted)
assert "alertname:" in extracted.lower(), extracted[:200]
assert alert and alert.alertname == "KubePodCrashLooping" and alert.namespace == "a2a-ops"
print("OK blocks-only:", alert.alertname, alert.namespace)

alert2 = parse_alert_message(body)
assert alert2 and alert2.namespace == "a2a-ops"
print("OK plain text:", alert2.alertname)

dense = body.replace("\n", " ")
alert3 = parse_alert_message(dense)
assert alert3 and alert3.namespace == "a2a-ops", alert3
print("OK single-line dense:", alert3.alertname)

from master.alert_parser import infer_alert_from_context

tick = (
    "*Subject:* KubePodCrashLooping\n"
    "`alertname`:KubePodCrashLooping\n"
    "`namespace`:a2a-ops\n"
    "`status`:firing\n"
)
alert_tick = parse_alert_message(tick)
assert alert_tick and alert_tick.namespace == "a2a-ops", alert_tick
print("OK backtick fields:", alert_tick.alertname)

infer = infer_alert_from_context(
    "namespace:a2a-ops status:firing",
    "What is the reason for pod crashloopbackoff?",
)
assert infer and infer.alertname == "KubePodCrashLooping"
print("OK infer from question:", infer.alertname, infer.namespace)
