# MCP Lambda Tools (Samples)

This directory contains 3 sample MCP tool implementations deployed as AWS Lambda functions.
The full platform supports 130+ tools organized into 8 Gateways.

## Included Samples

| File | Description |
|------|-------------|
| `aws_cloudwatch_mcp.py` | CloudWatch metrics, alarms, and log insights queries |
| `aws_eks_mcp.py` | EKS cluster status, pod health, node capacity |
| `cross_account.py` | Cross-account AWS resource access via STS AssumeRole |

## Gateway Integration

Each Lambda tool is registered as a target in an AgentCore MCP Gateway.
See `scripts/register-gateway-targets.py` for the registration pattern.

## Adding New Tools

1. Create a new Python file following the pattern in existing samples
2. Deploy as Lambda function
3. Register as Gateway target using the registration script
