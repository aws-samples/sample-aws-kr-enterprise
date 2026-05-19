# Infrastructure as Code — Terraform Modules

## Architecture

7 Terraform modules compose the AgentCore Agent Builder Platform infrastructure:

```
envs/dev/main.tf (Root Module)
    │
    ├── modules/network    VPC, Subnets, NAT, VPC Endpoints
    ├── modules/data       DynamoDB, S3
    ├── modules/auth       Cognito User Pool
    ├── modules/registry   ECR Repositories
    ├── modules/iam        IAM Roles (ECS, Platform API, AgentCore)
    ├── modules/compute    ECS Fargate, ALB, EventBridge
    └── modules/cdn        CloudFront, Route53, S3 Bucket Policy
```

## Module Dependency Graph

```
network ──→ compute ──→ cdn
  │              ↑        ↑
  └──→ data ─────┘────────┘
auth ──────→ compute
registry ──→ compute
iam ───────→ compute
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

## Required Variables

| Variable | Description | Sensitive |
|----------|-------------|-----------|
| `domain_name` | Route53 hosted zone domain | No |
| `cloudfront_secret` | Random string for CF→ALB origin validation | Yes |

## Prerequisites

- Terraform >= 1.5.0
- AWS CLI configured with appropriate permissions
- ACM wildcard certificate (*.your-domain.com) issued in both `ap-northeast-2` and `us-east-1`
- Route53 hosted zone for your domain
