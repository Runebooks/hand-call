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

API_KEY="${OPENAI_API_KEY_SK:-${OPENAI_API_KEY:-}}"
if [[ -z "${API_KEY}" ]]; then
  echo "Set OPENAI_API_KEY (Cloudverse JWT) or OPENAI_API_KEY_SK=sk-... in ${ENV_FILE}" >&2
  exit 1
fi

LLM_ENABLED_VALUE="${LLM_ENABLED:-true}"
AUTH_MODE="${OPENAI_AUTH_MODE:-auto}"
CLOUDVERSE_URL="${CLOUDVERSE_BASE_URL:-}"
BASE_URL_VALUE="${OPENAI_BASE_URL:-}"

if [[ "${API_KEY}" == eyJ* ]]; then
  AUTH_MODE="cloudverse"
  if [[ -n "${CLOUDVERSE_URL}" ]]; then
    BASE_URL_VALUE="${CLOUDVERSE_URL}"
  fi
  if [[ -z "${BASE_URL_VALUE}" ]]; then
    BASE_URL_VALUE="https://api.openai.com/v1"
  fi
  if [[ "${BASE_URL_VALUE}" == *api.openai.com* ]]; then
    if [[ "${LLM_ENABLED_VALUE}" == "true" ]]; then
      echo "ERROR: Cloudverse JWT needs CLOUDVERSE_BASE_URL (not api.openai.com)." >&2
      echo "  Set CLOUDVERSE_BASE_URL=https://<host>/v1 then ./scripts/enable-cloudverse-llm.sh" >&2
      exit 1
    fi
    LLM_ENABLED_VALUE="false"
    echo "NOTE: Cloudverse JWT without CLOUDVERSE_BASE_URL — LLM_ENABLED=false." >&2
  fi
elif [[ "${LLM_ENABLED_VALUE}" == "true" ]] && [[ "${API_KEY}" != sk-* ]]; then
  echo "ERROR: LLM_ENABLED=true requires sk-... or Cloudverse JWT (eyJ...)" >&2
  exit 1
else
  AUTH_MODE="${AUTH_MODE:-openai}"
  BASE_URL_VALUE="${BASE_URL_VALUE:-https://api.openai.com/v1}"
fi

kubectl create secret generic kubernetes-agent-llm \
  --namespace="${NAMESPACE}" \
  --from-literal=OPENAI_API_KEY="${API_KEY}" \
  --from-literal=OPENAI_MODEL="${OPENAI_MODEL:-gpt-5.5}" \
  --from-literal=OPENAI_REASONING_EFFORT="${OPENAI_REASONING_EFFORT:-none}" \
  --from-literal=OPENAI_BASE_URL="${BASE_URL_VALUE}" \
  --from-literal=CLOUDVERSE_BASE_URL="${CLOUDVERSE_URL}" \
  --from-literal=OPENAI_AUTH_MODE="${AUTH_MODE}" \
  --from-literal=LLM_ENABLED="${LLM_ENABLED_VALUE}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Secret kubernetes-agent-llm updated in namespace ${NAMESPACE} (LLM_ENABLED=${LLM_ENABLED_VALUE})"
