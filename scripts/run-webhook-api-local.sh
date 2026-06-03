#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT}/.env.local}"

[[ -f "${ENV_FILE}" ]] && { set -a; source "${ENV_FILE}"; set +a; }

export KUBERNETES_AGENT_URL="${KUBERNETES_AGENT_URL:-http://127.0.0.1:8082}"
export WEBHOOK_HOST="${WEBHOOK_HOST:-0.0.0.0}"
export WEBHOOK_PORT="${WEBHOOK_PORT:-8090}"

cd "${ROOT}"
.venv/bin/pip install -q fastapi uvicorn 2>/dev/null || true

echo "Webhook API: http://127.0.0.1:${WEBHOOK_PORT}/investigate" >&2
echo "Health:      http://127.0.0.1:${WEBHOOK_PORT}/health" >&2
echo "Agent:       ${KUBERNETES_AGENT_URL}" >&2

exec .venv/bin/python -m master.webhook_api
