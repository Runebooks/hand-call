#!/usr/bin/env bash
# Start all three A2A specialist agents locally (README architecture, no Temporal).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

ENV_FILE="${ENV_FILE:-${ROOT}/.env.local}"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

export A2A_HOST=127.0.0.1
export K8S_MUTATIONS_ENABLED="${K8S_MUTATIONS_ENABLED:-true}"
mkdir -p "${ROOT}/.local-logs"

start_agent() {
  local module=$1
  local port=$2
  local log="${ROOT}/.local-logs/${module}.log"
  if lsof -ti ":${port}" >/dev/null 2>&1; then
    if [[ "${RESTART_AGENTS:-1}" == "1" ]]; then
      echo "Restarting ${module} on :${port}" >&2
      lsof -ti ":${port}" | xargs kill 2>/dev/null || true
      sleep 1
    else
      echo "Port ${port} in use — skip ${module} (set RESTART_AGENTS=1 to restart)" >&2
      return
    fi
  fi
  A2A_PORT="${port}" .venv/bin/python -m "${module}" >>"${log}" 2>&1 &
  echo "Started ${module} on :${port} (log: ${log})"
}

if [[ -n "${OPENAI_API_KEY:-}" ]]; then
  echo "LLM: enabled (model=${OPENAI_MODEL:-gpt-5.5})" >&2
else
  echo "LLM: disabled — add OPENAI_API_KEY to ${ENV_FILE} for model routing" >&2
fi

start_agent agents.prometheus.server 8080
start_agent agents.rds.server 8081
start_agent agents.kubernetes.server 8082

export AGENT_URLS="http://127.0.0.1:8080,http://127.0.0.1:8081,http://127.0.0.1:8082"
echo ""
echo "AGENT_URLS=${AGENT_URLS}"
echo "Run Slack bot: ./scripts/run-slack-bot-local.sh"
