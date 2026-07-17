#!/usr/bin/env bash
#
# agentcore-websearch.sh — deploy/destroy the AgentCore web-search gateway and
# register/unregister the local MCP proxy with Claude Code, in one step.
#
#   ./agentcore-websearch.sh deploy  [--profile NAME]   # deploy CFN + register MCP server
#   ./agentcore-websearch.sh destroy [--profile NAME]   # unregister MCP + delete CFN stack
#   ./agentcore-websearch.sh url     [--profile NAME]   # print the deployed gateway MCP URL
#
# --profile NAME (or -p NAME) selects the AWS profile for the AWS CLI. On 'deploy'
# it is ALSO written into the MCP server's env, so the proxy signs gateway requests
# with the same profile. If omitted, the AWS_PROFILE environment variable is used;
# if that is also unset, the standard AWS credential chain applies (and no AWS_PROFILE
# is written into the MCP env).
#
# Configuration (environment variables, all optional):
#   REGION       AWS region                       (default: us-east-1 — Web Search is us-east-1 only)
#   STACK_NAME   CloudFormation stack name         (default: agentcore-websearch)
#   MCP_NAME     MCP server name in Claude         (default: agentcore-websearch)
#   MCP_SCOPE    Claude MCP scope                  (default: user)
#   PYTHON       Python interpreter for the proxy  (default: python3)
#   AWS_PROFILE  Fallback profile if --profile is not given
#
set -euo pipefail

REGION="${REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-agentcore-websearch}"
MCP_NAME="${MCP_NAME:-agentcore-websearch}"
MCP_SCOPE="${MCP_SCOPE:-user}"
PYTHON="${PYTHON:-python3}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/cloudformation/agentcore-websearch-gateway.yaml"
SERVER="$SCRIPT_DIR/server.py"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "'$1' not found on PATH. $2"; }

usage() {
  cat >&2 <<EOF
Usage: $(basename "$0") {deploy|destroy|url} [--profile NAME]

  deploy    Deploy the CloudFormation stack and register the MCP server with Claude Code.
  destroy   Unregister the MCP server and delete the CloudFormation stack.
  url       Print the deployed gateway MCP URL.

Options:
  -p, --profile NAME   AWS profile for the AWS CLI. On 'deploy' it is also written
                       into the MCP server env. Falls back to \$AWS_PROFILE.

Config via env vars: REGION, STACK_NAME, MCP_NAME, MCP_SCOPE, PYTHON, AWS_PROFILE.
EOF
}

gateway_url() {
  # '|| true' so a missing stack yields an empty string (handled by callers)
  # rather than aborting the script under 'set -e'.
  aws cloudformation describe-stacks \
    --region "$REGION" --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='GatewayMcpUrl'].OutputValue" \
    --output text 2>/dev/null || true
}

cmd_deploy() {
  need aws "Install the AWS CLI: https://aws.amazon.com/cli/"
  [ -f "$TEMPLATE" ] || die "Template not found: $TEMPLATE"
  [ -f "$SERVER" ]   || die "Proxy not found: $SERVER"

  # botocore is required at runtime by the proxy; warn early rather than at first use.
  if ! "$PYTHON" -c 'import botocore' >/dev/null 2>&1; then
    warn "$PYTHON cannot import botocore. The proxy needs it at runtime: pip install botocore"
  fi

  log "Deploying CloudFormation stack '$STACK_NAME' in $REGION${PROFILE:+ (profile: $PROFILE)} ..."
  aws cloudformation deploy \
    --region "$REGION" \
    --stack-name "$STACK_NAME" \
    --template-file "$TEMPLATE" \
    --capabilities CAPABILITY_IAM

  local url
  url="$(gateway_url)"
  [ -n "$url" ] && [ "$url" != "None" ] || die "Could not read GatewayMcpUrl from stack outputs."
  log "Gateway MCP URL: $url"

  if command -v claude >/dev/null 2>&1; then
    # Build MCP env: always the gateway URL; add AWS_PROFILE only if one was resolved.
    local -a env_args=(--env "AGENTCORE_GATEWAY_URL=$url")
    if [ -n "$PROFILE" ]; then
      env_args+=(--env "AWS_PROFILE=$PROFILE")
      log "MCP env will include AWS_PROFILE=$PROFILE"
    else
      warn "No profile resolved — MCP env has no AWS_PROFILE; the proxy will use the default AWS credential chain."
    fi

    # Re-registering: remove any existing entry so 'add' does not fail on a duplicate.
    claude mcp remove "$MCP_NAME" --scope "$MCP_SCOPE" >/dev/null 2>&1 || true
    log "Registering MCP server '$MCP_NAME' (scope: $MCP_SCOPE) ..."
    claude mcp add "$MCP_NAME" \
      --scope "$MCP_SCOPE" \
      "${env_args[@]}" \
      -- "$PYTHON" "$SERVER"
    log "Done. Verify with: claude mcp get $MCP_NAME"
  else
    warn "'claude' CLI not found — skipping MCP registration."
    local profile_line=""
    [ -n "$PROFILE" ] && profile_line=", \"AWS_PROFILE\": \"$PROFILE\""
    cat <<EOF

Register the MCP server manually with this config:

  {
    "mcpServers": {
      "$MCP_NAME": {
        "command": "$PYTHON",
        "args": ["$SERVER"],
        "env": { "AGENTCORE_GATEWAY_URL": "$url"$profile_line }
      }
    }
  }
EOF
  fi
}

cmd_destroy() {
  need aws "Install the AWS CLI: https://aws.amazon.com/cli/"

  if command -v claude >/dev/null 2>&1; then
    log "Unregistering MCP server '$MCP_NAME' ..."
    claude mcp remove "$MCP_NAME" --scope "$MCP_SCOPE" >/dev/null 2>&1 || true
  fi

  log "Deleting CloudFormation stack '$STACK_NAME' in $REGION ..."
  aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"
  log "Waiting for stack deletion to complete ..."
  aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME"
  log "Stack deleted."
}

cmd_url() {
  need aws "Install the AWS CLI: https://aws.amazon.com/cli/"
  local url
  url="$(gateway_url)"
  [ -n "$url" ] && [ "$url" != "None" ] || die "Stack '$STACK_NAME' not found or has no GatewayMcpUrl output."
  printf '%s\n' "$url"
}

# --- Parse subcommand and options --------------------------------------------
SUBCMD="${1:-}"
shift || true

PROFILE_OPT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -p|--profile) PROFILE_OPT="${2:-}"; [ -n "$PROFILE_OPT" ] || die "--profile requires a value"; shift 2 ;;
    --profile=*)  PROFILE_OPT="${1#*=}"; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            die "Unknown option: $1 (see --help)" ;;
  esac
done

# --profile option wins; otherwise fall back to the AWS_PROFILE env var.
PROFILE="${PROFILE_OPT:-${AWS_PROFILE:-}}"
# Export so every 'aws' call in this run uses the resolved profile.
[ -n "$PROFILE" ] && export AWS_PROFILE="$PROFILE"

case "$SUBCMD" in
  deploy)  cmd_deploy ;;
  destroy) cmd_destroy ;;
  url)     cmd_url ;;
  -h|--help) usage; exit 0 ;;
  *) usage; exit 2 ;;
esac
