#!/bin/bash
# scripts/deploy-gateways.sh — Create 8 AgentCore MCP Gateways
# Idempotent: skips existing gateways
set -euo pipefail

if [ -z "${AWS_REGION:-}" ]; then
    echo "ERROR: AWS_REGION is not set. export AWS_REGION first." >&2
    exit 1
fi
REGION="$AWS_REGION"
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"

# Verify or create AWSopsAgentCoreRole
ROLE_ARN=$(aws iam get-role --role-name AWSopsAgentCoreRole --query "Role.Arn" --output text 2>/dev/null || echo "")
if [ -z "$ROLE_ARN" ] || [ "$ROLE_ARN" = "None" ]; then
    echo "Creating AWSopsAgentCoreRole..."
    aws iam create-role --role-name AWSopsAgentCoreRole \
        --assume-role-policy-document '{
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Principal": {"Service": "bedrock.amazonaws.com"}, "Action": "sts:AssumeRole"},
                {"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole"}
            ]
        }' 2>/dev/null || true
    # Least-privilege inline policy (scoped actions, no service wildcards).
    # Assumed by AgentCore Gateways to: invoke Bedrock models, pull ECR images
    # for MCP Lambda targets, invoke MCP Lambda functions, and manage gateways/runtimes.
    aws iam put-role-policy --role-name AWSopsAgentCoreRole --policy-name AWSopsGatewayLeastPriv \
        --policy-document '{
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "BedrockModelInvoke",
                    "Effect": "Allow",
                    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                    "Resource": [
                        "arn:aws:bedrock:*::foundation-model/*",
                        "arn:aws:bedrock:*:*:inference-profile/*"
                    ]
                },
                {
                    "Sid": "EcrImagePull",
                    "Effect": "Allow",
                    "Action": [
                        "ecr:GetAuthorizationToken",
                        "ecr:BatchGetImage",
                        "ecr:GetDownloadUrlForLayer",
                        "ecr:BatchCheckLayerAvailability"
                    ],
                    "Resource": "*"
                },
                {
                    "Sid": "LambdaMcpTargets",
                    "Effect": "Allow",
                    "Action": ["lambda:InvokeFunction", "lambda:GetFunction"],
                    "Resource": "arn:aws:lambda:*:*:function:awsops-*"
                },
                {
                    "Sid": "AgentCoreGatewayRuntime",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:InvokeAgentRuntime",
                        "bedrock-agentcore:GetAgentRuntime",
                        "bedrock-agentcore:CreateGateway",
                        "bedrock-agentcore:ListGateways",
                        "bedrock-agentcore:GetGateway",
                        "bedrock-agentcore:CreateGatewayTarget",
                        "bedrock-agentcore:ListGatewayTargets"
                    ],
                    "Resource": "*"
                }
            ]
        }'
    ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/AWSopsAgentCoreRole"
    echo "Waiting for IAM propagation (10s)..."
    sleep 10
fi

echo "=== Creating 8 Gateways ==="
GATEWAYS=(
    "awsops-network-gateway:Network - VPC, TGW, VPN, ENI, Firewall, Reachability, Flow Logs"
    "awsops-container-gateway:Container - EKS, ECS"
    "awsops-iac-gateway:IaC - CloudFormation, CDK, Terraform"
    "awsops-data-gateway:Data - DynamoDB, RDS, ElastiCache, MSK"
    "awsops-security-gateway:Security - IAM users, roles, policies, simulation"
    "awsops-monitoring-gateway:Monitoring - CloudWatch metrics/alarms/logs, CloudTrail"
    "awsops-cost-gateway:Cost - Cost Explorer, Pricing, Budgets, FinOps"
    "awsops-ops-gateway:Ops - AWS Knowledge, Core MCP"
)

for entry in "${GATEWAYS[@]}"; do
    GW_NAME="${entry%%:*}"
    GW_DESC="${entry##*:}"
    EXISTING=$(aws bedrock-agentcore-control list-gateways --region "$REGION" --output json 2>/dev/null | \
        python3 -c "import json,sys;gws=json.load(sys.stdin).get('items',[]); print(next((g['gatewayId'] for g in gws if g.get('name','')=='$GW_NAME'), ''))" 2>/dev/null || echo "")
    if [ -n "$EXISTING" ] && [ "$EXISTING" != "" ]; then
        echo "  EXISTS: $GW_NAME ($EXISTING)"
    else
        RESULT=$(aws bedrock-agentcore-control create-gateway \
            --name "$GW_NAME" --role-arn "$ROLE_ARN" \
            --protocol-type MCP --authorizer-type NONE \
            --description "$GW_DESC" \
            --region "$REGION" --output json 2>&1)
        GW_ID=$(echo "$RESULT" | python3 -c "import json,sys;print(json.load(sys.stdin).get('gatewayId',''))" 2>/dev/null || echo "")
        echo "  CREATED: $GW_NAME ($GW_ID)"
    fi
done

echo ""
echo "=== Gateway Status ==="
aws bedrock-agentcore-control list-gateways --region "$REGION" --output json 2>/dev/null | \
    python3 -c "
import json,sys
gws=json.load(sys.stdin).get('items',[])
for g in sorted(gws, key=lambda x: x.get('name','')):
    if 'awsops' in g.get('name',''):
        print('  {} [{}] {}'.format(g['name'], g['status'], g['gatewayId']))
"
