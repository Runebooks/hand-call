#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-a2a-ops}"

echo "==> Shared source ConfigMap"
ARCHIVE="$(mktemp -t a2a-agents-src.XXXXXX.tar.gz)"
COPYFILE_DISABLE=1 tar -czf "${ARCHIVE}" -C "${ROOT}" common agents master requirements.txt
kubectl create configmap a2a-agents-source \
  --namespace="${NAMESPACE}" \
  --from-file=agent-src.tar.gz="${ARCHIVE}" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create configmap kubernetes-agent-source \
  --namespace="${NAMESPACE}" \
  --from-file=agent-src.tar.gz="${ARCHIVE}" \
  --dry-run=client -o yaml | kubectl apply -f -
rm -f "${ARCHIVE}"

echo "==> Rollout restart (pick up ConfigMap source)"
kubectl rollout restart deployment/kubernetes-agent deployment/prometheus-agent deployment/rds-agent deployment/master-agent -n "${NAMESPACE}"

echo "==> RBAC + K8s agent"
kubectl apply -f "${ROOT}/deploy/kubernetes/rbac.yaml"
kubectl apply -f "${ROOT}/deploy/kubernetes/service.yaml"
kubectl apply -f "${ROOT}/deploy/kubernetes/deployment-configmap.yaml"
kubectl rollout status deployment/kubernetes-agent -n "${NAMESPACE}" --timeout=180s

echo "==> Prometheus + RDS agents"
kubectl apply -f "${ROOT}/deploy/kubernetes/deployment-prometheus.yaml"
kubectl apply -f "${ROOT}/deploy/kubernetes/deployment-rds.yaml"
kubectl rollout status deployment/prometheus-agent -n "${NAMESPACE}" --timeout=180s
kubectl rollout status deployment/rds-agent -n "${NAMESPACE}" --timeout=180s

echo "==> Master agent (router)"
kubectl apply -f "${ROOT}/deploy/kubernetes/deployment-master-agent.yaml"
kubectl rollout status deployment/master-agent -n "${NAMESPACE}" --timeout=180s

echo ""
echo "Agents (in cluster):"
echo "  prometheus-agent:8080  rds-agent:8081  kubernetes-agent:8082  master-agent:8095"
echo ""
echo "Production Slack bot (in cluster, no local scripts):"
echo "  ./scripts/create-slack-secret.sh && ./scripts/deploy-slack-bot-incluster.sh"
echo "  Or full stack: ./scripts/deploy-production-incluster.sh"
