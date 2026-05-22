# AgentCore Agent Builder Platform Demo

> A reference architecture for building an enterprise AI agent management platform using [AWS Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/).

[English](#overview) | [한국어](#개요)

---

## Overview

This sample demonstrates how to build a full-stack agent management platform that:

- **Creates & manages AI agents** through a web-based UI with visual workflow design
- **Connects agents to tools** via MCP (Model Context Protocol) Gateways backed by Lambda functions
- **Provides real-time observability** — traces, logs, and metrics for agent execution
- **Automates incident response** — CloudWatch Alarms trigger agent-driven RCA (Root Cause Analysis)

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              CloudFront                                   │
│                          (HTTPS + Auth)                                   │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │     ALB + Cognito       │
                    └────────┬───────┬────────┘
                             │       │
               ┌─────────────┘       └─────────────┐
               ▼                                     ▼
    ┌──────────────────┐                  ┌──────────────────┐
    │  Platform API    │                  │    Frontend UI   │
    │  (FastAPI/ECS)   │                  │  (Next.js/ECS)   │
    └────────┬─────────┘                  └──────────────────┘
             │
             ├── DynamoDB (Agent Registry, Configs)
             ├── S3 (Reports)
             │
             ▼
    ┌──────────────────┐      ┌──────────────────┐
    │  Bedrock         │      │  MCP Gateways    │
    │  AgentCore       │◄────►│  (Lambda Tools)  │
    │  (Agent Runtime) │      │  130+ tools      │
    └──────────────────┘      └──────────────────┘
             │
             ▼
    ┌──────────────────┐
    │  Observability   │
    │  (X-Ray, CW,     │
    │   OTEL Spans)    │
    └──────────────────┘
```

## Project Structure

```
agentcore-agent-builder-platform-demo/
├── README.md
├── code/
│   ├── control-plane/
│   │   ├── api/           # FastAPI — Platform API
│   │   └── ui/            # Next.js 14 — Platform UI
│   ├── agent-runtime/     # Strands SDK Base Image
│   └── tools/             # Sample MCP Lambda Tools (3 of 130+)
├── iac/
│   ├── modules/           # 7 Terraform modules
│   │   ├── network/       # VPC, Subnets, NAT, VPC Endpoints
│   │   ├── data/          # DynamoDB, S3
│   │   ├── auth/          # Cognito
│   │   ├── registry/      # ECR
│   │   ├── iam/           # IAM Roles
│   │   ├── compute/       # ECS Fargate, ALB, EventBridge
│   │   └── cdn/           # CloudFront, Route53
│   └── envs/dev/          # Root module (environment config)
└── scripts/
    ├── deploy-all.sh      # One-shot full deployment
    ├── deploy-agents.sh   # AgentCore agent registration
    ├── deploy-gateways.sh # MCP Gateway setup
    ├── seed-dynamodb.sh   # Initial data seeding
    └── register-gateway-targets.py
```

## Prerequisites

- AWS Account with Bedrock AgentCore access
- Terraform >= 1.5.0
- Docker
- Node.js >= 18
- AWS CLI v2 configured
- (Optional) ACM wildcard certificate + Route53 hosted zone for custom domain

## Quick Start

```bash
# 1. Clone
git clone https://github.com/aws-samples/sample-aws-kr-enterprise.git
cd ai-ml/agentcore-agent-builder-platform-demo

# 2. Set required environment variables
export AWS_REGION=us-west-2
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 3. Deploy everything (uses CloudFront default domain — no custom domain needed)
./scripts/deploy-all.sh
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `AWS_REGION` | Yes | AWS region (e.g. us-west-2) |
| `ACCOUNT_ID` | Yes | 12-digit AWS Account ID |
| `DOMAIN_NAME` | No | Route53 hosted zone domain (empty = CloudFront default domain) |
| `PROJECT_PREFIX` | No | Resource prefix (default: aiops-v2-dev) |

## Key Features

### Agent Builder UI
- Visual agent workflow designer
- Real-time chat playground for testing agents
- Agent status monitoring dashboard

### MCP Gateway Integration
- 130+ pre-built AWS operational tools
- Lambda-based tool execution
- Cross-account resource access support

### Observability
- X-Ray distributed tracing for agent invocations
- CloudWatch Logs with structured spans
- Real-time metrics dashboard

### Automated Incident Response
- EventBridge rule captures CloudWatch Alarm state changes
- Automatically triggers incident RCA agent
- Generates structured reports saved to S3

## Cleanup

```bash
cd iac/envs/dev
terraform destroy -auto-approve
```

## Security

See [CONTRIBUTING](../../CONTRIBUTING.md) for security issue reporting.

## License

This project is licensed under the MIT-0 License. See the [LICENSE](../../LICENSE) file.

---

# 한국어

## 개요

이 샘플은 [AWS Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/)를 활용한 엔터프라이즈 AI 에이전트 관리 플랫폼의 레퍼런스 아키텍처입니다.

**주요 기능:**

- **AI 에이전트 생성/관리** — 웹 기반 UI에서 에이전트 워크플로우를 시각적으로 설계
- **MCP 도구 연결** — Lambda 기반 Gateway를 통해 130개 이상의 AWS 운영 도구 제공
- **실시간 관측성** — X-Ray 트레이싱, CloudWatch 로그, OTEL 스팬 기반 모니터링
- **자동 인시던트 대응** — CloudWatch 알람 → EventBridge → 에이전트 RCA 자동 실행

## 빠른 시작

```bash
# 1. 환경변수 설정
export AWS_REGION=us-west-2
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 2. 전체 배포 (약 15분, custom domain 없이 CloudFront 기본 도메인 사용)
./scripts/deploy-all.sh
```

## 기술 스택

| 계층 | 기술 |
|------|------|
| Frontend | Next.js 14, Tailwind CSS |
| Backend API | FastAPI (Python) |
| Agent Runtime | Strands SDK on Bedrock AgentCore |
| MCP Tools | AWS Lambda (Python) |
| Compute | ECS Fargate |
| CDN/Auth | CloudFront + Cognito |
| Data | DynamoDB, S3 |
| IaC | Terraform (7 modules) |
| Observability | X-Ray, CloudWatch, OTEL |

## 정리 (리소스 삭제)

```bash
cd iac/envs/dev
terraform destroy -auto-approve
```

## 참고

- [AWS Bedrock AgentCore Documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html)
- [Strands SDK](https://github.com/strands-agents/sdk-python)
- [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
