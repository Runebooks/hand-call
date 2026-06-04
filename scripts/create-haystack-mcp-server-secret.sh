#!/usr/bin/env bash
# Create haystack-grafana-mcp-config for in-cluster grafana/mcp-grafana deployment.
# Get GRAFANA_URL and GRAFANA_SERVICE_ACCOUNT_TOKEN from observability / Haystack team.
set -euo pipefail

NAMESPACE="${NAMESPACE:-a2a-ops}"
SECRET_NAME="${SECRET_NAME:-haystack-grafana-mcp-config}"

GRAFANA_URL="${GRAFANA_URL:-}"
GRAFANA_SERVICE_ACCOUNT_TOKEN="${GRAFANA_SERVICE_ACCOUNT_TOKEN:-}"
GRAFANA_ORG_ID="${GRAFANA_ORG_ID:-}"

if [[ -z "${GRAFANA_URL}" || -z "${GRAFANA_SERVICE_ACCOUNT_TOKEN}" ]]; then
  echo "Set GRAFANA_URL and GRAFANA_SERVICE_ACCOUNT_TOKEN (from Haystack / observability)." >&2
  echo "Example:" >&2
  echo "  GRAFANA_URL=https://metrics.haystack.es \\" >&2
  echo "  GRAFANA_SERVICE_ACCOUNT_TOKEN=<grafana-sa-token> \\" >&2
  echo "  GRAFANA_ORG_ID=<org-id-for-fw-noc> \\" >&2
  echo "  $0" >&2
  exit 1
fi

kubectl create secret generic "${SECRET_NAME}" \
  --namespace="${NAMESPACE}" \
  --from-literal=GRAFANA_URL="${GRAFANA_URL}" \
  --from-literal=GRAFANA_SERVICE_ACCOUNT_TOKEN="${GRAFANA_SERVICE_ACCOUNT_TOKEN}" \
  --from-literal=GRAFANA_ORG_ID="${GRAFANA_ORG_ID}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Created ${NAMESPACE}/${SECRET_NAME}"
echo "Deploy MCP server: kubectl apply -f deploy/kubernetes/deployment-haystack-mcp.yaml"
echo "Point prometheus-agent at:"
echo "  HAYSTACK_MCP_URL=http://haystack-grafana-mcp.${NAMESPACE}.svc.cluster.local:8000"
echo "  PROMETHEUS_QUERY_BACKEND=auto"
