#!/usr/bin/env bash
# Ask the kubernetes A2A agent a question from the CLI.
#
# Usage:
#   ./scripts/ask-agent.sh "Show pods in CrashLoopBackOff in namespace a2a-ops"
#
# Modes (auto):
#   1. BASE_URL (default http://127.0.0.1:8082) if port-forward is running
#   2. In-cluster via kubectl exec (no port-forward needed)
#
# Force in-cluster only:
#   USE_INCLUSTER=1 ./scripts/ask-agent.sh "your question"
set -euo pipefail

QUERY="${1:-}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8082}"
NAMESPACE="${NAMESPACE:-a2a-ops}"
DEPLOYMENT="${DEPLOYMENT:-kubernetes-agent}"
USE_INCLUSTER="${USE_INCLUSTER:-}"

if [[ -z "${QUERY}" ]]; then
  echo "Usage: $0 \"your question about the cluster\"" >&2
  exit 1
fi

QUERY_JSON="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "${QUERY}")"
QUERY_B64="$(printf '%s' "${QUERY_JSON}" | base64 | tr -d '\n')"

print_response() {
  export RESPONSE
  python3 <<'PY'
import json, os, sys

raw = os.environ.get("RESPONSE", "")
if not raw.strip():
    print("Empty response from agent.", file=sys.stderr)
    sys.exit(1)

r = json.loads(raw)
if r.get("error"):
    print("Error:", r["error"], file=sys.stderr)
    sys.exit(1)

task = r.get("result", {})
for art in task.get("artifacts", []):
    for part in art.get("parts", []):
        if part.get("type") == "text":
            print(part.get("text", ""))

state = task.get("status", {}).get("state")
if state:
    print(f"\n--- task state: {state} ---", file=sys.stderr)
PY
}

ask_via_curl() {
  local url="$1"
  curl -sf --connect-timeout 60 -X POST "${url}/" \
    -H "Content-Type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":\"cli\",\"method\":\"tasks/send\",\"params\":{\"message\":{\"role\":\"user\",\"parts\":[{\"type\":\"text\",\"text\":${QUERY_JSON}}]}}}"
}

ask_via_kubectl() {
  kubectl exec -n "${NAMESPACE}" "deploy/${DEPLOYMENT}" -- \
    python3 -c "
import base64, json, urllib.request

query_json = base64.b64decode('${QUERY_B64}').decode()
query_text = json.loads(query_json)
body = {
    'jsonrpc': '2.0',
    'id': 'cli',
    'method': 'tasks/send',
    'params': {
        'message': {
            'role': 'user',
            'parts': [{'type': 'text', 'text': query_text}],
        }
    },
}
data = json.dumps(body).encode()
req = urllib.request.Request(
    'http://127.0.0.1:8082/',
    data=data,
    headers={'Content-Type': 'application/json'},
    method='POST',
)
print(urllib.request.urlopen(req, timeout=120).read().decode())
"
}

if [[ "${USE_INCLUSTER}" == "1" ]]; then
  echo "--- via in-cluster (kubectl exec) ---" >&2
  RESPONSE="$(ask_via_kubectl)"
  print_response
  exit 0
fi

if curl -sf --connect-timeout 3 "${BASE_URL}/health" >/dev/null 2>&1; then
  echo "--- via port-forward (${BASE_URL}) ---" >&2
  RESPONSE="$(ask_via_curl "${BASE_URL}")"
  print_response
  exit 0
fi

if kubectl get deployment "${DEPLOYMENT}" -n "${NAMESPACE}" >/dev/null 2>&1; then
  echo "--- port-forward not running; using in-cluster kubectl exec ---" >&2
  RESPONSE="$(ask_via_kubectl)"
  print_response
  exit 0
fi

echo "Cannot reach agent at ${BASE_URL} and deployment ${DEPLOYMENT} not found in ${NAMESPACE}." >&2
echo "" >&2
echo "Option A — port-forward (separate terminal):" >&2
echo "  ./scripts/port-forward-agent.sh" >&2
echo "" >&2
echo "Option B — in-cluster only:" >&2
echo "  USE_INCLUSTER=1 $0 \"${QUERY}\"" >&2
exit 1
