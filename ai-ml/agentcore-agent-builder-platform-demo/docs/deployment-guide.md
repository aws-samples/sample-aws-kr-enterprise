# Deployment Guide

## Prerequisites

| Requirement | Version | Purpose |
|-------------|---------|---------|
| AWS CLI | v2+ | AWS resource management |
| Terraform | >= 1.5.0 | Infrastructure provisioning |
| Node.js | >= 18 | Frontend build |
| Python | >= 3.11 | API/agent runtime + deploy scripts |
| boto3 | latest (`pip install boto3`) | Phase 8 (`register-gateway-targets.py`) and other python3 phases; deploy fails at Phase 8 without it |

> **No local Docker required.** The `deploy-all.sh` / CodeBuild path builds all container images in the cloud (Phase 2) — you do not need Docker installed locally.

### AWS Prerequisites

1. **AWS Account** with Bedrock AgentCore access enabled
2. **Bedrock foundation-model access** enabled in `AWS_REGION` — both **Claude Sonnet** (agents, `global.anthropic.claude-sonnet-4-6`) and **Claude Opus** (`global.anthropic.claude-opus-4-6-v1`, used by Agent Builder). Without model access, the Playground and Builder fail at invocation time even though deploy succeeds.
3. **CloudWatch Transaction Search** — required for the Trace Viewer to show data. The terraform `observability` module enables it automatically (`aws/spans` log group + X-Ray trace-segment destination); enable it manually if you deploy without that module (see [README — CloudWatch Transaction Search](../README.md#cloudwatch-transaction-search-trace-viewer)).
4. **(Optional) Custom domain** — only if you set `DOMAIN_NAME`. Leaving it empty uses the CloudFront default domain (no ACM cert or Route53 needed). For a custom domain you additionally need:
   - ACM wildcard certificate (`*.your-domain.com`) in your deployment region **and** in `us-east-1` (required for CloudFront)
   - A Route53 hosted zone for your domain

## Environment Variables

`AWS_REGION` and `ACCOUNT_ID` are required; `DOMAIN_NAME` and `PROJECT_PREFIX` are optional (matches the README quick start).

```bash
# Required
export AWS_REGION=us-west-2
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Optional
export DOMAIN_NAME=            # empty = CloudFront default domain
export PROJECT_PREFIX=aiops-v2-dev
```

## Deployment Steps

### Option A: One-Shot (Recommended)

```bash
./scripts/deploy-all.sh
```

This runs all 8 phases automatically. Takes approximately 15-20 minutes.

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

#### Phase 2: Container Images (CodeBuild — no local Docker)

Images are built in the cloud by CodeBuild and pushed to ECR (`platform-api`, `frontend`, `base-image`, `report-image`):

```bash
./scripts/build-images.sh
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
```

#### Phase 7: MCP Lambda Tools

Deploys the 19 Lambda-backed MCP tools (auto-creates the Lambda execution role):

```bash
./scripts/deploy-lambda-tools.sh
```

#### Phase 8: Gateway Targets

Registers tool targets on the 8 gateways (requires Python 3.11+ with boto3):

```bash
python3 scripts/register-gateway-targets.py
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

1. Access the platform URL. With a custom domain set, this is `https://aiops-v2.${DOMAIN_NAME}`; with `DOMAIN_NAME` empty, use the CloudFront default domain (`https://<distribution-id>.cloudfront.net` — from the `cloudfront_distribution_id` Terraform output).
2. Log in with the created user credentials
3. Navigate to Agents page — should show pre-configured agents
4. Open an agent's Playground and send a test message

## Troubleshooting

| Issue | Check |
|-------|-------|
| 403 on platform URL | CloudFront VPC Origin → ALB reachability (security groups, target health) |
| Cognito redirect loop | Callback URLs in Cognito client configuration |
| ECS tasks failing | CloudWatch Logs: `/ecs/${PROJECT_PREFIX}/platform-api` |
| Agent invocation timeout | Bedrock AgentCore service limits, VPC endpoint connectivity |
| Playground/Builder fails at invocation | Bedrock model access (Claude Sonnet + Opus) not enabled in `AWS_REGION` |
| No traces appearing | CloudWatch Transaction Search not enabled; IAM role permissions for X-Ray, OTEL log group |

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
