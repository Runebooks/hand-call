#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-a2a-ops}"

echo "==> Slack bot secret (run ./scripts/create-slack-secret.sh first if missing)"
kubectl get secret slack-bot-secrets -n "${NAMESPACE}" >/dev/null 2>&1 \
  || { echo "ERROR: slack-bot-secrets not found — run ./scripts/create-slack-secret.sh" >&2; exit 1; }

echo "==> Source ConfigMap"
ARCHIVE="$(mktemp -t a2a-slack-src.XXXXXX.tar.gz)"
COPYFILE_DISABLE=1 tar -czf "${ARCHIVE}" -C "${ROOT}" common agents master requirements.txt
kubectl create configmap slack-bot-source \
  --namespace="${NAMESPACE}" \
  --from-file=agent-src.tar.gz="${ARCHIVE}" \
  --dry-run=client -o yaml | kubectl apply -f -
rm -f "${ARCHIVE}"

echo "==> Deployment"
kubectl apply -f "${ROOT}/deploy/kubernetes/deployment-slack-bot.yaml"

echo "==> Rollout"
kubectl rollout restart deployment/slack-bot -n "${NAMESPACE}" 2>/dev/null || true
kubectl rollout status deployment/slack-bot -n "${NAMESPACE}" --timeout=180s
kubectl get pods -n "${NAMESPACE}" -l app.kubernetes.io/name=slack-bot
echo ""
echo "Startup logs:"
kubectl logs -n "${NAMESPACE}" deploy/slack-bot --tail=25
