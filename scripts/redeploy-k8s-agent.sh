#!/usr/bin/env bash
# Rebuild and restart in-cluster kubernetes-agent (required after server.py changes).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-a2a-ops}"
"${ROOT}/scripts/deploy-k8s-agent-incluster.sh"
kubectl rollout status deployment/kubernetes-agent -n "${NAMESPACE}" --timeout=180s
echo "kubernetes-agent restarted. Slack bot must also be restarted locally."
