#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-a2a-ops}"

echo "==> Namespace + RBAC + Service"
kubectl apply -f "${ROOT}/deploy/kubernetes/rbac.yaml"
kubectl apply -f "${ROOT}/deploy/kubernetes/service.yaml"

echo "==> Source ConfigMap (tar archive)"
ARCHIVE="$(mktemp -t a2a-agent-src.XXXXXX.tar.gz)"
tar -czf "${ARCHIVE}" -C "${ROOT}" common agents master requirements.txt
kubectl create configmap a2a-agents-source \
  --namespace="${NAMESPACE}" \
  --from-file=agent-src.tar.gz="${ARCHIVE}" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create configmap kubernetes-agent-source \
  --namespace="${NAMESPACE}" \
  --from-file=agent-src.tar.gz="${ARCHIVE}" \
  --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
rm -f "${ARCHIVE}"

echo "==> Deployment (python:3.11-slim + mounted source)"
kubectl apply -f "${ROOT}/deploy/kubernetes/deployment-configmap.yaml"

echo "==> Waiting for rollout"
kubectl rollout status deployment/kubernetes-agent -n "${NAMESPACE}" --timeout=180s
kubectl get pods,svc -n "${NAMESPACE}" -l app.kubernetes.io/name=kubernetes-agent

echo ""
echo "Test in cluster:"
echo "  kubectl run -n ${NAMESPACE} curl-test --rm -it --restart=Never --image=curlimages/curl:8.5.0 -- \\"
echo "    curl -s http://kubernetes-agent:8082/health"
