#!/bin/bash
# scripts/seed-dynamodb.sh
# DynamoDB initial data seeding - Populates agent configs, gateway catalog, sample incidents
set -euo pipefail

if [ -z "${AWS_REGION:-}" ]; then
    echo "ERROR: AWS_REGION is not set. export AWS_REGION first." >&2
    exit 1
fi
REGION="$AWS_REGION"
: "${PROJECT_PREFIX:=aiops-v2-dev}"

# Resolve project root so prompt files load regardless of the caller's cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"
TABLE="${DYNAMODB_TABLE:-${PROJECT_PREFIX}-platform}"
INC_TABLE="${INCIDENTS_TABLE:-${PROJECT_PREFIX}-incidents}"

MONITORING_GW="${MONITORING_GW_ID:-awsops-monitoring-gateway}"
CONTAINER_GW="${CONTAINER_GW_ID:-awsops-container-gateway}"
DATA_GW="${DATA_GW_ID:-awsops-data-gateway}"
COST_GW="${COST_GW_ID:-awsops-cost-gateway}"

echo "=== Seeding DynamoDB tables ==="
echo "  Region: $REGION"
echo "  Gateways: monitoring=$MONITORING_GW container=$CONTAINER_GW data=$DATA_GW cost=$COST_GW"

# --- Platform Policy ---
echo "1. Platform Policy"
aws dynamodb put-item --table-name "$TABLE" --region "$REGION" --item '{
  "PK": {"S": "PLATFORM"},
  "SK": {"S": "POLICY"},
  "rateLimits": {"M": {"maxRequestsPerMinute": {"N": "60"}}},
  "costGuards": {"M": {"maxTokensPerSession": {"N": "100000"}}},
  "permissionMatrix": {"M": {}},
  "maxDelegationDepth": {"N": "2"}
}'

# --- Supervisor Config ---
echo "2. Supervisor Config"
SUPERVISOR_PROMPT=$(cat code/agent-runtime/prompts/supervisor.md | python3 -c "import sys,json; print(json.dumps(sys.stdin.read())[1:-1])")
aws dynamodb put-item --table-name "$TABLE" --region "$REGION" --item "$(cat <<ITEM
{
  "PK": {"S": "AGENT#supervisor-001"},
  "SK": {"S": "CONFIG"},
  "agentId": {"S": "supervisor-001"},
  "name": {"S": "Supervisor"},
  "contextBoundary": {"S": "Route user requests to the most appropriate domain agent"},
  "model": {"S": "global.anthropic.claude-sonnet-4-6"},
  "systemPrompt": {"S": "${SUPERVISOR_PROMPT}"},
  "gateways": {"L": []},
  "delegations": {"L": []},
  "internalTools": {"L": [
    {"M": {"name": {"S": "load_agent_registry"}, "description": {"S": "Load active Agent Card list from DynamoDB"}, "type": {"S": "dynamodb_query"}, "fixedPK": {"S": "SUPERVISOR"}}},
    {"M": {"name": {"S": "invoke_domain_agent"}, "description": {"S": "Invoke selected Domain Agent Runtime"}, "type": {"S": "agent_invoke"}}}
  ]},
  "harness": {"M": {
    "preHooks": {"L": [{"S": "scope-validation"}]},
    "postHooks": {"L": [{"S": "note-taking"}]},
    "hitlActions": {"L": []},
    "evaluator": {"M": {"enabled": {"BOOL": false}}}
  }},
  "triggers": {"L": [{"M": {"type": {"S": "chat"}}}]},
  "createdBy": {"S": "platform"},
  "version": {"N": "1"}
}
ITEM
)"

# --- Incident Agent Config ---
echo "3. Incident Agent Config"
INCIDENT_PROMPT=$(cat code/agent-runtime/prompts/incident.md | python3 -c "import sys,json; print(json.dumps(sys.stdin.read())[1:-1])")
aws dynamodb put-item --table-name "$TABLE" --region "$REGION" --item "$(cat <<ITEM
{
  "PK": {"S": "AGENT#incident-agent-001"},
  "SK": {"S": "CONFIG"},
  "agentId": {"S": "incident-agent-001"},
  "name": {"S": "Incident Agent"},
  "contextBoundary": {"S": "Incident management — create, track, and analyze incidents"},
  "model": {"S": "global.anthropic.claude-sonnet-4-6"},
  "systemPrompt": {"S": "${INCIDENT_PROMPT}"},
  "gateways": {"L": [
    {"M": {"gatewayId": {"S": "$MONITORING_GW"}, "toolFilter": {"L": [{"S": "get_active_alarms"}, {"S": "get_alarm_history"}, {"S": "analyze_log_group"}]}}}
  ]},
  "delegations": {"L": []},
  "internalTools": {"L": [
    {"M": {"name": {"S": "list_incidents"}, "description": {"S": "List incidents"}, "type": {"S": "dynamodb_query"}, "table": {"S": "${PROJECT_PREFIX}-incidents"}}},
    {"M": {"name": {"S": "get_incident_detail"}, "description": {"S": "Get incident detail"}, "type": {"S": "dynamodb_get"}, "table": {"S": "${PROJECT_PREFIX}-incidents"}}},
    {"M": {"name": {"S": "create_incident"}, "description": {"S": "Create new incident"}, "type": {"S": "dynamodb_put"}, "table": {"S": "${PROJECT_PREFIX}-incidents"}}},
    {"M": {"name": {"S": "get_similar_incidents"}, "description": {"S": "Search similar incidents"}, "type": {"S": "dynamodb_query"}, "table": {"S": "${PROJECT_PREFIX}-incidents"}}}
  ]},
  "harness": {"M": {
    "preHooks": {"L": [{"S": "scope-validation"}, {"S": "persona-injection"}]},
    "postHooks": {"L": [{"S": "evaluator"}]},
    "hitlActions": {"L": [{"S": "create_incident"}]},
    "evaluator": {"M": {"enabled": {"BOOL": true}}}
  }},
  "triggers": {"L": [{"M": {"type": {"S": "chat"}}}, {"M": {"type": {"S": "event"}, "source": {"S": "aws.cloudwatch"}}}]},
  "createdBy": {"S": "platform"},
  "version": {"N": "1"}
}
ITEM
)"

# --- Observability Agent Config ---
echo "4. Observability Agent Config"
OBS_PROMPT=$(cat code/agent-runtime/prompts/observability.md | python3 -c "import sys,json; print(json.dumps(sys.stdin.read())[1:-1])")
aws dynamodb put-item --table-name "$TABLE" --region "$REGION" --item "$(cat <<ITEM
{
  "PK": {"S": "AGENT#observability-agent-001"},
  "SK": {"S": "CONFIG"},
  "agentId": {"S": "observability-agent-001"},
  "name": {"S": "Observability Agent"},
  "contextBoundary": {"S": "Cloud resource observability — metrics, logs, alarms, audit trail, service discovery"},
  "model": {"S": "global.anthropic.claude-sonnet-4-6"},
  "systemPrompt": {"S": "${OBS_PROMPT}"},
  "gateways": {"L": [
    {"M": {"gatewayId": {"S": "$MONITORING_GW"}, "toolFilter": {"S": "all"}}},
    {"M": {"gatewayId": {"S": "$CONTAINER_GW"}, "toolFilter": {"L": [{"S": "get_cloudwatch_metrics"}, {"S": "get_eks_metrics_guidance"}, {"S": "list_eks_clusters"}]}}}
  ]},
  "delegations": {"L": []},
  "internalTools": {"L": []},
  "harness": {"M": {
    "preHooks": {"L": [{"S": "scope-validation"}, {"S": "persona-injection"}]},
    "postHooks": {"L": [{"S": "evaluator"}, {"S": "note-taking"}]},
    "hitlActions": {"L": []},
    "evaluator": {"M": {"enabled": {"BOOL": true}, "criteria": {"S": "accuracy,completeness"}}}
  }},
  "triggers": {"L": [{"M": {"type": {"S": "chat"}}}, {"M": {"type": {"S": "schedule"}, "cron": {"S": "0 9 * * *"}}}]},
  "createdBy": {"S": "platform"},
  "version": {"N": "1"}
}
ITEM
)"

# --- RCA Agent Config ---
echo "5. RCA Agent Config"
RCA_PROMPT=$(cat code/agent-runtime/prompts/rca.md | python3 -c "import sys,json; print(json.dumps(sys.stdin.read())[1:-1])")
aws dynamodb put-item --table-name "$TABLE" --region "$REGION" --item "$(cat <<ITEM
{
  "PK": {"S": "AGENT#rca-agent-001"},
  "SK": {"S": "CONFIG"},
  "agentId": {"S": "rca-agent-001"},
  "name": {"S": "RCA Agent"},
  "contextBoundary": {"S": "Root cause analysis for cloud service incidents"},
  "model": {"S": "global.anthropic.claude-sonnet-4-6"},
  "systemPrompt": {"S": "${RCA_PROMPT}"},
  "gateways": {"L": [
    {"M": {"gatewayId": {"S": "$MONITORING_GW"}, "toolFilter": {"L": [{"S": "get_metric_data"}, {"S": "get_active_alarms"}, {"S": "analyze_log_group"}, {"S": "execute_log_insights_query"}]}}}
  ]},
  "delegations": {"L": [
    {"M": {"purpose": {"S": "Query incident history"}, "timeout": {"N": "60"}, "targetAgent": {"S": "incident-agent-001"}, "scope": {"L": [{"S": "list_incidents"}, {"S": "get_similar_incidents"}]}}},
    {"M": {"purpose": {"S": "Check CloudTrail change history"}, "timeout": {"N": "60"}, "targetAgent": {"S": "observability-agent-001"}, "scope": {"L": [{"S": "lookup_events"}]}}},
    {"M": {"purpose": {"S": "Generate RCA report HTML"}, "timeout": {"N": "90"}, "targetAgent": {"S": "report-agent-001"}, "scope": {"L": [{"S": "render_report"}]}}}
  ]},
  "internalTools": {"L": []},
  "harness": {"M": {
    "preHooks": {"L": [{"S": "scope-validation"}, {"S": "persona-injection"}]},
    "postHooks": {"L": [{"S": "evaluator"}, {"S": "note-taking"}]},
    "hitlActions": {"L": []},
    "evaluator": {"M": {"enabled": {"BOOL": true}, "criteria": {"S": "accuracy,completeness,safety"}}}
  }},
  "triggers": {"L": [{"M": {"type": {"S": "chat"}}}]},
  "createdBy": {"S": "platform"},
  "version": {"N": "1"}
}
ITEM
)"

# --- Report Agent Config ---
echo "6. Report Agent Config"
REPORT_PROMPT=$(cat code/agent-runtime/prompts/report.md | python3 -c "import sys,json; print(json.dumps(sys.stdin.read())[1:-1])")
aws dynamodb put-item --table-name "$TABLE" --region "$REGION" --item "$(cat <<ITEM
{
  "PK": {"S": "AGENT#report-agent-001"},
  "SK": {"S": "CONFIG"},
  "agentId": {"S": "report-agent-001"},
  "name": {"S": "Report Generator Agent"},
  "contextBoundary": {"S": "Generate HTML/CSS reports from structured analysis data"},
  "model": {"S": "global.anthropic.claude-sonnet-4-6"},
  "maxTokens": {"N": "32768"},
  "systemPrompt": {"S": "${REPORT_PROMPT}"},
  "gateways": {"L": []},
  "delegations": {"L": []},
  "internalTools": {"L": [
    {"M": {"name": {"S": "publish_report"}, "description": {"S": "Render structured report data to HTML and publish to S3; returns the shareable CloudFront URL"}, "type": {"S": "python_function"}, "module": {"S": "report_tools.s3_uploader"}}}
  ]},
  "harness": {"M": {
    "preHooks": {"L": [{"S": "scope-validation"}]},
    "postHooks": {"L": [{"S": "note-taking"}]},
    "hitlActions": {"L": []},
    "evaluator": {"M": {"enabled": {"BOOL": false}}}
  }},
  "triggers": {"L": []},
  "createdBy": {"S": "platform"},
  "version": {"N": "1"},
  "metadata": {"M": {"supportedReportTypes": {"L": [{"S": "rca"}, {"S": "incident"}, {"S": "health-check"}, {"S": "daily-summary"}, {"S": "security-audit"}]}}}
}
ITEM
)"

# --- Data Agent Config ---
echo "7. Data Agent Config"
DATA_PROMPT=$(cat code/agent-runtime/prompts/data.md | python3 -c "import sys,json; print(json.dumps(sys.stdin.read())[1:-1])")
aws dynamodb put-item --table-name "$TABLE" --region "$REGION" --item "$(cat <<ITEM
{
  "PK": {"S": "AGENT#data-agent-001"},
  "SK": {"S": "CONFIG"},
  "agentId": {"S": "data-agent-001"},
  "name": {"S": "Data Agent"},
  "contextBoundary": {"S": "Database management and monitoring — DynamoDB, RDS/Aurora, ElastiCache/Valkey, MSK Kafka"},
  "model": {"S": "global.anthropic.claude-sonnet-4-6"},
  "systemPrompt": {"S": "${DATA_PROMPT}"},
  "gateways": {"L": [
    {"M": {"gatewayId": {"S": "$DATA_GW"}, "toolFilter": {"S": "all"}}}
  ]},
  "delegations": {"L": []},
  "internalTools": {"L": []},
  "harness": {"M": {
    "preHooks": {"L": [{"S": "scope-validation"}, {"S": "persona-injection"}]},
    "postHooks": {"L": [{"S": "evaluator"}]},
    "hitlActions": {"L": []},
    "evaluator": {"M": {"enabled": {"BOOL": true}}}
  }},
  "triggers": {"L": [{"M": {"type": {"S": "chat"}}}]},
  "createdBy": {"S": "platform"},
  "version": {"N": "1"}
}
ITEM
)"

# --- Cost Agent Config ---
echo "8. Cost Agent Config"
COST_PROMPT=$(cat code/agent-runtime/prompts/cost.md | python3 -c "import sys,json; print(json.dumps(sys.stdin.read())[1:-1])")
aws dynamodb put-item --table-name "$TABLE" --region "$REGION" --item "$(cat <<ITEM
{
  "PK": {"S": "AGENT#cost-agent-001"},
  "SK": {"S": "CONFIG"},
  "agentId": {"S": "cost-agent-001"},
  "name": {"S": "Cost Agent"},
  "contextBoundary": {"S": "AWS cost analysis, forecasting, RI/SP recommendations, FinOps optimization"},
  "model": {"S": "global.anthropic.claude-sonnet-4-6"},
  "systemPrompt": {"S": "${COST_PROMPT}"},
  "gateways": {"L": [
    {"M": {"gatewayId": {"S": "$COST_GW"}, "toolFilter": {"S": "all"}}}
  ]},
  "delegations": {"L": []},
  "internalTools": {"L": []},
  "harness": {"M": {
    "preHooks": {"L": [{"S": "scope-validation"}, {"S": "persona-injection"}]},
    "postHooks": {"L": [{"S": "evaluator"}]},
    "hitlActions": {"L": []},
    "evaluator": {"M": {"enabled": {"BOOL": true}}}
  }},
  "triggers": {"L": [{"M": {"type": {"S": "chat"}}}]},
  "createdBy": {"S": "platform"},
  "version": {"N": "1"}
}
ITEM
)"

# --- Agent Cards ---
echo "9. Agent Cards"
for AGENT in supervisor-001 incident-agent-001 observability-agent-001 rca-agent-001 report-agent-001 data-agent-001 cost-agent-001; do
  NAME=$(aws dynamodb get-item --table-name "$TABLE" --region "$REGION" --key "{\"PK\":{\"S\":\"AGENT#${AGENT}\"},\"SK\":{\"S\":\"CONFIG\"}}" --query 'Item.name.S' --output text)
  BOUNDARY=$(aws dynamodb get-item --table-name "$TABLE" --region "$REGION" --key "{\"PK\":{\"S\":\"AGENT#${AGENT}\"},\"SK\":{\"S\":\"CONFIG\"}}" --query 'Item.contextBoundary.S' --output text)

  aws dynamodb put-item --table-name "$TABLE" --region "$REGION" --item "{
    \"PK\": {\"S\": \"AGENT#${AGENT}\"},
    \"SK\": {\"S\": \"CARD\"},
    \"name\": {\"S\": \"${NAME}\"},
    \"description\": {\"S\": \"${BOUNDARY}\"},
    \"capabilities\": {\"L\": []},
    \"status\": {\"S\": \"active\"},
    \"delegatesTo\": {\"L\": []},
    \"contextBoundary\": {\"S\": \"${BOUNDARY}\"}
  }"
  echo "  Card: $AGENT"
done

# --- Supervisor Registry ---
echo "10. Supervisor Registry"
for AGENT in incident-agent-001 observability-agent-001 rca-agent-001 report-agent-001 data-agent-001 cost-agent-001; do
  NAME=$(aws dynamodb get-item --table-name "$TABLE" --region "$REGION" --key "{\"PK\":{\"S\":\"AGENT#${AGENT}\"},\"SK\":{\"S\":\"CONFIG\"}}" --query 'Item.name.S' --output text)
  BOUNDARY=$(aws dynamodb get-item --table-name "$TABLE" --region "$REGION" --key "{\"PK\":{\"S\":\"AGENT#${AGENT}\"},\"SK\":{\"S\":\"CONFIG\"}}" --query 'Item.contextBoundary.S' --output text)
  RT_ARN=$(aws dynamodb get-item --table-name "$TABLE" --region "$REGION" --key "{\"PK\":{\"S\":\"AGENT#${AGENT}\"},\"SK\":{\"S\":\"RUNTIME\"}}" --query 'Item.runtimeArn.S' --output text 2>/dev/null || echo "")

  aws dynamodb put-item --table-name "$TABLE" --region "$REGION" --item "{
    \"PK\": {\"S\": \"SUPERVISOR\"},
    \"SK\": {\"S\": \"AGENT#${AGENT}\"},
    \"name\": {\"S\": \"${NAME}\"},
    \"contextBoundary\": {\"S\": \"${BOUNDARY}\"},
    \"capabilities\": {\"L\": []},
    \"runtimeArn\": {\"S\": \"${RT_ARN}\"}
  }"
  echo "  Registry: $AGENT"
done

# --- Sample Incidents (for demo) ---
echo "11. Sample Incidents"
aws dynamodb put-item --table-name "$INC_TABLE" --region "$REGION" --item '{
  "PK": {"S": "INC#INC-042"},
  "SK": {"S": "META"},
  "title": {"S": "CPU spike on ECS service"},
  "severity": {"S": "medium"},
  "status": {"S": "resolved"},
  "service": {"S": "foodorder-api"},
  "createdAt": {"S": "2026-04-21T10:30:00Z"},
  "resolvedAt": {"S": "2026-04-21T11:15:00Z"}
}'

aws dynamodb put-item --table-name "$INC_TABLE" --region "$REGION" --item '{
  "PK": {"S": "INC#INC-039"},
  "SK": {"S": "META"},
  "title": {"S": "Memory leak in worker pods"},
  "severity": {"S": "high"},
  "status": {"S": "resolved"},
  "service": {"S": "foodorder-worker"},
  "createdAt": {"S": "2026-04-10T14:00:00Z"},
  "resolvedAt": {"S": "2026-04-10T16:30:00Z"}
}'

# --- Gateway Catalog ---
echo "12. Gateway Catalog"
for GW_ENTRY in \
  "${MONITORING_GW}:Monitoring Gateway:CloudWatch metrics/logs/alarms + CloudTrail events:observability:16" \
  "${CONTAINER_GW}:Container Gateway:EKS + ECS cluster management:container:12" \
  "${DATA_GW}:Data Gateway:DynamoDB, RDS, ElastiCache/Valkey, MSK:data:24" \
  "${COST_GW}:Cost Gateway:Cost Explorer, Pricing, Budgets, FinOps:cost:14"; do
  GW_ID="${GW_ENTRY%%:*}"
  REST="${GW_ENTRY#*:}"
  GW_NAME="${REST%%:*}"
  REST="${REST#*:}"
  GW_DESC="${REST%%:*}"
  REST="${REST#*:}"
  GW_DOMAIN="${REST%%:*}"
  GW_TOOLS="${REST##*:}"
  aws dynamodb put-item --table-name "$TABLE" --region "$REGION" --item "{
    \"PK\": {\"S\": \"GATEWAY#${GW_ID}\"},
    \"SK\": {\"S\": \"CONFIG\"},
    \"gatewayId\": {\"S\": \"${GW_ID}\"},
    \"gatewayName\": {\"S\": \"${GW_NAME}\"},
    \"description\": {\"S\": \"${GW_DESC}\"},
    \"domain\": {\"S\": \"${GW_DOMAIN}\"},
    \"toolCount\": {\"N\": \"${GW_TOOLS}\"}
  }"
  echo "  Gateway: $GW_NAME ($GW_ID)"
done

echo "=== Seed complete ==="
