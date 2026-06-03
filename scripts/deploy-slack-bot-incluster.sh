#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-a2a-ops}"

echo "==> Slack bot secret (run ./scripts/create-slack-secret.sh first if missing)"
kubectl get secret slack-bot-secrets -n "${NAMESPACE}" >/dev/null 2>&1 \
  || echo "WARN: slack-bot-secrets not found" >&2

echo "==> Source ConfigMap"
ARCHIVE="$(mktemp -t a2a-slack-src.XXXXXX.tar.gz)"
tar -czf "${ARCHIVE}" -C "${ROOT}" common agents master requirements.txt
kubectl create configmap slack-bot-source \
  --namespace="${NAMESPACE}" \
  --from-file=agent-src.tar.gz="${ARCHIVE}" \
  --dry-run=client -o yaml | kubectl apply -f -
rm -f "${ARCHIVE}"

echo "==> Deployment"
kubectl apply -f "${ROOT}/deploy/kubernetes/deployment-slack-bot.yaml"

echo "==> Rollout"
kubectl rollout status deployment/slack-bot -n "${NAMESPACE}" --timeout=180s
kubectl get pods -n "${NAMESPACE}" -l app.kubernetes.io/name=slack-bot
kubectl logs -n "${NAMESPACE}" deploy/slack-bot --tail=20
