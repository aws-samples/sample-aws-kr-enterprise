# IAM Policies (Single Source of Truth)

This directory holds the IAM policy documents for the roles used by the AgentCore
agent-builder platform. The JSON here is authoritative: `scripts/deploy-lambda-tools.sh`
reads `awsops-lambda-execution-role.json` at deploy time to create/refresh the Lambda
execution role, so there is **no wildcard policy hardcoded in the shell scripts**.

## Roles overview

| # | Role | Purpose | Where it is defined | This work? |
|---|------|---------|---------------------|------------|
| 1 | `AWSopsLambdaNetworkRole` | Execution role for the 19 `awsops-*` MCP Lambda tools (read-only AWS diagnostics + a few non-read query APIs). | Managed base `ReadOnlyAccess` (attached) + inline `awsops-lambda-execution-role.json` (this dir), applied by `scripts/deploy-lambda-tools.sh`. | **Yes** |
| 2 | `AWSopsAgentCoreRole` | Assumed by the 8 AgentCore MCP Gateways to invoke Bedrock models, pull ECR images, invoke the `awsops-*` Lambda targets, and manage gateways/runtimes. | Inline least-privilege policy in `scripts/deploy-gateways.sh`. | No change |
| 3 | `{prefix}-platform-api-task` | ECS task role for the agent-build platform API (DynamoDB CRUD, Bedrock invoke, AgentCore runtime lifecycle, X-Ray/CloudWatch reads). | Terraform: `iac/modules/iam/platform_api.tf`. | No change |

## Least-privilege rationale — `AWSopsLambdaNetworkRole`

### Strategy: `ReadOnlyAccess` base + minimal non-read inline

The role gets two things:

1. **Base — AWS managed `ReadOnlyAccess`** (`arn:aws:iam::aws:policy/ReadOnlyAccess`),
   attached separately by `deploy-lambda-tools.sh`. This is *not* in the JSON here.
   It covers every read/`Describe`/`List`/`Get` action across services that the MCP
   tools need for diagnostics.
2. **Inline — `awsops-lambda-execution-role.json`** (this dir). It contains **only the
   non-read actions that `ReadOnlyAccess` does not include**:

| Action(s) | Used by | Why not covered by ReadOnlyAccess |
|-----------|---------|-----------------------------------|
| `rds-data:ExecuteStatement`, `rds-data:BatchExecuteStatement` | `awsops-rds-mcp` (`execute_sql`, SELECT-only) | RDS Data API statement calls are not read-classified. |
| `secretsmanager:GetSecretValue` (scoped) | `awsops-rds-mcp` (`execute_sql` → `secretArn`) | RDS Data API authorizes the DB connection via the referenced secret, so the caller must read that secret. `ReadOnlyAccess` grants only `secretsmanager:Describe*/GetResourcePolicy/List*`, **not** `GetSecretValue`. Scoped to RDS/Aurora credential secret ARNs (see below) — a single non-read action, not a service wildcard. |
| `logs:StartQuery`, `logs:GetQueryResults`, `logs:StopQuery` | `awsops-cloudwatch-mcp` (Logs Insights) | Insights query lifecycle actions are not in `ReadOnlyAccess`. |
| `cloudtrail:StartQuery`, `cloudtrail:GetQueryResults` | `awsops-cloudtrail-mcp` (CloudTrail Lake) | Lake query actions are not read-classified. |
| `ec2:CreateNetworkInsightsPath`, `ec2:StartNetworkInsightsAnalysis`, `ec2:CreateTags` | `awsops-reachability-analyzer` (`reachability.py`, network Gateway tool `analyze_reachability`) | Reachability Analyzer is inherently write-based — see note below. `CreateTags` is required because `create_network_insights_path` passes `TagSpecifications` for the `network-insights-path` resource. |
| `sts:AssumeRole` (scoped) | `cross_account.py` | Needed for cross-account diagnostics; scoped to one role name (see below). |

### Why the service wildcards were removed

The original prototype attached a single inline policy with broad service wildcards
(`ec2:*`, `dynamodb:*`, `rds:*`, `eks:*`, `ecs:*`, `kafka:*`, `elasticache:*`,
`cloudtrail:*`, `network-firewall:*`, `networkmanager:*`, `cloudformation:*`,
`iam:List*/Get*`, `ce:*`, `pricing:*`, ...). Those grant write/mutate permissions the
tools never use. Replacing them with `ReadOnlyAccess` + the small inline set above:

- keeps every **read** path the tools rely on working (ReadOnlyAccess is broad on reads), and
- removes the ability to **create/modify/delete** any resource.

### Scoping the RDS Data API secret read

`awsops-rds-mcp`'s `execute_sql` calls `rds-data:ExecuteStatement` with a `secretArn`;
the RDS Data API uses that Secrets Manager secret to authenticate the DB connection, so
the Lambda principal must be able to read it. `ReadOnlyAccess` deliberately omits
`secretsmanager:GetSecretValue` (it grants only `Describe*`/`GetResourcePolicy`/`List*`),
so we add exactly that one action inline. To keep least-privilege, `Resource` is scoped to
the common RDS/Aurora credential-secret naming patterns rather than `*`:

- `arn:aws:secretsmanager:*:*:secret:rds-*` — user-created RDS/Aurora credential secrets.
- `arn:aws:secretsmanager:*:*:secret:rds!*` — RDS-managed master-user secrets (managed
  secret names are prefixed `rds!`).
- `arn:aws:secretsmanager:*:*:secret:aurora-*` — common Aurora credential-secret prefix.

This is a single non-read action (not a service wildcard), so the no-wildcard hard gate
still holds. If a deployment stores its DB credentials under a different secret name,
add that secret's ARN to the `RdsDataApiSecretRead` statement instead of broadening to
`*`.

### Why this matters: `call_aws` is guarded only by IAM

`awsops-core-mcp` exposes `call_aws`, which invokes **arbitrary boto3 methods** chosen at
runtime. There is no per-method allowlist in code, so **the role's IAM permissions are the
only guardrail**. The `ReadOnlyAccess` base is exactly the right shape here: any read API
the agent asks for succeeds, while any write/mutate API is denied by IAM regardless of
what `call_aws` is asked to run.

### Exception: `awsops-reachability-analyzer` is write-based by design

VPC Reachability Analyzer has no read-only mode. To answer "can A reach B?",
`reachability.py` must **create** a `network-insights-path` and **start** an analysis on
it — there is no `Describe`-only equivalent. So the `ReachabilityAnalyzer` statement is an
**intentional exception to the read-only intent**: it grants the three write actions the
tool cannot function without.

- `ec2:CreateNetworkInsightsPath` — create the path to analyze.
- `ec2:StartNetworkInsightsAnalysis` — run the analysis on that path.
- `ec2:CreateTags` — required because `create_network_insights_path` passes
  `TagSpecifications` (tagging the `network-insights-path` resource at create time). Without
  it the create call fails `UnauthorizedOperation`.

The corresponding reads (`ec2:DescribeNetworkInsightsAnalyses` /
`ec2:DescribeNetworkInsightsPaths`) are already covered by `ec2:Describe*` in
`ReadOnlyAccess`, so they are not listed here. These actions are scoped by `Sid`
`ReachabilityAnalyzer` and do **not** widen to any other EC2 mutation. This is kept because
removing it would leave the deployed-and-registered `analyze_reachability` Gateway tool
returning `AccessDenied` on every invocation (**HARD GATE**: the tool must stay functional).

> Note: the inline action set is a static-analysis baseline. If real-workload testing
> surfaces an `AccessDenied` on a legitimate non-read action, add that specific action to
> `awsops-lambda-execution-role.json` and re-run `deploy-lambda-tools.sh` (it re-applies
> the inline policy idempotently). Do not reintroduce service wildcards.

## Manual creation guide (fallback CLI)

`scripts/deploy-lambda-tools.sh` performs these steps automatically. Use the commands
below only if you need to create the role by hand. Run from the repo root; `AWS_REGION`
must be set and your caller needs `iam:CreateRole` / `iam:AttachRolePolicy` /
`iam:PutRolePolicy`.

```bash
# 1) Create the role with a Lambda trust policy
aws iam create-role \
  --role-name AWSopsLambdaNetworkRole \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}
    ]
  }'

# 2) Attach the managed base (read-only across services)
aws iam attach-role-policy \
  --role-name AWSopsLambdaNetworkRole \
  --policy-arn arn:aws:iam::aws:policy/ReadOnlyAccess

# 3) Attach the minimal non-read inline policy (this file is the SSoT)
aws iam put-role-policy \
  --role-name AWSopsLambdaNetworkRole \
  --policy-name AWSopsLambdaNonReadInline \
  --policy-document file://docs/iam-policies/awsops-lambda-execution-role.json
```

CloudWatch Logs for the Lambda functions themselves are covered by `ReadOnlyAccess`
plus the AgentCore invoke path; if you deploy the functions inside a VPC you must also
attach `arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole`. The
default demo deploys the tools **without a VPC**, so no VPC policy is required.

## Cross-account note

The `sts:AssumeRole` statement is scoped to
`arn:aws:iam::*:role/AWSopsReadOnlyRole`. This supports **cross-account diagnostics**:
`cross_account.py` assumes a role named `AWSopsReadOnlyRole` in a target account and runs
read-only calls there.

- **Default single-account demo works without it.** When no target-account role ARN is
  passed, the tools use the Lambda's own credentials in the deploy account — nothing extra
  to set up.
- **For cross-account diagnostics**, create an `AWSopsReadOnlyRole` in each target account
  separately: attach `ReadOnlyAccess` and a trust policy allowing this account's
  `AWSopsLambdaNetworkRole` to assume it. (Optionally set `AWSOPS_EXTERNAL_ID` /
  `AWSOPS_ROLE_NAME` env vars on the Lambdas to override the external ID and role name.)
