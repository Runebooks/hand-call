#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-a2a-ops}"
IMAGE_NAME="${IMAGE_NAME:-a2a-kubernetes-agent}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
AWS_REGION="${AWS_REGION:-us-east-1}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${IMAGE_NAME}:${IMAGE_TAG}"

echo "==> Applying RBAC and Service"
kubectl apply -f "${ROOT}/deploy/kubernetes/rbac.yaml"
kubectl apply -f "${ROOT}/deploy/kubernetes/service.yaml"

echo "==> Building image ${IMAGE_NAME}:${IMAGE_TAG}"
docker build -t "${IMAGE_NAME}:${IMAGE_TAG}" "${ROOT}"

echo "==> Pushing to ${ECR_URI}"
aws ecr describe-repositories --repository-names "${IMAGE_NAME}" --region "${AWS_REGION}" 2>/dev/null \
  || aws ecr create-repository --repository-name "${IMAGE_NAME}" --region "${AWS_REGION}"

aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

docker tag "${IMAGE_NAME}:${IMAGE_TAG}" "${ECR_URI}"
docker push "${ECR_URI}"

echo "==> Deploying agent"
sed "s|IMAGE_PLACEHOLDER|${ECR_URI}|g" "${ROOT}/deploy/kubernetes/deployment.yaml" \
  | kubectl apply -f -

kubectl rollout status deployment/kubernetes-agent -n "${NAMESPACE}" --timeout=120s
kubectl get pods,svc -n "${NAMESPACE}" -l app.kubernetes.io/name=kubernetes-agent

echo ""
echo "Port-forward to test locally:"
echo "  kubectl port-forward -n ${NAMESPACE} svc/kubernetes-agent 8082:8082"
