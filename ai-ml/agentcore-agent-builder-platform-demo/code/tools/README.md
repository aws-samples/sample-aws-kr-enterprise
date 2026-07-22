# MCP Lambda Tools

This directory contains 19 MCP tool implementations deployed as AWS Lambda functions,
organized into 8 AgentCore MCP Gateways by domain. Each handler exposes a `lambda_handler`
entry point and is registered as one or more Gateway targets.

## Dependencies

No external dependencies — pure `boto3` (bundled in the Lambda runtime) plus the Python
standard library (e.g. `html`, `shlex`, `json`, `urllib`). No `pip install` / build step is
required; the deploy script zips `<handler>.py` + `cross_account.py` directly.

## Tools by Gateway

### network
| File | Lambda function | Description |
|------|-----------------|-------------|
| `network_mcp.py` | `awsops-network-mcp` | VPC config, subnets, SG/NACL/route tables, Transit Gateways, Network Firewalls |
| `reachability.py` | `awsops-reachability-analyzer` | VPC Reachability Analyzer path analysis (stub) |
| `flowmonitor.py` | `awsops-flow-monitor` | VPC Flow Monitor metrics (stub) |

### container
| File | Lambda function | Description |
|------|-----------------|-------------|
| `aws_eks_mcp.py` | `awsops-eks-mcp` | EKS cluster status, pod health, node capacity |
| `aws_ecs_mcp.py` | `awsops-ecs-mcp` | ECS clusters, services, tasks, and deployments |

### iac
| File | Lambda function | Description |
|------|-----------------|-------------|
| `aws_iac_mcp.py` | `awsops-iac-mcp` | Infrastructure-as-Code documentation and guidance |
| `aws_terraform_mcp.py` | `awsops-terraform-mcp` | Terraform module/provider lookups and best practices |

### data
| File | Lambda function | Description |
|------|-----------------|-------------|
| `aws_dynamodb_mcp.py` | `awsops-dynamodb-mcp` | DynamoDB tables, queries, data modeling, cost estimation |
| `aws_rds_mcp.py` | `awsops-rds-mcp` | RDS instances/clusters and read-only SQL via RDS Data API |
| `aws_valkey_mcp.py` | `awsops-valkey-mcp` | ElastiCache Valkey/Redis cluster inspection |
| `aws_msk_mcp.py` | `awsops-msk-mcp` | MSK (Kafka) cluster and configuration inspection |

### security
| File | Lambda function | Description |
|------|-----------------|-------------|
| `aws_iam_mcp.py` | `awsops-iam-mcp` | IAM roles/policies, access analysis, account security summary |

### monitoring
| File | Lambda function | Description |
|------|-----------------|-------------|
| `aws_cloudwatch_mcp.py` | `awsops-cloudwatch-mcp` | CloudWatch metrics, alarms, and Log Insights queries |
| `aws_cloudtrail_mcp.py` | `awsops-cloudtrail-mcp` | CloudTrail event lookup and CloudTrail Lake SQL queries |
| `datasource_diag_mcp.py` | `awsops-datasource-diag-mcp` | Datasource connectivity diagnostics — URL, DNS, NLB, SG, network path, HTTP |

### cost
| File | Lambda function | Description |
|------|-----------------|-------------|
| `aws_cost_mcp.py` | `awsops-cost-mcp` | Cost Explorer usage, comparisons, drivers, and forecasts |
| `aws_finops_mcp.py` | `awsops-finops-mcp` | FinOps analysis — Cost Optimization Hub and Trusted Advisor cost checks |

### ops
| File | Lambda function | Description |
|------|-----------------|-------------|
| `aws_knowledge.py` | `awsops-aws-knowledge` | AWS documentation search and knowledge retrieval |
| `aws_core_mcp.py` | `awsops-core-mcp` | Core operations — generic read-only `call_aws` via boto3 |

## Shared Helper

`cross_account.py` is a shared helper (not a Lambda function itself) imported by the handlers
to perform cross-account AWS resource access via STS `AssumeRole` into `AWSopsReadOnlyRole`.
It is packaged into every function's deployment zip.

## Gateway Integration

Each Lambda tool is registered as a target in an AgentCore MCP Gateway.
See `scripts/register-gateway-targets.py` for the registration pattern and per-tool schemas,
and `scripts/deploy-lambda-tools.sh` for packaging and deployment.

## Adding New Tools

1. Create a new Python file following the pattern in existing handlers (define `lambda_handler`).
2. Add it to `scripts/deploy-lambda-tools.sh` and deploy as a Lambda function.
3. Register as a Gateway target using `scripts/register-gateway-targets.py`.
