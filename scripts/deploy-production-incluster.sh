#!/usr/bin/env bash
# Production deploy — full NOC stack in a2a-ops (no local agents required).
#
# Prerequisites:
#   .env.local with OPENAI_API_KEY, SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_ALERT_CHANNEL_IDS
#
# Usage:
#   ./scripts/create-llm-secret.sh
#   ./scripts/create-slack-secret.sh
#   ./scripts/deploy-production-incluster.sh
#
# Optional (n8n / external HTTP from outside cluster):
#   ./scripts/deploy-production-incluster.sh --with-ingress
#   Edit deploy/kubernetes/ingress-webhook.yaml host + TLS first.
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-a2a-ops}"
WITH_INGRESS=false

for arg in "$@"; do
  case "${arg}" in
    --with-ingress) WITH_INGRESS=true ;;
    -h|--help)
      sed -n '1,18p' "$0"
      exit 0
      ;;
    *) echo "Unknown arg: ${arg} (try --with-ingress)" >&2; exit 1 ;;
  esac
done

echo "==> Preflight: namespace ${NAMESPACE}"
kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 \
  || kubectl create namespace "${NAMESPACE}"

missing=0
for secret in kubernetes-agent-llm slack-bot-secrets; do
  if ! kubectl get secret "${secret}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    echo "MISSING secret: ${secret}" >&2
    missing=1
  fi
done
if [[ "${missing}" -eq 1 ]]; then
  echo "" >&2
  echo "Run first:" >&2
  echo "  ./scripts/create-llm-secret.sh" >&2
  echo "  ./scripts/create-slack-secret.sh" >&2
  exit 1
fi

echo ""
echo "==> Core agents + master-agent"
"${ROOT}/scripts/deploy-all-agents-incluster.sh"

echo ""
echo "==> Slack bot (Socket Mode — outbound to Slack, no Ingress required)"
"${ROOT}/scripts/deploy-slack-bot-incluster.sh"

if [[ "${WITH_INGRESS}" == "true" ]]; then
  echo ""
  echo "==> Ingress (webhook API for n8n / external callers)"
  kubectl apply -f "${ROOT}/deploy/kubernetes/ingress-webhook.yaml"
  echo "Edit host/TLS in deploy/kubernetes/ingress-webhook.yaml if needed."
fi

echo ""
echo "==> Production stack in ${NAMESPACE}"
kubectl get pods -n "${NAMESPACE}" -l 'app.kubernetes.io/part-of=a2a-ops' \
  -o custom-columns=NAME:.metadata.name,READY:.status.containerStatuses[0].ready,STATUS:.status.phase

echo ""
cat <<EOF
Done. Stop any LOCAL processes so Slack does not hit your laptop:
  pkill -f 'master.slack_bot|master.server|agents.kubernetes.server' || true

Verify Slack → cluster (after @mention in alert thread):
  kubectl logs -n ${NAMESPACE} -l app.kubernetes.io/name=slack-bot -f --tail=30
  kubectl logs -n ${NAMESPACE} -l app.kubernetes.io/name=master-agent -f | grep -v 'GET /health'
  kubectl logs -n ${NAMESPACE} -l app.kubernetes.io/name=kubernetes-agent -f | grep -E 'Task received|Loaded in-cluster'

Internal URLs (ClusterIP — no Ingress needed for Slack):
  master-agent:     http://master-agent.${NAMESPACE}.svc.cluster.local:8095
  kubernetes-agent: http://kubernetes-agent.${NAMESPACE}.svc.cluster.local:8082
EOF
