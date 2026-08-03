# Infrastructure as Code — Terraform Modules

## Architecture

9 Terraform modules compose the AgentCore Agent Builder Platform infrastructure:

```
envs/dev/main.tf (Root Module)
    │
    ├── modules/network        VPC, Subnets, NAT, VPC Endpoints
    ├── modules/data           DynamoDB, S3
    ├── modules/auth           Cognito User Pool
    ├── modules/registry       ECR Repositories
    ├── modules/iam            IAM Roles (ECS, Platform API, AgentCore)
    ├── modules/compute        ECS Fargate, ALB, EventBridge
    ├── modules/cdn            CloudFront (VPC Origin), Route53, S3 Bucket Policy
    ├── modules/build          CodeBuild (x86 + arm64), S3 source bucket
    └── modules/observability  CloudWatch Transaction Search (aws/spans + X-Ray)
```

> **Note:** `modules/observability` (CloudWatch Transaction Search for the Trace Viewer) is wired into `envs/dev/main.tf`; the root module composes 9 modules (auth, build, cdn, compute, data, iam, network, observability, registry). It enables Transaction Search automatically via a `null_resource` local-exec (requires AWS CLI on the deploy host) — see the [README](../README.md#cloudwatch-transaction-search-trace-viewer) for the manual fallback.

## Module Dependency Graph

```
network ──→ compute ──→ cdn
  │              ↑        ↑
  └──→ data ─────┘────────┘
auth ──────→ compute
registry ──→ compute
iam ───────→ compute
build ─────→ (CodeBuild images pushed to registry/ECR)
```

## Quick Start

```bash
cd iac/envs/dev
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your values

terraform init
terraform plan
terraform apply
```

## Variables

| Variable | Required | Description | Sensitive |
|----------|----------|-------------|-----------|
| `aws_region` | Yes | AWS region (e.g. `us-west-2`) | No |
| `project` | Yes | Project name (e.g. `aiops-v2`) | No |
| `env` | Yes | Environment (e.g. `dev`) | No |
| `vpc_cidr` | Yes | VPC CIDR block | No |
| `domain_name` | No | Route53 hosted zone domain — leave empty to use the CloudFront default domain | No |

> CloudFront uses a **VPC Origin** to reach the ALB, so there is no `cloudfront_secret` variable.

## Prerequisites

- Terraform >= 1.5.0
- AWS CLI configured with appropriate permissions
- **(Optional, custom domain only)** If `domain_name` is set: an ACM wildcard certificate (`*.your-domain.com`) issued in **both** your deployment region and `us-east-1`, plus a Route53 hosted zone. Leave `domain_name` empty to use the CloudFront default domain (no ACM cert or Route53 required).
