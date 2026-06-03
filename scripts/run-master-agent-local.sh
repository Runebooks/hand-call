#!/usr/bin/env bash
# Master agent router (README architecture). Specialists must be reachable via AGENT_URLS.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT}/.env.local}"
[[ -f "${ENV_FILE}" ]] && { set -a; source "${ENV_FILE}"; set +a; }

export AGENT_URLS="${AGENT_URLS:-http://127.0.0.1:8080,http://127.0.0.1:8081,http://127.0.0.1:8082}"
export A2A_PORT="${A2A_PORT:-${MASTER_PORT:-8095}}"
export A2A_HOST="${A2A_HOST:-${MASTER_HOST:-0.0.0.0}}"

cd "${ROOT}"
echo "Master agent (A2A) on http://127.0.0.1:${A2A_PORT}" >&2
echo "Agent Card: http://127.0.0.1:${A2A_PORT}/.well-known/agent.json" >&2
echo "AGENT_URLS=${AGENT_URLS}" >&2
exec .venv/bin/python -m master.server
