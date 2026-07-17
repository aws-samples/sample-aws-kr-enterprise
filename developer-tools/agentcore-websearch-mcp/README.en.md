# agentcore-websearch-mcp

*한국어 버전: [README.md](README.md)*

A tiny, dependency-light **MCP server that gives any MCP client (Claude Code, etc.) web search**, backed by the built-in Web Search Tool on **Amazon Bedrock AgentCore Gateway**.

It ships two pieces:

1. **`server.py`** — a local stdio↔HTTPS MCP proxy. It speaks the MCP stdio transport your client expects, and forwards each request to your AgentCore Gateway over HTTPS, signing every call with SigV4 using your normal AWS credentials.
2. **`cloudformation/agentcore-websearch-gateway.yaml`** — one-command infrastructure that stands up the gateway, its IAM service role, and the web-search connector target. Deploy it, copy one output value, and you're done.

```
MCP client  ──stdio JSON-RPC──▶  server.py  ──SigV4 HTTPS──▶  AgentCore Gateway (Web Search Tool)
```

## Why this exists

The AgentCore Gateway exposes web search as a standard MCP tool, but only over an **IAM-authenticated Streamable HTTP** endpoint — MCP clients speak **stdio** and can't SigV4-sign requests. This proxy bridges the two: stdio in, SigV4-signed HTTPS out. It also injects a helpful description on the web-search tool (the gateway returns it with an empty description), so the model knows *when* to reach for it.

- **No hardcoded credentials or profile.** Uses the standard AWS credential chain — env vars, `AWS_PROFILE`, shared config/credentials, SSO, and instance/container roles all work unchanged.
- **No third-party HTTP library.** Only `botocore` (for credentials + SigV4) plus the Python standard library.
- **Search stays in AWS.** The gateway serves queries entirely within AWS using its purpose-built web index.

---

## Quick start

### Prerequisites

- Python 3.9+ with `botocore` installed (`pip install botocore`).
- AWS credentials configured (`aws configure`, `AWS_PROFILE`, SSO, or a role) with permission to deploy the stack.
- The AWS CLI, for the deploy step below.

> **Region:** as of July 2026, the Web Search Tool connector is only available in **`us-east-1`** (N. Virginia). Deploy the stack there.

### 0. Clone the repository

Clone the repository and change into this project directory first. **`server.py` has to stay here** (see the note below), so clone it somewhere it won't be deleted or moved.

```bash
git clone https://github.com/aws-samples/sample-aws-kr-enterprise.git
cd sample-aws-kr-enterprise/developer-tools/agentcore-websearch-mcp
```

> **`server.py` is used continuously, not once.** The registration step below records `python3 /absolute/path/to/server.py` as the **local command Claude Code runs on every web search** — the proxy stays resident and is re-invoked for each query. Even after deploying, **do not delete or move the cloned repository (`server.py` in particular).** If you do move it, update the path in the MCP registration too (re-running the script is the easiest way).

### Automated setup (recommended)

The `agentcore-websearch.sh` script does the CloudFormation deploy and the Claude Code MCP registration in one step.

```bash
./agentcore-websearch.sh deploy     # deploy stack + register MCP server
./agentcore-websearch.sh destroy    # unregister MCP server + delete stack
./agentcore-websearch.sh url        # print the deployed gateway MCP URL
```

Use **`--profile NAME`** (or `-p NAME`) to select an AWS profile. On `deploy` it is used for the AWS CLI *and* **written into the MCP server env automatically**, so the proxy signs gateway requests with the same profile.

```bash
# Both the gateway URL and AWS_PROFILE are recorded into the MCP env
./agentcore-websearch.sh deploy --profile my-profile
```

If `--profile` is omitted, the `AWS_PROFILE` environment variable is used. If that is also unset, the standard AWS credential chain applies and no `AWS_PROFILE` is written into the MCP env.

> **Note:** If your deploy credentials differ from your gateway-invocation credentials (e.g. deploy with an admin role, but run with an IAM user that only has `InvokeGateway`), pass the *runtime* profile to `--profile`. Inbound auth is `AWS_IAM`, so real IAM credentials are required.

Tune the rest with environment variables (all optional): `REGION` (default `us-east-1`), `STACK_NAME`, `MCP_NAME`, `MCP_SCOPE` (default `user`), `PYTHON`.

If the `claude` CLI isn't found, the deploy still runs and the script prints the config for manual registration.

To run each step yourself, see the manual steps below.

### 1. Deploy the gateway

```bash
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name agentcore-websearch \
  --template-file cloudformation/agentcore-websearch-gateway.yaml \
  --capabilities CAPABILITY_IAM
```

Grab the MCP endpoint URL from the stack outputs:

```bash
aws cloudformation describe-stacks \
  --region us-east-1 \
  --stack-name agentcore-websearch \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayMcpUrl'].OutputValue" \
  --output text
```

### 2. Register the MCP server with your client

Point your MCP client at `server.py`, passing the URL from step 1 as `AGENTCORE_GATEWAY_URL`.

**Claude Code** (`claude mcp add`):

```bash
claude mcp add agentcore-websearch \
  --env AGENTCORE_GATEWAY_URL="https://<your-gateway>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp" \
  -- python3 /absolute/path/to/server.py
```

Or add it directly to your MCP client config:

```json
{
  "mcpServers": {
    "agentcore-websearch": {
      "command": "python3",
      "args": ["/absolute/path/to/server.py"],
      "env": {
        "AGENTCORE_GATEWAY_URL": "https://<your-gateway>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
      }
    }
  }
}
```

If your credentials come from a specific profile, add `"AWS_PROFILE": "your-profile"` to `env` (or export it in your shell). Anything the AWS SDK understands works.

### 3. Use it

Ask your client something that needs current information ("What's the latest release of X?"). It will call the `WebSearch` tool through the proxy.

### Try it without a client (optional)

The proxy reads one JSON-RPC message per line on stdin and writes responses to stdout; logs go to stderr.

```bash
export AGENTCORE_GATEWAY_URL="https://<your-gateway>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3 server.py
```

You should see an `initialize` result followed by a `tools/list` result containing the web-search tool.

---

## Configuration

All configuration is via environment variables:

| Variable | Required | Description |
| --- | --- | --- |
| `AGENTCORE_GATEWAY_URL` | **Yes** | The gateway MCP URL (the `GatewayMcpUrl` stack output). The proxy exits on startup if unset. |
| `AGENTCORE_SIGNING_REGION` | No | Override the SigV4 signing region. By default the region is parsed from the gateway URL, so a stray `AWS_REGION` can't break signing. |
| AWS credential vars | — | Standard AWS resolution: `AWS_PROFILE`, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, SSO, instance/container roles, etc. |

The caller's IAM identity needs `bedrock-agentcore:InvokeGateway` on the gateway ARN (inbound auth is `AWS_IAM`). The gateway's own outbound call to the web-search backend is authorized by the service role created in the CloudFormation stack.

## Required IAM permissions (deploy vs. use)

**The deploy identity and the runtime identity can differ.** A common setup is to deploy once with an admin role and run with a least-privilege user that can only invoke the gateway. In that case, after `agentcore-websearch.sh deploy`, set the MCP server's `AWS_PROFILE` env to the **runtime** profile.

### To deploy (creates/deletes the stack)

The deployer must be able to create and delete the CloudFormation stack and its resources (gateway, IAM role, target). At minimum:

- `cloudformation:CreateStack` / `DeleteStack` / `DescribeStacks` / `DescribeStackEvents` / `DescribeStackResources` / `GetTemplateSummary` (plus the change-set actions `deploy` uses: `CreateChangeSet` / `DescribeChangeSet` / `ExecuteChangeSet` / `DeleteChangeSet`)
- `bedrock-agentcore:CreateGateway` / `DeleteGateway` / `GetGateway` / `UpdateGateway`, and `CreateGatewayTarget` / `DeleteGatewayTarget` / `GetGatewayTarget` / `ListGatewayTargets`
- `iam:CreateRole` / `DeleteRole` / `GetRole` / `PutRolePolicy` / `DeleteRolePolicy` / `PassRole` — the stack creates the gateway service role (`aws cloudformation deploy` needs `--capabilities CAPABILITY_IAM`)

> In practice this is usually an admin or a dedicated deploy role rather than these actions granted individually.

### To use (the MCP proxy invoking the gateway)

The IAM identity the proxy signs with (i.e. the MCP env `AWS_PROFILE`) needs **just one permission**:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "bedrock-agentcore:InvokeGateway",
      "Resource": "arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:gateway/agentcore-websearch-*"
    }
  ]
}
```

- Using the `agentcore-websearch-*` wildcard for `Resource` (instead of one specific gateway ARN) means you don't have to edit the policy when a redeploy changes the gateway ID.
- This identity needs no deploy or describe permissions — it only invokes the gateway (runs web search).
- The gateway's outbound `bedrock-agentcore:InvokeWebSearch` permission belongs to the **gateway service role** created by CloudFormation, not to this identity.

## What the CloudFormation stack creates

- **`AWS::BedrockAgentCore::Gateway`** — MCP protocol, `AWS_IAM` inbound authorizer.
- **`AWS::IAM::Role`** — the gateway's outbound service role, trusting `bedrock-agentcore.amazonaws.com` and granting `bedrock-agentcore:InvokeGateway` plus `bedrock-agentcore:InvokeWebSearch` on the service-owned tool ARN `arn:aws:bedrock-agentcore:us-east-1:aws:tool/web-search.v1`.
- **`AWS::BedrockAgentCore::GatewayTarget`** — the built-in `web-search` connector target, authenticating with `GATEWAY_IAM_ROLE`.

Outputs: `GatewayMcpUrl`, `GatewayId`, `GatewayArn`, `GatewayServiceRoleArn`.

## Teardown

```bash
aws cloudformation delete-stack --region us-east-1 --stack-name agentcore-websearch
```

## Cost

The Web Search Tool and AgentCore Gateway are billed per use (search requests / gateway invocations). See the [Amazon Bedrock AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/) page for current rates.
