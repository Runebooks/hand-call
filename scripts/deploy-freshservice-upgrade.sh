#!/usr/bin/env bash
# Deploy the Freshservice leadership upgrade (Postgres MIM store, issue-category filters,
# check_ongoing_outages, mim-store-sync CronJob).
#
# Prerequisites:
#   export KUBECONFIG=~/.kube/teleport-n8n-prod   # after: tsh kube login n8n-prod
#   Write access to a2a-ops (configmaps, secrets, deployments, cronjobs, jobs)
#   .env.local with FRESHSERVICE_API_KEY + PG* + SLACK_BOT_TOKEN (see .env.example)
#
# Usage:
#   ./scripts/create-freshservice-secret.sh   # or ensure secret has PG* + SLACK keys
#   ./scripts/deploy-freshservice-upgrade.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-a2a-ops}"

if ! kubectl auth can-i patch deployment -n "${NAMESPACE}" >/dev/null 2>&1; then
  echo "ERROR: no write access to deployments in ${NAMESPACE}." >&2
  echo "Your Teleport role may be read-only (k8s-user). Ask for deploy access or run as cluster admin." >&2
  kubectl auth can-i patch deployment -n "${NAMESPACE}" 2>&1 || true
  exit 1
fi

echo "==> Update freshservice-agent-config secret (PG*, Slack, Freshservice)"
if [[ -x "${ROOT}/scripts/create-freshservice-secret.sh" ]]; then
  ENV_FILE="${ENV_FILE:-${ROOT}/.env.local}" ./scripts/create-freshservice-secret.sh
else
  echo "WARN: create-freshservice-secret.sh not found; assuming secret already has PG* + SLACK_BOT_TOKEN"
fi

echo "==> Rebuild a2a-agents-source ConfigMap"
ARCHIVE="$(mktemp -t a2a-agents-src.XXXXXX.tar.gz)"
COPYFILE_DISABLE=1 tar -czf "${ARCHIVE}" -C "${ROOT}" common agents master requirements.txt
kubectl delete configmap a2a-agents-source kubernetes-agent-source \
  --namespace="${NAMESPACE}" --ignore-not-found
kubectl create configmap a2a-agents-source \
  --namespace="${NAMESPACE}" \
  --from-file=agent-src.tar.gz="${ARCHIVE}"
kubectl create configmap kubernetes-agent-source \
  --namespace="${NAMESPACE}" \
  --from-file=agent-src.tar.gz="${ARCHIVE}"
rm -f "${ARCHIVE}"

echo "==> Apply Freshservice deployment + CronJobs (includes mim-store-sync)"
kubectl apply -f "${ROOT}/deploy/kubernetes/deployment-freshservice.yaml"
kubectl apply -f "${ROOT}/deploy/kubernetes/cronjobs-freshservice.yaml"

echo "==> Rollout freshservice-agent + master-agent"
kubectl rollout restart deployment/freshservice-agent -n "${NAMESPACE}"
kubectl rollout restart deployment/master-agent -n "${NAMESPACE}"
kubectl rollout status deployment/freshservice-agent -n "${NAMESPACE}" --timeout=240s

echo "==> Verify startup logs"
kubectl logs deployment/freshservice-agent -n "${NAMESPACE}" --tail=30 | grep -E 'MIM_Store|fw-outage|Postgres|Started|ERROR|WARNING' || true

echo "==> One-off MIM store sync (populates Postgres fallback cache)"
kubectl create job "mim-store-sync-manual-$(date +%s)" \
  --namespace="${NAMESPACE}" \
  --from=cronjob/freshservice-mim-store-sync 2>/dev/null \
  || echo "WARN: could not create manual sync job (CronJob may not exist yet)"

echo ""
echo "Done. Spot-check in Slack:"
echo "  @NOC Handover Can you get MIMs related to third-party in the last one month?"
echo "  @NOC Handover Is there any ongoing outage right now?"
echo "  @NOC Handover Who was involved in the Freshdesk outage on May 14?"
echo ""
echo "In-cluster tools test:"
echo "  kubectl exec -n ${NAMESPACE} deploy/freshservice-agent -- python scripts/test-freshservice-questions.py --tools-only"
