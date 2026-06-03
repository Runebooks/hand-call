#!/usr/bin/env bash
# Port-forward the kubernetes agent to localhost (keep this terminal open).
set -euo pipefail

LOCAL_PORT="${LOCAL_PORT:-8082}"
NAMESPACE="${NAMESPACE:-a2a-ops}"

echo "Waiting for agent pod to be ready..."
kubectl wait -n "${NAMESPACE}" --for=condition=ready pod \
  -l app.kubernetes.io/name=kubernetes-agent --timeout=180s

echo "Forwarding http://127.0.0.1:${LOCAL_PORT} -> kubernetes-agent:${LOCAL_PORT}"
echo "Press Ctrl+C to stop."
exec kubectl port-forward -n "${NAMESPACE}" "svc/kubernetes-agent" "${LOCAL_PORT}:${LOCAL_PORT}"
