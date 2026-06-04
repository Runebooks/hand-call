#!/usr/bin/env bash
# Enable LLM with Cloudverse JWT + model via OpenAI-compatible gateway.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-a2a-ops}"
ENV_FILE="${ENV_FILE:-${ROOT}/.env.local}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

API_KEY="${OPENAI_API_KEY:-}"
BASE_URL="${CLOUDVERSE_BASE_URL:-${OPENAI_BASE_URL:-}}"
MODEL="${OPENAI_MODEL:-gpt-5.5}"

if [[ -z "${API_KEY}" ]]; then
  echo "Set OPENAI_API_KEY to your Cloudverse JWT in ${ENV_FILE}" >&2
  exit 1
fi
if [[ "${API_KEY}" != eyJ* ]]; then
  echo "Expected Cloudverse JWT (eyJ...) in OPENAI_API_KEY" >&2
  exit 1
fi
if [[ -z "${BASE_URL}" ]]; then
  echo "Set CLOUDVERSE_BASE_URL=https://<your-cloudverse-host>/v1 in ${ENV_FILE}" >&2
  echo "  (from Cloudverse playground / internal wiki — NOT api.openai.com)" >&2
  exit 1
fi
if [[ "${BASE_URL}" == *api.openai.com* ]]; then
  echo "CLOUDVERSE_BASE_URL must not be api.openai.com for JWT auth" >&2
  exit 1
fi

echo "==> Probe ${BASE_URL}/chat/completions (model=${MODEL})"
export API_KEY BASE_URL MODEL
if ! python3 - <<'PY'
import os, sys
try:
    import httpx
except ImportError:
    print("httpx not installed locally; skipping probe (cluster pod will verify)")
    sys.exit(0)
key = os.environ["API_KEY"]
base = os.environ["BASE_URL"].rstrip("/")
model = os.environ["MODEL"]
r = httpx.post(
    base + "/chat/completions",
    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    json={"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
    timeout=30,
)
print("status", r.status_code)
if r.status_code >= 400:
    print(r.text[:400])
    if os.environ.get("CLOUDVERSE_SKIP_PROBE", "").lower() in ("1", "true", "yes"):
        print("CLOUDVERSE_SKIP_PROBE set — continuing deploy")
        sys.exit(0)
    sys.exit(1)
print("Cloudverse LLM OK")
PY
then
  echo "WARN: probe failed; set exact model from Cloudverse playground or CLOUDVERSE_SKIP_PROBE=1" >&2
  if [[ "${CLOUDVERSE_SKIP_PROBE:-}" != "1" && "${CLOUDVERSE_SKIP_PROBE:-}" != "true" ]]; then
    exit 1
  fi
fi

echo "==> kubernetes-agent-llm secret"
kubectl create secret generic kubernetes-agent-llm \
  --namespace="${NAMESPACE}" \
  --from-literal=OPENAI_API_KEY="${API_KEY}" \
  --from-literal=OPENAI_MODEL="${MODEL}" \
  --from-literal=OPENAI_REASONING_EFFORT="${OPENAI_REASONING_EFFORT:-none}" \
  --from-literal=OPENAI_BASE_URL="${BASE_URL}" \
  --from-literal=CLOUDVERSE_BASE_URL="${BASE_URL}" \
  --from-literal=OPENAI_AUTH_MODE="cloudverse" \
  --from-literal=LLM_ENABLED="true" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl set env "deployment/kubernetes-agent" "deployment/master-agent" -n "${NAMESPACE}" \
  LLM_ENABLED=true "OPENAI_MODEL=${MODEL}" "OPENAI_AUTH_MODE=cloudverse" \
  "OPENAI_BASE_URL=${BASE_URL}" "CLOUDVERSE_BASE_URL=${BASE_URL}" --overwrite

kubectl rollout restart "deployment/kubernetes-agent" "deployment/master-agent" -n "${NAMESPACE}"
kubectl rollout status "deployment/kubernetes-agent" -n "${NAMESPACE}" --timeout=120s

echo "Done. LLM enabled with Cloudverse JWT (model=${MODEL})."
