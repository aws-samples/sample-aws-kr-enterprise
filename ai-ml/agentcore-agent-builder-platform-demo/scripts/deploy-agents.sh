#!/bin/bash
# scripts/deploy-agents.sh
# AgentCore Runtime deployment - Creates agent runtimes on AgentCore
set -euo pipefail

if [ -z "${AWS_REGION:-}" ]; then
    echo "ERROR: AWS_REGION is not set. export AWS_REGION=ap-northeast-2 first." >&2
    exit 1
fi
REGION="$AWS_REGION"
: "${PROJECT_PREFIX:=aiops-v2-dev}"
TABLE="${DYNAMODB_TABLE:-${PROJECT_PREFIX}-platform}"
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
BASE_IMAGE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${PROJECT_PREFIX}/base-image:latest"
REPORT_IMAGE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${PROJECT_PREFIX}/report-image:latest"
ROLE_ARN="${AGENTCORE_ROLE_ARN:-arn:aws:iam::${ACCOUNT_ID}:role/${PROJECT_PREFIX}-agentcore-runtime}"
INCIDENTS_TABLE="${INCIDENTS_TABLE:-${PROJECT_PREFIX}-incidents}"

# Report agent runtime needs S3/CloudFront targets to write reports. Prefer
# values already exported (by deploy-all.sh Phase 1); otherwise pull from
# terraform outputs. Without these, report generation raises a clear runtime error.
TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../iac/envs/dev" && pwd)"
REPORT_BUCKET="${S3_BUCKET:-${REPORT_BUCKET:-$(cd "$TF_DIR" && terraform output -raw s3_reports_bucket 2>/dev/null || echo "")}}"
REPORT_CF_DOMAIN="${REPORT_CF_DOMAIN:-$(cd "$TF_DIR" && terraform output -raw reports_distribution_domain 2>/dev/null || echo "")}"

# agent-id:runtime-name pairs
AGENTS=(
    "supervisor-001:supervisor001"
    "incident-agent-001:incidentagent001"
    "observability-agent-001:observabilityagent001"
    "rca-agent-001:rcaagent001"
    "data-agent-001:dataagent001"
    "cost-agent-001:costagent001"
)
REPORT_AGENTS=("report-agent-001:reportagent001")

# Build the environment-variables JSON for a given agent id. Includes the
# report S3/CloudFront targets (empty string if unresolved — the runtime then
# surfaces a clear error instead of crashing on import).
build_env() {
  local aid="$1"
  cat <<ENVJSON
{"AGENT_ID":"${aid}","DYNAMODB_TABLE":"${TABLE}","INCIDENTS_TABLE":"${INCIDENTS_TABLE}","AWS_REGION":"${REGION}","AGENT_OBSERVABILITY_ENABLED":"true","REPORT_BUCKET":"${REPORT_BUCKET}","REPORT_CF_DOMAIN":"${REPORT_CF_DOMAIN}"}
ENVJSON
}

echo "=== Deploying Agent Runtimes ==="
echo "  Region: $REGION | Image: $BASE_IMAGE"
echo "  Report target: bucket=${REPORT_BUCKET:-<unset>} cf=${REPORT_CF_DOMAIN:-<unset>}"

for entry in "${AGENTS[@]}"; do
  AGENT_ID="${entry%%:*}"
  RUNTIME_NAME="${entry##*:}"
  echo "Deploying: $AGENT_ID → $RUNTIME_NAME (base image)"

  RUNTIME_ARN=$(aws bedrock-agentcore-control create-agent-runtime \
    --agent-runtime-name "$RUNTIME_NAME" \
    --role-arn "$ROLE_ARN" \
    --network-configuration '{"networkMode":"PUBLIC"}' \
    --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"${BASE_IMAGE}\"}}" \
    --environment-variables "$(build_env "$AGENT_ID")" \
    --region "$REGION" \
    --query 'agentRuntimeArn' --output text 2>/dev/null || echo "ALREADY_EXISTS")

  if [ "$RUNTIME_ARN" != "ALREADY_EXISTS" ]; then
    echo "  Created: $RUNTIME_ARN"
    aws dynamodb put-item --table-name "$TABLE" --region "$REGION" --item "{
      \"PK\": {\"S\": \"AGENT#${AGENT_ID}\"},
      \"SK\": {\"S\": \"RUNTIME\"},
      \"runtimeArn\": {\"S\": \"${RUNTIME_ARN}\"},
      \"status\": {\"S\": \"provisioning\"},
      \"createdAt\": {\"S\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"},
      \"version\": {\"N\": \"1\"}
    }"
    aws dynamodb update-item --table-name "$TABLE" --region "$REGION" \
      --key "{\"PK\":{\"S\":\"SUPERVISOR\"},\"SK\":{\"S\":\"AGENT#${AGENT_ID}\"}}" \
      --update-expression "SET runtimeArn = :arn" \
      --expression-attribute-values "{\":arn\":{\"S\":\"${RUNTIME_ARN}\"}}" 2>/dev/null || true
  else
    echo "  Already exists, skipping"
  fi
done

for entry in "${REPORT_AGENTS[@]}"; do
  AGENT_ID="${entry%%:*}"
  RUNTIME_NAME="${entry##*:}"
  echo "Deploying: $AGENT_ID → $RUNTIME_NAME (report image)"

  RUNTIME_ARN=$(aws bedrock-agentcore-control create-agent-runtime \
    --agent-runtime-name "$RUNTIME_NAME" \
    --role-arn "$ROLE_ARN" \
    --network-configuration '{"networkMode":"PUBLIC"}' \
    --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"${REPORT_IMAGE}\"}}" \
    --environment-variables "$(build_env "$AGENT_ID")" \
    --region "$REGION" \
    --query 'agentRuntimeArn' --output text 2>/dev/null || echo "ALREADY_EXISTS")

  if [ "$RUNTIME_ARN" != "ALREADY_EXISTS" ]; then
    echo "  Created: $RUNTIME_ARN"
    aws dynamodb put-item --table-name "$TABLE" --region "$REGION" --item "{
      \"PK\": {\"S\": \"AGENT#${AGENT_ID}\"},
      \"SK\": {\"S\": \"RUNTIME\"},
      \"runtimeArn\": {\"S\": \"${RUNTIME_ARN}\"},
      \"status\": {\"S\": \"provisioning\"},
      \"createdAt\": {\"S\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"},
      \"version\": {\"N\": \"1\"}
    }"
  fi
done

echo "=== Waiting for runtimes to become READY ==="
for entry in "${AGENTS[@]}" "${REPORT_AGENTS[@]}"; do
  AGENT_ID="${entry%%:*}"
  echo -n "  $AGENT_ID: "
  RUNTIME_ARN=$(aws dynamodb get-item --table-name "$TABLE" --region "$REGION" \
    --key "{\"PK\":{\"S\":\"AGENT#${AGENT_ID}\"},\"SK\":{\"S\":\"RUNTIME\"}}" \
    --query 'Item.runtimeArn.S' --output text 2>/dev/null || echo "")
  if [ -z "$RUNTIME_ARN" ] || [ "$RUNTIME_ARN" = "None" ]; then
    echo "SKIP (no runtime ARN)"
    continue
  fi
  RT_ID=$(echo "$RUNTIME_ARN" | awk -F/ '{print $NF}')
  for i in $(seq 1 30); do
    ACTUAL_STATUS=$(aws bedrock-agentcore-control get-agent-runtime \
      --agent-runtime-id "$RT_ID" --region "$REGION" \
      --query 'status' --output text 2>/dev/null || echo "unknown")
    if [ "$ACTUAL_STATUS" = "READY" ]; then
      aws dynamodb update-item --table-name "$TABLE" --region "$REGION" \
        --key "{\"PK\":{\"S\":\"AGENT#${AGENT_ID}\"},\"SK\":{\"S\":\"RUNTIME\"}}" \
        --update-expression "SET #s = :s" \
        --expression-attribute-names '{"#s":"status"}' \
        --expression-attribute-values '{":s":{"S":"active"}}'
      echo "READY"
      break
    fi
    echo -n "."
    sleep 10
  done
done

echo "=== Deploy complete ==="
