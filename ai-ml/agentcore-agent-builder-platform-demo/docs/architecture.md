# Architecture

## System Overview

The AgentCore Agent Builder Platform consists of 4 major layers:

1. **Presentation Layer** — CloudFront + Next.js UI
2. **Control Plane** — FastAPI on ECS Fargate
3. **Agent Runtime** — Bedrock AgentCore + Strands SDK
4. **Tool Layer** — MCP Gateways backed by Lambda functions

## Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Internet / Users                                    │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
┌──────────────────────────────────▼──────────────────────────────────────────┐
│  CloudFront Distribution                                                     │
│  - HTTPS termination                                                         │
│  - X-CloudFront-Secret header injection                                      │
│  - Route: /* → Frontend, /api/* → Platform API                              │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
┌──────────────────────────────────▼──────────────────────────────────────────┐
│  ALB + Cognito Authentication                                                │
│  - HTTPS listener with Cognito auth action                                   │
│  - HTTP listener for CloudFront (secret header validation)                   │
│  - Path-based routing: /api/* → API TG, /* → Frontend TG                   │
└─────────────────┬───────────────────────────────────┬───────────────────────┘
                  │                                   │
    ┌─────────────▼──────────────┐     ┌─────────────▼──────────────┐
    │   Platform API (ECS)       │     │   Frontend UI (ECS)        │
    │   FastAPI / Python         │     │   Next.js 14 / Node.js     │
    │   Port 8000                │     │   Port 3000                │
    │                            │     │                            │
    │   Responsibilities:        │     │   Responsibilities:        │
    │   - Agent CRUD             │     │   - Agent management UI    │
    │   - AgentCore integration  │     │   - Chat playground        │
    │   - Session management     │     │   - Workflow designer       │
    │   - Observability queries  │     │   - Trace viewer           │
    └─────────────┬──────────────┘     └────────────────────────────┘
                  │
    ┌─────────────▼──────────────────────────────────────────────┐
    │                    AWS Services                              │
    │                                                             │
    │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐       │
    │  │  DynamoDB    │  │     S3      │  │   Cognito   │       │
    │  │  - Platform  │  │  - Reports  │  │  - Auth     │       │
    │  │  - Incidents │  │             │  │             │       │
    │  └─────────────┘  └─────────────┘  └─────────────┘       │
    │                                                             │
    │  ┌─────────────────────────────────────────────────┐       │
    │  │  Bedrock AgentCore                               │       │
    │  │  - Agent Runtime (Strands SDK containers)        │       │
    │  │  - MCP Gateways (8 gateways, 130+ tools)        │       │
    │  │  - Observability (X-Ray, OTEL Spans)             │       │
    │  └──────────────────────┬──────────────────────────┘       │
    │                         │                                   │
    │  ┌──────────────────────▼──────────────────────────┐       │
    │  │  Lambda Functions (MCP Tools)                    │       │
    │  │  - aws_cloudwatch_mcp                            │       │
    │  │  - aws_eks_mcp                                   │       │
    │  │  - cross_account                                 │       │
    │  │  - ... (130+ total)                              │       │
    │  └─────────────────────────────────────────────────┘       │
    │                                                             │
    │  ┌─────────────────────────────────────────────────┐       │
    │  │  EventBridge                                     │       │
    │  │  - CloudWatch Alarm → API Destination            │       │
    │  │  - Triggers automated incident RCA               │       │
    │  └─────────────────────────────────────────────────┘       │
    └─────────────────────────────────────────────────────────────┘
```

## Data Flow

### Agent Invocation Flow

1. User sends message via Chat UI
2. Frontend calls `POST /api/sessions/{id}/invoke`
3. Platform API creates AgentCore `InvokeAgent` request
4. AgentCore pulls container image from ECR
5. Agent runtime (Strands SDK) processes the request
6. Agent connects to MCP Gateways for tool access
7. Side-channel events stream back via DynamoDB
8. Platform API streams SSE events to Frontend

### Automated Incident Flow

1. CloudWatch Alarm transitions to ALARM state
2. EventBridge rule captures the state change event
3. EventBridge sends event to Platform API via API Destination
4. Platform API triggers the Incident RCA agent
5. Agent performs root cause analysis using MCP tools
6. Report generated and saved to S3

## Security

### Network Security
- ALB only accepts traffic from CloudFront (managed prefix list)
- ECS tasks run in private subnets with NAT Gateway
- VPC Endpoints for DynamoDB, S3, Bedrock (no internet traversal)
- X-CloudFront-Secret header prevents direct ALB access

### Authentication
- Cognito User Pool with email-based authentication
- ALB HTTPS listener enforces Cognito auth
- OAuth2 Authorization Code flow with PKCE

### IAM
- Least-privilege IAM roles per component
- AgentCore runtime role scoped to required services
- Platform API task role with DynamoDB, Bedrock, and PassRole permissions

## Terraform Module Boundaries

| Module | Resources | Outputs Used By |
|--------|-----------|-----------------|
| network | VPC, Subnets, NAT, VPC Endpoints | compute |
| data | DynamoDB tables, S3 bucket | iam, compute, cdn |
| auth | Cognito Pool, Client, Domain | compute |
| registry | ECR repositories | compute |
| iam | IAM roles and policies | compute |
| compute | ECS, ALB, EventBridge | cdn |
| cdn | CloudFront, Route53, S3 Policy | — |
