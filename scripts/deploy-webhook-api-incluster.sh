#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-a2a-ops}"

ARCHIVE="$(mktemp -t a2a-webhook-src.XXXXXX.tar.gz)"
tar -czf "${ARCHIVE}" -C "${ROOT}" common agents master requirements.txt
kubectl create configmap a2a-webhook-api-source \
  --namespace="${NAMESPACE}" \
  --from-file=agent-src.tar.gz="${ARCHIVE}" \
  --dry-run=client -o yaml | kubectl apply -f -
rm -f "${ARCHIVE}"

kubectl apply -f "${ROOT}/deploy/kubernetes/deployment-webhook-api.yaml"
kubectl rollout status deployment/a2a-webhook-api -n "${NAMESPACE}" --timeout=180s
kubectl get svc,pods -n "${NAMESPACE}" -l app.kubernetes.io/name=a2a-webhook-api

echo ""
echo "n8n URL (in cluster): http://a2a-webhook-api.${NAMESPACE}.svc.cluster.local:8090/investigate"
