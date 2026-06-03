#!/usr/bin/env bash
# Kill stale local Slack bot processes and start the current code.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Stopping old slack bot processes..." >&2
pkill -f "python -m master.slack_bot" 2>/dev/null || true
pkill -f "master.slack_bot" 2>/dev/null || true
sleep 1

if lsof -ti :3000 >/dev/null 2>&1; then
  echo "Port 3000 still in use (HTTP bot?). Kill manually if needed." >&2
fi

exec "${ROOT}/scripts/run-slack-bot-local.sh"
