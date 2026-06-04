#!/usr/bin/env bash
# Sync prometheus-agent-config from Haystack in-cluster auth (n8n-prod / fw-noc).
set -euo pipefail

NAMESPACE="${NAMESPACE:-a2a-ops}"
HAYSTACK_NS="${HAYSTACK_NS:-haystack}"
HAYSTACK_SECRET="${HAYSTACK_SECRET:-haystack--fw-noc--auth-token}"
HAYSTACK_TOKEN_KEY="${HAYSTACK_TOKEN_KEY:-haystack--fw-noc--auth-token}"
HAYSTACK_ENV_CM="${HAYSTACK_ENV_CM:-haystack-env-thb6hd6t4f}"

# Haystack Mimir query API (same VPC endpoint as remote_write push, /api/prom prefix)
DEFAULT_PROM_URL="http://vpce-0da3bda96a81ffc92-829begfe.vpce-svc-0680c3e2a8c08eb7c.us-east-1.vpce.amazonaws.com:10001/api/prom"
DEFAULT_ORG_ID="fw-noc"
# In-cluster Grafana MCP (VPC) — see docs/HAYSTACK_GRAFANA_MCP.md
DEFAULT_MCP_URL="http://o11y.int.haystack.es/mcp"
DEFAULT_MCP_TENANT="fw-noc"

if ! kubectl get secret "${HAYSTACK_SECRET}" -n "${HAYSTACK_NS}" >/dev/null 2>&1; then
  echo "ERROR: secret ${HAYSTACK_SECRET} not found in ${HAYSTACK_NS}" >&2
  echo "List: kubectl get secrets -n ${HAYSTACK_NS}" >&2
  exit 1
fi

TOKEN="$(kubectl get secret "${HAYSTACK_SECRET}" -n "${HAYSTACK_NS}" \
  -o "jsonpath={.data.${HAYSTACK_TOKEN_KEY}}" | base64 -d)"

if [[ -z "${TOKEN}" ]]; then
  echo "ERROR: empty token from ${HAYSTACK_NS}/${HAYSTACK_SECRET}" >&2
  exit 1
fi

PROM_URL="${PROMETHEUS_URL:-}"
# Ignore Grafana UI URL — direct Prom API must be VPC /api/prom or explicit PROMETHEUS_URL
if [[ "${PROM_URL}" == *metrics.haystack.es* ]]; then
  PROM_URL=""
fi
if [[ -z "${PROM_URL}" ]] && kubectl get configmap "${HAYSTACK_ENV_CM}" -n "${HAYSTACK_NS}" >/dev/null 2>&1; then
  PUSH="$(kubectl get configmap "${HAYSTACK_ENV_CM}" -n "${HAYSTACK_NS}" \
    -o jsonpath='{.data.HAYSTACK_METRICS_ENDPOINT}')"
  PROM_URL="${PUSH%/api/prom/push}"
  PROM_URL="${PROM_URL%/push}"
fi
PROM_URL="${PROM_URL:-${DEFAULT_PROM_URL}}"
PROM_URL="${PROM_URL%/}"
if [[ "${PROM_URL}" != */api/prom ]]; then
  PROM_URL="${PROM_URL}/api/prom"
fi

ORG_ID="${PROMETHEUS_ORG_ID:-${DEFAULT_ORG_ID}}"
MCP_URL="${HAYSTACK_MCP_URL:-${DEFAULT_MCP_URL}}"
MCP_TENANT="${HAYSTACK_MCP_TENANT:-${DEFAULT_MCP_TENANT}}"
QUERY_BACKEND="${PROMETHEUS_QUERY_BACKEND:-direct}"

kubectl create secret generic prometheus-agent-config \
  --namespace="${NAMESPACE}" \
  --from-literal=PROMETHEUS_URL="${PROM_URL}" \
  --from-literal=PROMETHEUS_TOKEN="${TOKEN}" \
  --from-literal=PROMETHEUS_ORG_ID="${ORG_ID}" \
  --from-literal=PROMETHEUS_VERIFY_SSL="${PROMETHEUS_VERIFY_SSL:-true}" \
  --from-literal=PROMETHEUS_INTERNAL="${PROMETHEUS_INTERNAL:-true}" \
  --from-literal=PROMQL_HOSTNAME_QUERY="${PROMQL_HOSTNAME_QUERY:-}" \
  --from-literal=HAYSTACK_MCP_URL="${MCP_URL}" \
  --from-literal=HAYSTACK_MCP_TENANT="${MCP_TENANT}" \
  --from-literal=HAYSTACK_MCP_TOKEN="${TOKEN}" \
  --from-literal=PROMETHEUS_QUERY_BACKEND="${QUERY_BACKEND}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "prometheus-agent-config updated (prom=${PROM_URL}, org=${ORG_ID}, mcp=${MCP_URL}, tenant=${MCP_TENANT}, backend=${QUERY_BACKEND})"
echo "Redeploy: kubectl rollout restart deployment/prometheus-agent -n ${NAMESPACE}"
