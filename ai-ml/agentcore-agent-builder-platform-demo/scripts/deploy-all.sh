#!/bin/bash
# scripts/deploy-all.sh — Scratch to full deployment (one-shot)
# Prerequisites: AWS CLI configured, Terraform >= 1.5, Docker, Node.js 18+
set -euo pipefail

################################################################################
# Environment Variables (Required)
################################################################################

# Exported so child scripts launched via `bash <script>` (new processes) inherit
# them. seed-dynamodb.sh / deploy-agents.sh run under `set -u` and reference
# PROJECT_PREFIX/DOMAIN_NAME directly, so a non-exported value would abort them.
export AWS_REGION
export ACCOUNT_ID
: "${AWS_REGION:?Set AWS_REGION (e.g. us-west-2)}"
: "${ACCOUNT_ID:?Set ACCOUNT_ID (12-digit AWS account ID)}"
export DOMAIN_NAME="${DOMAIN_NAME:-}"
export PROJECT_PREFIX="${PROJECT_PREFIX:-aiops-v2-dev}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "=============================================="
echo " AgentCore Agent Builder Platform — Full Deploy"
echo "=============================================="
echo "Region:  $AWS_REGION"
echo "Account: $ACCOUNT_ID"
echo "Domain:  ${DOMAIN_NAME:-<CloudFront default>}"
echo "Prefix:  $PROJECT_PREFIX"
echo "=============================================="
echo ""

################################################################################
# Phase 1: Infrastructure (Terraform)
################################################################################

echo "▶ Phase 1: Terraform Infrastructure"
cd "$PROJECT_ROOT/iac/envs/dev"

cat > terraform.tfvars <<TFVARS
aws_region  = "$AWS_REGION"
project     = "$(echo $PROJECT_PREFIX | cut -d'-' -f1-2)"
env         = "$(echo $PROJECT_PREFIX | rev | cut -d'-' -f1 | rev)"
vpc_cidr    = "10.1.0.0/16"
domain_name = "$DOMAIN_NAME"
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

# Pre-create AgentCore Service Linked Role (required for first-time deployment)
echo "▶ Pre-check: AgentCore Service Linked Role"
aws iam create-service-linked-role --aws-service-name bedrock-agentcore.amazonaws.com --region "$AWS_REGION" 2>/dev/null \
  && echo "  Created AgentCore SLR" \
  || echo "  SLR already exists (OK)"

################################################################################
# Phase 2: Container Images (Build via CodeBuild)
################################################################################

echo "▶ Phase 2: Build Container Images (CodeBuild)"
export CB_PROJECT_X86=$(cd "$PROJECT_ROOT/iac/envs/dev" && terraform output -raw codebuild_project_x86)
export CB_PROJECT_ARM64=$(cd "$PROJECT_ROOT/iac/envs/dev" && terraform output -raw codebuild_project_arm64)
export CB_SOURCE_BUCKET=$(cd "$PROJECT_ROOT/iac/envs/dev" && terraform output -raw codebuild_source_bucket)
cd "$SCRIPT_DIR"
bash build-images.sh
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
# Phase 7: MCP Lambda Tools
################################################################################

echo "▶ Phase 7: Deploy MCP Lambda Tools"
cd "$SCRIPT_DIR"
bash deploy-lambda-tools.sh
echo "✓ Phase 7 complete — Lambda tools deployed"
echo ""

################################################################################
# Phase 8: Gateway Targets
################################################################################

echo "▶ Phase 8: Register Gateway Targets"
cd "$SCRIPT_DIR"
AWS_REGION="$AWS_REGION" ACCOUNT_ID="$ACCOUNT_ID" python3 register-gateway-targets.py
echo "✓ Phase 8 complete — Gateway targets registered"
echo ""

################################################################################
# Done
################################################################################

CF_DOMAIN=$(cd "$PROJECT_ROOT/iac/envs/dev" && terraform output -raw cloudfront_distribution_id 2>/dev/null || echo "")
PLATFORM_URL=""
if [ -n "$DOMAIN_NAME" ]; then
  PLATFORM_URL="https://aiops-v2.${DOMAIN_NAME}"
elif [ -n "$CF_DOMAIN" ]; then
  CF_DOMAIN_NAME=$(aws cloudfront get-distribution --id "$CF_DOMAIN" --query 'Distribution.DomainName' --output text --region us-east-1 2>/dev/null || echo "")
  PLATFORM_URL="https://${CF_DOMAIN_NAME}"
fi

echo "=============================================="
echo " Deployment Complete!"
echo "=============================================="
echo ""
echo " Platform URL: ${PLATFORM_URL}"
echo " ALB DNS:      ${ALB_DNS} (internal)"
echo " ECS Cluster:  ${ECS_CLUSTER}"
echo " Cognito Pool: ${COGNITO_POOL_ID}"
echo ""
echo " Next steps:"
echo "   1. POST ${PLATFORM_URL}/api/auth/signup to create a user"
echo "   2. Verify email with code, then POST /api/auth/login"
echo "   3. Access the platform at ${PLATFORM_URL}"
echo "=============================================="
