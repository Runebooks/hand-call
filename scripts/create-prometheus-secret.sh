#!/usr/bin/env bash
# Prometheus endpoint for prometheus-agent (reads .env.local).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-a2a-ops}"
ENV_FILE="${ENV_FILE:-${ROOT}/.env.local}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Create ${ENV_FILE} and set PROMETHEUS_URL (e.g. https://metrics.haystack.es)" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

if [[ -z "${PROMETHEUS_URL:-}" ]]; then
  echo "PROMETHEUS_URL is empty in ${ENV_FILE}" >&2
  echo "Example: PROMETHEUS_URL=https://metrics.haystack.es" >&2
  exit 1
fi

kubectl create secret generic prometheus-agent-config \
  --namespace="${NAMESPACE}" \
  --from-literal=PROMETHEUS_URL="${PROMETHEUS_URL}" \
  --from-literal=PROMETHEUS_TOKEN="${PROMETHEUS_TOKEN:-}" \
  --from-literal=PROMETHEUS_ORG_ID="${PROMETHEUS_ORG_ID:-}" \
  --from-literal=PROMETHEUS_VERIFY_SSL="${PROMETHEUS_VERIFY_SSL:-true}" \
  --from-literal=PROMETHEUS_INTERNAL="${PROMETHEUS_INTERNAL:-false}" \
  --from-literal=PROMQL_HOSTNAME_QUERY="${PROMQL_HOSTNAME_QUERY:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Secret prometheus-agent-config updated in namespace ${NAMESPACE}"
echo ""
echo "Note: https://metrics.haystack.es is Grafana (Google SSO) — not a direct Prom API."
echo "For live PromQL use an in-cluster Prometheus URL or PROMETHEUS_TOKEN."
echo "Alert RPM/threshold answers work from Slack metadata without live Prom."
echo "Redeploy: kubectl rollout restart deployment/prometheus-agent -n ${NAMESPACE}"
