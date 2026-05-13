# Runbook

Operational procedures for deploying, operating, troubleshooting, and tearing down the real-time leaderboard system. This document assumes you have the AWS credentials, IaC tool, and regional defaults described in [`README.md`](../README.md#prerequisites).

**Scope.** This runbook covers development and operation of the leaderboard system. Customer-facing demo execution is out of scope and handled separately.

The system is deployed with **AWS CDK (Python)** using a nested-stack pattern (root: `LeaderboardApp`; children: `NetworkStack`, `DataStack`, `IngestStack`, `ApiStack`, `LoadGenStack`, `WebStack`). See [ADR-007](./adr/ADR-007-cdk-python-nested-stacks.md) for context.

## Environment Setup

### Required tools

- AWS CLI v2 with an authenticated profile pointing at the existing authorized AWS account
- AWS CDK ≥ 2.140 (Python) — install with `pip install aws-cdk-lib` (plus the CLI: `npm install -g aws-cdk`)
- Python 3.12 (CDK app + Lambda runtime in `app/`)
- Node.js 20 for the demo SPA build under `web/` and for the CDK CLI
- `redis-cli` or `valkey-cli` (optional, for direct cache inspection from a bastion or Session Manager — Valkey 8.0 is wire-compatible with Redis 7, either client works)

### Environment variables

The deployment reads the following from your shell or a `.env` file (never commit `.env` — it is ignored by Git).

| Variable | Purpose |
|---|---|
| `AWS_PROFILE` | Profile used for deploys |
| `AWS_REGION` | Pinned to `us-east-1` for this demo |
| `CDK_DEFAULT_ACCOUNT` | Target AWS account ID (CDK reads this when synthesizing) |
| `CDK_DEFAULT_REGION` | `us-east-1` |
| `LEADERBOARD_ENV` | Short environment tag, e.g. `demo`, `dev` |

## Deployment

Implementation is planned for Phase 1. Once `infra/` lands, the flow will be:

```bash
make deploy           # provision everything
make seed             # seed a small synthetic leaderboard
make demo-url         # print the default CloudFront domain (e.g. https://dxxxx.cloudfront.net)
```

Until then, the manual steps are:

1. One-time per account/region: `cdk bootstrap aws://<account-id>/us-east-1`
2. `cd infra && python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
3. `cdk synth` to validate, `cdk diff` to preview, then `cdk deploy --all` to provision the root `LeaderboardApp` stack and its nested children.
4. Build and sync the demo SPA: `cd web && npm ci && npm run build && aws s3 sync dist/ s3://<bucket>/`
5. Invalidate CloudFront for `index.html`: `aws cloudfront create-invalidation --distribution-id <id> --paths /index.html`
6. Smoke test: run the seed script in `app/scripts/seed.py` against the API Gateway URL.

## Common Operations

### Rebuild Valkey from DynamoDB

Use when Valkey is emptied, corrupted, or replaced with a larger node.

1. Stop the processor Lambda (set reserved concurrency to 0) so no new writes race the rebuild.
2. Run the rebuild script (`app/scripts/rebuild_from_ddb.py` — Phase 1 deliverable):
   - Scans DynamoDB with a consistent paginator.
   - Aggregates `scoreDelta` by `(gameId, userId)`.
   - `ZADD`s the aggregate to the per-game sorted set.
3. Restore processor concurrency. Valkey is now consistent with the event log.

### Purge a leaderboard (manual season reset)

1. Identify the game ID. The sorted-set key is `lb:<gameId>`.
2. Confirm with the owner that a reset is intended — this is destructive for the ranking view.
3. Run `DEL lb:<gameId>` via `valkey-cli` (or `redis-cli` — wire-compatible) from a bastion, or a one-off Lambda.
4. DynamoDB raw events are untouched; the leaderboard rebuilds from the next ingested event onward.

### Scale Valkey for higher load

1. Modify the ElastiCache node type in the CDK `DataStack` (`cache.r7g.large` → `cache.r7g.xlarge`).
2. `cdk diff` to confirm the change is isolated, then `cdk deploy LeaderboardApp/DataStack`. ElastiCache performs the migration with a short failover; demo traffic should be paused.
3. Validate with a small burst run; confirm `EngineCPUUtilization` headroom.

### Inspect and replay the DLQ

1. In SQS console, open the DLQ and sample messages to confirm the failure class (parse error, permission error, downstream throttle).
2. If the root cause is fixed, use the **Start DLQ redrive** action in the SQS console to send messages back to the main queue. (CDK-managed redrive policy is a Phase 1 nice-to-have.)
3. Monitor the processor Lambda for errors to confirm successful reprocessing.

### Force a CloudFront cache invalidation

```bash
aws cloudfront create-invalidation \
  --distribution-id <DIST_ID> \
  --paths "/index.html" "/assets/*"
```

## Troubleshooting

| Symptom | Likely Cause | Action |
|---|---|---|
| Leaderboard is stale during a run | Processor Lambda throttled or concurrency capped | Check Lambda `Throttles` metric; raise reserved concurrency; check Valkey `EngineCPUUtilization` |
| Queue depth climbs monotonically | Processor erroring or Lambda not invoking | Check Lambda errors and ESM status; verify IAM permissions to read SQS and write to Valkey + DDB |
| Duplicate scores appear | Idempotency check bypassed | Confirm `eventId` is unique at source; verify DynamoDB `ConditionExpression` is set on the write |
| Read API returns 5xx | Reader Lambda error or Valkey connection issue | Check reader Lambda logs; verify VPC security groups allow Lambda → Valkey on 6379 |
| Read API returns 404 | Game ID has no events yet | Confirm the game ID matches an active injector run |
| DLQ fills during normal traffic | Poison message (schema mismatch) or repeated transient error | Sample the DLQ; patch the handler; redrive |
| p95 latency spikes at burst start | Cold starts on processor Lambda | Pre-warm before the demo; consider provisioned concurrency |
| Demo page shows blank leaderboard after deploy | CloudFront cached the previous build | Invalidate `/index.html` (see above) |
| Valkey `EngineCPUUtilization` near 100% | Node too small for TPS, or hot key | Scale up node type; inspect key-level stats with `valkey-cli --hotkeys` (or `redis-cli --hotkeys` — wire-compatible) |
| DynamoDB `ThrottledRequests` > 0 | On-demand scaling still catching up after idle | Run a warmup burst before the demo; switch to provisioned capacity if recurrent |

## Teardown

Teardown must leave the sandbox account clean. Tagged resources (`project=leaderboard-demo`) simplify verification.

1. Stop all Step Functions executions:
   ```bash
   aws stepfunctions list-executions --state-machine-arn <ARN> --status-filter RUNNING
   # then for each execution ARN:
   aws stepfunctions stop-execution --execution-arn <EXEC_ARN>
   ```
2. Empty the S3 bucket holding the SPA (CloudFront origin):
   ```bash
   aws s3 rm s3://<bucket>/ --recursive
   ```
3. Run `cdk destroy --all` (or `cdk destroy LeaderboardApp` — nested stacks cascade). Confirm the synth preview shows every resource removed.
4. Verify in the console:
   - ElastiCache cluster: none
   - Lambda functions tagged `project=leaderboard-demo`: none
   - DynamoDB table: none
   - SQS queues (main + DLQ): none
   - API Gateway API: none
   - S3 bucket: none
   - CloudFront distribution: deleted (CloudFront deletion takes several minutes after the distribution is disabled)
5. Delete CloudWatch log groups manually if they outlive the Lambdas — CDK sometimes leaves these behind with default log-retention settings:
   ```bash
   aws logs delete-log-group --log-group-name /aws/lambda/<function>
   ```
6. Review the AWS Cost Explorer the following day to confirm billing has stopped.
