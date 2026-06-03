#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-a2a-ops}"
kubectl apply -f "${ROOT}/deploy/kubernetes/deployment-master-agent.yaml"
kubectl rollout status deployment/master-agent -n "${NAMESPACE}" --timeout=180s
kubectl get svc,pods -n "${NAMESPACE}" -l app.kubernetes.io/name=master-agent
echo ""
echo "Master agent: http://master-agent.${NAMESPACE}.svc.cluster.local:8095"
echo "Set on slack-bot: MASTER_AGENT_URL=http://master-agent.${NAMESPACE}.svc.cluster.local:8095"
