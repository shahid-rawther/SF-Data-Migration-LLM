#!/bin/bash
# deploy.sh – Build, push Docker image to ECR and deploy to ECS Fargate via CloudFormation.
# Usage: ./deploy/deploy.sh [--stack-name NAME] [--region REGION] [--tag TAG]
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
APP_NAME="itsm-migration"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
STACK_NAME="${APP_NAME}"
IMAGE_TAG="${1:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${APP_NAME}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

echo "======================================================"
echo "  ITSM Migration – AWS Deployment"
echo "======================================================"
echo "  App name   : $APP_NAME"
echo "  Account    : $ACCOUNT_ID"
echo "  Region     : $REGION"
echo "  Stack      : $STACK_NAME"
echo "  Image tag  : $IMAGE_TAG"
echo "======================================================"

# ── Preflight: Ensure ECR repo exists and stack is recoverable ───────────────
echo ""
echo "▶ [0/5] Running preflight checks..."

STACK_STATUS=$(aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].StackStatus" \
    --output text 2>/dev/null || true)

if [[ "$STACK_STATUS" == "ROLLBACK_COMPLETE" ]]; then
    echo "  Existing stack is in ROLLBACK_COMPLETE. Deleting before redeploy..."
    aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"
    aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME"
    echo "  Stack deleted."
fi

aws ecr describe-repositories \
    --region "$REGION" \
    --repository-names "$APP_NAME" >/dev/null 2>&1 \
    || aws ecr create-repository --region "$REGION" --repository-name "$APP_NAME" >/dev/null

# ── Step 1: Authenticate Docker to ECR ───────────────────────────────────────
echo ""
echo "▶ [1/5] Authenticating Docker with ECR..."
aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

# ── Step 2: Build the Docker image ───────────────────────────────────────────
echo ""
echo "▶ [2/5] Building Docker image..."
cd "$ROOT_DIR"
docker build \
    --platform linux/amd64 \
    -t "${APP_NAME}:${IMAGE_TAG}" \
    -f Dockerfile \
    .

# ── Step 3: Push image to ECR ────────────────────────────────────────────────
echo ""
echo "▶ [3/5] Pushing image to ECR..."
docker tag "${APP_NAME}:${IMAGE_TAG}" "${ECR_URI}:${IMAGE_TAG}"
docker tag "${APP_NAME}:${IMAGE_TAG}" "${ECR_URI}:latest"
docker push "${ECR_URI}:${IMAGE_TAG}"
docker push "${ECR_URI}:latest"

# ── Step 4: Deploy/Update CloudFormation stack with ready image ──────────────
echo ""
echo "▶ [4/5] Deploying CloudFormation infrastructure..."
aws cloudformation deploy \
    --region "$REGION" \
    --stack-name "$STACK_NAME" \
    --template-file "$SCRIPT_DIR/cloudformation.yml" \
    --parameter-overrides \
        AppName="$APP_NAME" \
        ImageUri="${ECR_URI}:${IMAGE_TAG}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset

# ── Step 5: Force ECS deployment and wait for stability ──────────────────────
echo ""
echo "▶ [5/5] Forcing ECS redeploy and waiting for service stability..."

CLUSTER=$(aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='ECSCluster'].OutputValue" \
    --output text)

SERVICE=$(aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='ECSService'].OutputValue" \
    --output text)

aws ecs update-service \
    --region "$REGION" \
    --cluster "$CLUSTER" \
    --service "$SERVICE" \
    --force-new-deployment \
    --query "service.serviceName" \
    --output text

if ! aws ecs wait services-stable --region "$REGION" --cluster "$CLUSTER" --services "$SERVICE"; then
    echo ""
    echo "ECS service did not stabilize. Recent service events:"
    aws ecs describe-services \
        --region "$REGION" \
        --cluster "$CLUSTER" \
        --services "$SERVICE" \
        --query "services[0].events[0:10].[createdAt,message]" \
        --output table || true
    exit 1
fi

# ── Done ──────────────────────────────────────────────────────────────────────
APP_URL=$(aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='AppURL'].OutputValue" \
    --output text)

echo ""
echo "======================================================"
echo "  Deployment complete!"
echo "  URL: $APP_URL"
echo "  (ECS may take 1-2 min for the health check to pass)"
echo "======================================================"
