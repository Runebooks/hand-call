#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8082}"

echo "==> Health"
curl -sf "${BASE_URL}/health" | python3 -m json.tool
echo ""

echo ""
echo "==> Agent Card"
curl -sf "${BASE_URL}/.well-known/agent.json" | python3 -m json.tool | head -20

echo ""
echo "==> Task: list problem pods"
curl -sf -X POST "${BASE_URL}/" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "test-1",
    "method": "tasks/send",
    "params": {
      "message": {
        "role": "user",
        "parts": [{"type": "text", "text": "Show pods that are restarting or in CrashLoopBackOff"}]
      }
    }
  }' | python3 -c "
import json, sys
r = json.load(sys.stdin)
task = r.get('result', {})
arts = task.get('artifacts', [])
for a in arts:
    for p in a.get('parts', []):
        if p.get('type') == 'text':
            print(p.get('text', '')[:2000])
"
