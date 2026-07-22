#!/bin/bash
# scripts/deploy-lambda-tools.sh — Deploy 19 Lambda MCP tools
# Idempotent: creates the execution role and functions if absent, updates code otherwise.
# The inline IAM policy lives in docs/iam-policies (source of truth); this script only reads it.
set -euo pipefail

if [ -z "${AWS_REGION:-}" ]; then
    echo "ERROR: AWS_REGION is not set. export AWS_REGION first." >&2
    exit 1
fi
REGION="$AWS_REGION"
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"

# Resolve project root so the IAM policy and tool sources load regardless of the caller's cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LAMBDA_DIR="$PROJECT_ROOT/code/tools"
ROLE_NAME="AWSopsLambdaNetworkRole"
INLINE_POLICY_FILE="$PROJECT_ROOT/docs/iam-policies/awsops-lambda-execution-role.json"

echo "=== Deploying 19 Lambda MCP Tools ==="
echo "  Region: $REGION | Account: $ACCOUNT_ID"
echo "  Source: $LAMBDA_DIR"

# [1/3] IAM Role for Lambda execution.
# Base is the AWS managed ReadOnlyAccess policy; the inline JSON adds only the
# non-read actions ReadOnlyAccess cannot cover. No wildcard policy is hardcoded here.
echo ""
echo "[1/3] IAM Role: $ROLE_NAME"
ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query "Role.Arn" --output text 2>/dev/null || echo "")
if [ -z "$ROLE_ARN" ] || [ "$ROLE_ARN" = "None" ]; then
    if [ ! -f "$INLINE_POLICY_FILE" ]; then
        echo "ERROR: inline policy file not found: $INLINE_POLICY_FILE" >&2
        exit 1
    fi
    echo "  Creating $ROLE_NAME..."
    aws iam create-role --role-name "$ROLE_NAME" \
        --assume-role-policy-document '{
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]
        }' >/dev/null
    aws iam attach-role-policy --role-name "$ROLE_NAME" \
        --policy-arn "arn:aws:iam::aws:policy/ReadOnlyAccess"
    aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name "AWSopsLambdaInline" \
        --policy-document file://"$INLINE_POLICY_FILE"
    ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
    echo "  Waiting for IAM propagation (10s)..."
    sleep 10
else
    echo "  EXISTS: $ROLE_ARN"
fi

# [2/3] Deploy 19 standard Lambda functions (no VPC, pure boto3 + stdlib).
echo ""
echo "[2/3] Deploying 19 Lambda functions..."

LAMBDAS=(
    "awsops-network-mcp:network_mcp"
    "awsops-reachability-analyzer:reachability"
    "awsops-flow-monitor:flowmonitor"
    "awsops-eks-mcp:aws_eks_mcp"
    "awsops-ecs-mcp:aws_ecs_mcp"
    "awsops-iac-mcp:aws_iac_mcp"
    "awsops-terraform-mcp:aws_terraform_mcp"
    "awsops-iam-mcp:aws_iam_mcp"
    "awsops-cloudwatch-mcp:aws_cloudwatch_mcp"
    "awsops-cloudtrail-mcp:aws_cloudtrail_mcp"
    "awsops-cost-mcp:aws_cost_mcp"
    "awsops-finops-mcp:aws_finops_mcp"
    "awsops-aws-knowledge:aws_knowledge"
    "awsops-core-mcp:aws_core_mcp"
    "awsops-dynamodb-mcp:aws_dynamodb_mcp"
    "awsops-rds-mcp:aws_rds_mcp"
    "awsops-valkey-mcp:aws_valkey_mcp"
    "awsops-msk-mcp:aws_msk_mcp"
    "awsops-datasource-diag-mcp:datasource_diag_mcp"
)

for entry in "${LAMBDAS[@]}"; do
    FUNC_NAME="${entry%%:*}"
    HANDLER="${entry##*:}"
    SRC="$LAMBDA_DIR/${HANDLER}.py"

    if [ ! -f "$SRC" ]; then
        echo "  SKIP: $FUNC_NAME (source not found: $SRC)"
        continue
    fi

    # Package: handler.py + cross_account.py
    PKG_DIR=$(mktemp -d)
    ZIP_PATH="$PKG_DIR/${HANDLER}.zip"
    cp "$SRC" "$PKG_DIR/"
    [ -f "$LAMBDA_DIR/cross_account.py" ] && cp "$LAMBDA_DIR/cross_account.py" "$PKG_DIR/"
    ( cd "$PKG_DIR" && zip -j -q "$ZIP_PATH" ./*.py )

    # Create if absent, otherwise update the code.
    aws lambda create-function \
        --function-name "$FUNC_NAME" --runtime python3.12 \
        --handler "${HANDLER}.lambda_handler" \
        --role "$ROLE_ARN" --zip-file "fileb://${ZIP_PATH}" \
        --timeout 60 --memory-size 256 \
        --region "$REGION" >/dev/null 2>&1 || \
    aws lambda update-function-code \
        --function-name "$FUNC_NAME" --zip-file "fileb://${ZIP_PATH}" \
        --region "$REGION" >/dev/null 2>&1

    # Grant AgentCore invoke permission (idempotent).
    aws lambda add-permission --function-name "$FUNC_NAME" \
        --statement-id agentcore-invoke --action lambda:InvokeFunction \
        --principal bedrock-agentcore.amazonaws.com \
        --region "$REGION" >/dev/null 2>&1 || true

    echo "  OK: $FUNC_NAME"
    rm -rf "$PKG_DIR"
done

# [3/3] Summary — count deployed awsops-* functions.
echo ""
echo "[3/3] Summary"
# shellcheck disable=SC2016  # backticks are JMESPath syntax, not shell expansion
DEPLOYED=$(aws lambda list-functions --region "$REGION" \
    --query 'Functions[?starts_with(FunctionName, `awsops-`)].FunctionName' --output json 2>/dev/null | \
    python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
echo "  Deployed awsops-* Lambda functions: $DEPLOYED"
echo "  Done."
