#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
URL="${WEBHOOK_URL:-http://127.0.0.1:8090/investigate}"
SAMPLE="${ROOT}/master/samples/investigate_request.json"

curl -sf -X POST "${URL}" \
  -H "Content-Type: application/json" \
  ${WEBHOOK_API_KEY:+-H "X-API-Key: ${WEBHOOK_API_KEY}"} \
  -d @"${SAMPLE}" | python3 -m json.tool
