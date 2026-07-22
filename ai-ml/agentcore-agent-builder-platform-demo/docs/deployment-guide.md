# Deployment Guide

## Prerequisites

| Requirement | Version | Purpose |
|-------------|---------|---------|
| AWS CLI | v2+ | AWS resource management |
| Terraform | >= 1.5.0 | Infrastructure provisioning |
| Docker | 20.10+ | Container image builds |
| Node.js | >= 18 | Frontend build |
| Python | >= 3.11 | API and agent runtime |

### AWS Prerequisites

1. **AWS Account** with Bedrock AgentCore access enabled
2. **ACM Certificates** (wildcard `*.your-domain.com`):
   - One in your deployment region (e.g., `ap-northeast-2`)
   - One in `us-east-1` (required for CloudFront)
3. **Route53 Hosted Zone** for your domain
4. **Bedrock Model Access** — Claude Sonnet enabled in your region

## Environment Variables

```bash
export AWS_REGION=ap-northeast-2
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export DOMAIN_NAME=your-domain.com
export CLOUDFRONT_SECRET=$(openssl rand -hex 16)
export PROJECT_PREFIX=aiops-v2-dev
```

## Deployment Steps

### Option A: One-Shot (Recommended)

```bash
./scripts/deploy-all.sh
```

This runs all 6 phases automatically. Takes approximately 15-20 minutes.

### Option B: Step-by-Step

#### Phase 1: Infrastructure

```bash
cd iac/envs/dev
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your values

terraform init
terraform plan
terraform apply
```

#### Phase 2: Container Images

```bash
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ECR_BASE"

# Platform API
docker build -t "${ECR_BASE}/${PROJECT_PREFIX}/platform-api:latest" code/control-plane/api
docker push "${ECR_BASE}/${PROJECT_PREFIX}/platform-api:latest"

# Frontend
docker build -t "${ECR_BASE}/${PROJECT_PREFIX}/frontend:latest" code/control-plane/ui
docker push "${ECR_BASE}/${PROJECT_PREFIX}/frontend:latest"

# Agent Base Image
docker build -t "${ECR_BASE}/${PROJECT_PREFIX}/base-image:latest" code/agent-runtime
docker push "${ECR_BASE}/${PROJECT_PREFIX}/base-image:latest"

# Report Image
docker build -f code/agent-runtime/Dockerfile.report \
  -t "${ECR_BASE}/${PROJECT_PREFIX}/report-image:latest" code/agent-runtime
docker push "${ECR_BASE}/${PROJECT_PREFIX}/report-image:latest"
```

#### Phase 3: Seed Data

```bash
./scripts/seed-dynamodb.sh
```

#### Phase 4: ECS Service Deployment

```bash
ECS_CLUSTER=$(cd iac/envs/dev && terraform output -raw ecs_cluster_arn)
aws ecs update-service --cluster "$ECS_CLUSTER" \
  --service "${PROJECT_PREFIX}-platform-api" --force-new-deployment
aws ecs update-service --cluster "$ECS_CLUSTER" \
  --service "${PROJECT_PREFIX}-frontend" --force-new-deployment
aws ecs wait services-stable --cluster "$ECS_CLUSTER" \
  --services "${PROJECT_PREFIX}-platform-api" "${PROJECT_PREFIX}-frontend"
```

#### Phase 5: AgentCore Agents

```bash
./scripts/deploy-agents.sh
```

#### Phase 6: MCP Gateways

```bash
./scripts/deploy-gateways.sh
./scripts/register-gateway-targets.py
```

## Post-Deployment

### Create Initial User

```bash
aws cognito-idp admin-create-user \
  --user-pool-id $(cd iac/envs/dev && terraform output -raw cognito_user_pool_id) \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com \
  --temporary-password "TempPass123!"
```

### Verify Deployment

1. Access `https://aiops-v2.${DOMAIN_NAME}`
2. Log in with the created user credentials
3. Navigate to Agents page — should show pre-configured agents
4. Open an agent's Playground and send a test message

## Troubleshooting

| Issue | Check |
|-------|-------|
| 403 on platform URL | CloudFront → ALB secret header mismatch |
| Cognito redirect loop | Callback URLs in Cognito client configuration |
| ECS tasks failing | CloudWatch Logs: `/ecs/${PROJECT_PREFIX}/platform-api` |
| Agent invocation timeout | Bedrock AgentCore service limits, VPC endpoint connectivity |
| No traces appearing | IAM role permissions for X-Ray, OTEL log group |

## Cleanup

```bash
# Remove AgentCore resources first (not managed by Terraform)
./scripts/deploy-agents.sh --delete  # if supported
./scripts/deploy-gateways.sh --delete  # if supported

# Destroy infrastructure
cd iac/envs/dev
terraform destroy -auto-approve
```

**Warning:** `force_destroy = true` is set on S3 and ECR resources for easy cleanup. Do not use this setting in production.
