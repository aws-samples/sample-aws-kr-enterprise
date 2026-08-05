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
NETWORK_GW="${NETWORK_GW_ID:-awsops-network-gateway}"
IAC_GW="${IAC_GW_ID:-awsops-iac-gateway}"
SECURITY_GW="${SECURITY_GW_ID:-awsops-security-gateway}"
OPS_GW="${OPS_GW_ID:-awsops-ops-gateway}"

echo "=== Seeding DynamoDB tables ==="
echo "  Region: $REGION"
echo "  Gateways: monitoring=$MONITORING_GW container=$CONTAINER_GW data=$DATA_GW cost=$COST_GW"
echo "            network=$NETWORK_GW iac=$IAC_GW security=$SECURITY_GW ops=$OPS_GW"

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
# All 8 deployed gateways get a CONFIG row so the Builder can see and recommend
# every gateway (previously only 4 were seeded). toolCount matches the number of
# tools registered per gateway by register-gateway-targets.py.
echo "12. Gateway Catalog"
for GW_ENTRY in \
  "${MONITORING_GW}:Monitoring Gateway:CloudWatch metrics/logs/alarms + CloudTrail events + datasource diagnostics:observability:24" \
  "${CONTAINER_GW}:Container Gateway:EKS + ECS cluster management:container:12" \
  "${DATA_GW}:Data Gateway:DynamoDB, RDS, ElastiCache/Valkey, MSK:data:24" \
  "${COST_GW}:Cost Gateway:Cost Explorer, Pricing, Budgets, FinOps:cost:14" \
  "${NETWORK_GW}:Network Gateway:VPC, TGW, VPN, ENI, Firewall, Flow Logs, Reachability:network:17" \
  "${IAC_GW}:IaC Gateway:CloudFormation/CDK/Terraform validation, troubleshooting, docs:iac:12" \
  "${SECURITY_GW}:Security Gateway:IAM users, roles, groups, policies, policy simulation:security:14" \
  "${OPS_GW}:Ops Gateway:AWS Knowledge docs/regions + Core prompt understanding/CLI:ops:8"; do
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

# --- Gateway Tool Catalog (SK=TOOL#) ---
# Populates per-gateway tool rows so get_gateway_tools() is non-empty. This makes
# the Tier1 deploy quality gate's toolFilter validation actually enforce (it was a
# permanent no-op with an empty catalog) and lets the Builder LLM recommend
# specific tools. Tool names mirror those registered by register-gateway-targets.py.
echo "13. Gateway Tool Catalog"
seed_gateway_tools() {
  local gw_id="$1"; shift
  local entry name desc
  for entry in "$@"; do
    name="${entry%%|*}"
    desc="${entry#*|}"
    aws dynamodb put-item --table-name "$TABLE" --region "$REGION" --item "{
      \"PK\": {\"S\": \"GATEWAY#${gw_id}\"},
      \"SK\": {\"S\": \"TOOL#${name}\"},
      \"name\": {\"S\": \"${name}\"},
      \"description\": {\"S\": \"${desc}\"}
    }"
  done
  echo "  Tools seeded: $gw_id ($# entries)"
}

seed_gateway_tools "$MONITORING_GW" \
  "get_metric_data|Get metric data" "get_metric_metadata|Metric metadata" \
  "analyze_metric|Analyze trend" "get_recommended_metric_alarms|Alarm recommendations" \
  "get_active_alarms|Active alarms" "get_alarm_history|Alarm history" \
  "describe_log_groups|Log groups" "analyze_log_group|Search logs" \
  "execute_log_insights_query|Log Insights query" "get_logs_insight_query_results|Query results" \
  "cancel_logs_insight_query|Cancel query" "lookup_events|Look up CloudTrail events" \
  "list_event_data_stores|Lake data stores" "lake_query|Lake SQL query" \
  "get_query_status|Query status" "get_query_results|Query results" \
  "validate_datasource_url|Validate datasource URL" "resolve_dns|Resolve hostname to IPs" \
  "check_nlb_targets|Check NLB target health" "analyze_security_groups|Analyze SG chain" \
  "trace_network_path|Trace network path" "test_http_connectivity|Test HTTP endpoint" \
  "check_k8s_service_endpoints|Check K8s Service endpoints" "run_full_diagnosis|Full 6-step diagnostic"

seed_gateway_tools "$CONTAINER_GW" \
  "list_eks_clusters|List EKS clusters" "get_eks_vpc_config|EKS VPC config" \
  "get_eks_insights|EKS insights" "get_cloudwatch_logs|EKS CloudWatch logs" \
  "get_cloudwatch_metrics|EKS metrics" "get_eks_metrics_guidance|Container Insights guidance" \
  "get_policies_for_role|IAM role policies" "search_eks_troubleshoot_guide|EKS troubleshooting" \
  "generate_app_manifest|Generate K8s YAML" "ecs_resource_management|ECS resources" \
  "ecs_troubleshooting_tool|ECS troubleshooting" "wait_for_service_ready|Check service readiness"

seed_gateway_tools "$DATA_GW" \
  "list_tables|List tables" "describe_table|Describe table" \
  "query_table|Query/scan table" "get_item|Get item by key" \
  "dynamodb_data_modeling|Data modeling guide" "compute_performances_and_costs|Cost estimation" \
  "list_db_instances|List RDS instances" "list_db_clusters|List Aurora clusters" \
  "describe_db_instance|Describe instance" "describe_db_cluster|Describe cluster" \
  "execute_sql|SQL via Data API (SELECT only)" "list_snapshots|List snapshots" \
  "list_cache_clusters|List cache clusters" "describe_cache_cluster|Describe cache cluster" \
  "list_replication_groups|List replication groups" "describe_replication_group|Describe group" \
  "list_serverless_caches|Serverless caches" "elasticache_best_practices|Best practices" \
  "list_clusters|List Kafka clusters" "get_cluster_info|Cluster details" \
  "get_configuration_info|MSK configurations" "get_bootstrap_brokers|Bootstrap brokers" \
  "list_nodes|Broker nodes" "msk_best_practices|Best practices"

seed_gateway_tools "$COST_GW" \
  "get_today_date|Current date" "get_cost_and_usage|Cost and usage" \
  "get_cost_and_usage_comparisons|Compare months" "get_cost_comparison_drivers|Cost drivers" \
  "get_cost_forecast|Cost forecast" "get_dimension_values|Dimension values" \
  "get_tag_values|Tag values" "get_pricing|Service pricing" "list_budgets|List budgets" \
  "get_rightsizing_recommendations|Rightsizing recommendations" \
  "get_savings_plans_recommendations|Savings Plans recommendations" \
  "get_reserved_instance_recommendations|Reserved Instance recommendations" \
  "get_cost_optimization_hub_recommendations|Cost Optimization Hub recommendations" \
  "get_trusted_advisor_cost_checks|Trusted Advisor cost checks"

seed_gateway_tools "$NETWORK_GW" \
  "get_path_trace_methodology|Network troubleshooting methodology" "find_ip_address|Locate ENIs by IP" \
  "get_eni_details|ENI details" "list_vpcs|List VPCs" \
  "get_vpc_network_details|Full VPC config" "get_vpc_flow_logs|VPC flow logs" \
  "describe_network|Describe SG/NACL/RT/Subnet/VPC" "list_transit_gateways|List TGWs" \
  "get_tgw_details|TGW details" "get_tgw_routes|TGW routes" \
  "get_all_tgw_routes|All TGW routes" "list_tgw_peerings|TGW peerings" \
  "list_vpn_connections|VPN connections" "list_network_firewalls|Network Firewalls" \
  "get_firewall_rules|Firewall rules" "analyze_reachability|Analyze network reachability" \
  "query_flow_logs|Query flow logs"

seed_gateway_tools "$IAC_GW" \
  "validate_cloudformation_template|Validate CFn template" \
  "check_cloudformation_template_compliance|Check compliance" \
  "troubleshoot_cloudformation_deployment|Troubleshoot failures" \
  "search_cdk_documentation|Search CDK docs" "search_cloudformation_documentation|Search CFn docs" \
  "cdk_best_practices|CDK best practices" "read_iac_documentation_page|Fetch doc page" \
  "SearchAwsProviderDocs|AWS provider docs" "SearchAwsccProviderDocs|AWSCC provider docs" \
  "SearchSpecificAwsIaModules|AWS-IA modules" "SearchUserProvidedModule|Registry module" \
  "terraform_best_practices|Terraform best practices"

seed_gateway_tools "$SECURITY_GW" \
  "list_users|List IAM users" "get_user|User details" \
  "list_roles|List roles" "get_role_details|Role details" \
  "list_groups|List groups" "get_group|Group details" \
  "list_policies|List policies" "list_user_policies|User policies" \
  "list_role_policies|Role policies" "get_user_policy|User inline policy" \
  "get_role_policy|Role inline policy" "list_access_keys|Access keys" \
  "simulate_principal_policy|Policy simulation" "get_account_security_summary|Account security summary"

seed_gateway_tools "$OPS_GW" \
  "search_documentation|Search AWS docs" "read_documentation|Read doc page" \
  "recommend|Doc recommendations" "list_regions|List regions" \
  "get_regional_availability|Regional availability" "prompt_understanding|Solution design guide" \
  "call_aws|Execute AWS CLI" "suggest_aws_commands|Suggest commands"

echo "=== Seed complete ==="
