#!/bin/bash
# scripts/deploy-all.sh — Scratch to full deployment (one-shot)
# Prerequisites: AWS CLI configured, Terraform >= 1.5, Docker, Node.js 18+
set -euo pipefail

################################################################################
# Environment Variables (Required)
################################################################################

: "${AWS_REGION:?Set AWS_REGION (e.g. ap-northeast-2)}"
: "${ACCOUNT_ID:?Set ACCOUNT_ID (12-digit AWS account ID)}"
: "${DOMAIN_NAME:?Set DOMAIN_NAME (Route53 hosted zone domain)}"
: "${CLOUDFRONT_SECRET:?Set CLOUDFRONT_SECRET (random string for origin validation)}"
: "${PROJECT_PREFIX:=aiops-v2-dev}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "=============================================="
echo " AgentCore Agent Builder Platform — Full Deploy"
echo "=============================================="
echo "Region:  $AWS_REGION"
echo "Account: $ACCOUNT_ID"
echo "Domain:  $DOMAIN_NAME"
echo "Prefix:  $PROJECT_PREFIX"
echo "=============================================="
echo ""

################################################################################
# Phase 1: Infrastructure (Terraform)
################################################################################

echo "▶ Phase 1: Terraform Infrastructure"
cd "$PROJECT_ROOT/iac/envs/dev"

cat > terraform.tfvars <<TFVARS
aws_region        = "$AWS_REGION"
project           = "$(echo $PROJECT_PREFIX | cut -d'-' -f1-2)"
env               = "$(echo $PROJECT_PREFIX | rev | cut -d'-' -f1 | rev)"
vpc_cidr          = "10.1.0.0/16"
domain_name       = "$DOMAIN_NAME"
cloudfront_secret = "$CLOUDFRONT_SECRET"
TFVARS

terraform init
terraform apply -auto-approve

# Extract outputs for subsequent phases
export VPC_ID=$(terraform output -raw vpc_id)
export ECS_CLUSTER=$(terraform output -raw ecs_cluster_arn)
export ALB_DNS=$(terraform output -raw alb_dns)
export DYNAMODB_TABLE=$(terraform output -raw dynamodb_platform_table)
export INCIDENTS_TABLE=$(terraform output -raw dynamodb_incidents_table)
export S3_BUCKET=$(terraform output -raw s3_reports_bucket)
export COGNITO_POOL_ID=$(terraform output -raw cognito_user_pool_id)
export ECR_REPOS=$(terraform output -json ecr_repos)

echo "✓ Phase 1 complete — Infrastructure provisioned"
echo ""

################################################################################
# Phase 2: Container Images (Build + Push to ECR)
################################################################################

echo "▶ Phase 2: Build & Push Container Images"
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ECR_BASE"

# Platform API
echo "  Building platform-api..."
docker build -t "${ECR_BASE}/${PROJECT_PREFIX}/platform-api:latest" "$PROJECT_ROOT/code/control-plane/api"
docker push "${ECR_BASE}/${PROJECT_PREFIX}/platform-api:latest"

# Frontend
echo "  Building frontend..."
docker build -t "${ECR_BASE}/${PROJECT_PREFIX}/frontend:latest" "$PROJECT_ROOT/code/control-plane/ui"
docker push "${ECR_BASE}/${PROJECT_PREFIX}/frontend:latest"

# Agent Base Image
echo "  Building base-image..."
docker build -t "${ECR_BASE}/${PROJECT_PREFIX}/base-image:latest" "$PROJECT_ROOT/code/agent-runtime"
docker push "${ECR_BASE}/${PROJECT_PREFIX}/base-image:latest"

# Report Image
echo "  Building report-image..."
docker build -f "$PROJECT_ROOT/code/agent-runtime/Dockerfile.report" -t "${ECR_BASE}/${PROJECT_PREFIX}/report-image:latest" "$PROJECT_ROOT/code/agent-runtime"
docker push "${ECR_BASE}/${PROJECT_PREFIX}/report-image:latest"

echo "✓ Phase 2 complete — All images pushed to ECR"
echo ""

################################################################################
# Phase 3: Seed Data
################################################################################

echo "▶ Phase 3: Seed DynamoDB"
cd "$SCRIPT_DIR"
bash seed-dynamodb.sh
echo "✓ Phase 3 complete — DynamoDB seeded"
echo ""

################################################################################
# Phase 4: Force ECS Deployment
################################################################################

echo "▶ Phase 4: ECS Service Redeployment"
aws ecs update-service --cluster "$ECS_CLUSTER" --service "${PROJECT_PREFIX}-platform-api" --force-new-deployment --region "$AWS_REGION" > /dev/null
aws ecs update-service --cluster "$ECS_CLUSTER" --service "${PROJECT_PREFIX}-frontend" --force-new-deployment --region "$AWS_REGION" > /dev/null
echo "  Waiting for services to stabilize..."
aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "${PROJECT_PREFIX}-platform-api" "${PROJECT_PREFIX}-frontend" --region "$AWS_REGION"
echo "✓ Phase 4 complete — ECS services running"
echo ""

################################################################################
# Phase 5: AgentCore Agents
################################################################################

echo "▶ Phase 5: Deploy AgentCore Agents"
bash deploy-agents.sh
echo "✓ Phase 5 complete — Agents registered"
echo ""

################################################################################
# Phase 6: MCP Gateways
################################################################################

echo "▶ Phase 6: Deploy MCP Gateways"
bash deploy-gateways.sh
echo "✓ Phase 6 complete — Gateways configured"
echo ""

################################################################################
# Done
################################################################################

echo "=============================================="
echo " ✅ Deployment Complete!"
echo "=============================================="
echo ""
echo " Platform URL: https://aiops-v2.${DOMAIN_NAME}"
echo " ALB DNS:      ${ALB_DNS}"
echo " ECS Cluster:  ${ECS_CLUSTER}"
echo ""
echo " Next steps:"
echo "   1. Create a user in Cognito User Pool: ${COGNITO_POOL_ID}"
echo "   2. Access the platform at https://aiops-v2.${DOMAIN_NAME}"
echo "=============================================="
