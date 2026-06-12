#!/usr/bin/env bash
# Create freshservice-agent-config secret in the cluster.
# Values are read from .env.local (copy .env.example and fill in).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-a2a-ops}"
ENV_FILE="${ENV_FILE:-${ROOT}/.env.local}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Create ${ENV_FILE} and set the FRESHSERVICE_* variables (see .env.example)" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

if [[ -z "${FRESHSERVICE_API_KEY:-}" ]]; then
  echo "FRESHSERVICE_API_KEY is empty in ${ENV_FILE}" >&2
  echo "Get the key from https://freshworks.freshservice.com → Admin → API Settings." >&2
  exit 1
fi

kubectl create secret generic freshservice-agent-config \
  --namespace="${NAMESPACE}" \
  --from-literal=FRESHSERVICE_DOMAIN="${FRESHSERVICE_DOMAIN:-freshworks.freshservice.com}" \
  --from-literal=FRESHSERVICE_API_KEY="${FRESHSERVICE_API_KEY}" \
  --from-literal=FRESHSERVICE_ANALYTICS_EXPORT_ID="${FRESHSERVICE_ANALYTICS_EXPORT_ID:-}" \
  --from-literal=FRESHSTATUS_ACCOUNT_ID="${FRESHSTATUS_ACCOUNT_ID:-65}" \
  --from-literal=MYSQL_HOST="${MYSQL_HOST:-}" \
  --from-literal=MYSQL_PORT="${MYSQL_PORT:-3306}" \
  --from-literal=MYSQL_USER="${MYSQL_USER:-}" \
  --from-literal=MYSQL_PASSWORD="${MYSQL_PASSWORD:-}" \
  --from-literal=MYSQL_DATABASE="${MYSQL_DATABASE:-}" \
  --from-literal=FW_OUTAGE_SLACK_CHANNEL_ID="${FW_OUTAGE_SLACK_CHANNEL_ID:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Secret freshservice-agent-config updated in namespace ${NAMESPACE}."
echo ""
echo "Next: rebuild the source ConfigMap and roll out the deployment:"
echo "  ./scripts/deploy-all-agents-incluster.sh"
echo "  kubectl rollout restart deployment/freshservice-agent -n ${NAMESPACE}"
