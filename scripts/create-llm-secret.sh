#!/usr/bin/env bash
# Create/update K8s secret for OpenAI API key (reads .env.local, never committed).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-a2a-ops}"
ENV_FILE="${ENV_FILE:-${ROOT}/.env.local}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Create ${ENV_FILE} from .env.example and set OPENAI_API_KEY" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "OPENAI_API_KEY is empty in ${ENV_FILE}" >&2
  exit 1
fi

kubectl create secret generic kubernetes-agent-llm \
  --namespace="${NAMESPACE}" \
  --from-literal=OPENAI_API_KEY="${OPENAI_API_KEY}" \
  --from-literal=OPENAI_MODEL="${OPENAI_MODEL:-gpt-5.5}" \
  --from-literal=OPENAI_REASONING_EFFORT="${OPENAI_REASONING_EFFORT:-none}" \
  --from-literal=OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://api.openai.com/v1}" \
  --from-literal=LLM_ENABLED="${LLM_ENABLED:-true}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Secret kubernetes-agent-llm updated in namespace ${NAMESPACE}"
