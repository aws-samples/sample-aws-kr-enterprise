# ADR-007: Use AWS CDK (Python) with nested stacks

- **Status:** Accepted
- **Date:** 2026-05-06
- **Deciders:** Solutions Architect, Leaderboard Demo

## Context

The demo provisions six logical components:

1. **Network** — VPC, private subnets across two AZs, security groups.
2. **Data** — ElastiCache for Valkey, DynamoDB raw-events table.
3. **Ingest** — SQS queue + DLQ, processor Lambda, Event Source Mapping.
4. **Api** — API Gateway HTTP API, reader Lambda.
5. **LoadGen** — Step Functions state machine, trigger Lambda, load-generator Lambda.
6. **Web** — S3 demo-UI bucket, CloudFront distribution with OAC.

Requirements for the IaC choice:

- **Python-first team.** Application code (Lambdas) is Python 3.12; we want the same language for infrastructure to keep context-switching low.
- **Single-command deploy and teardown.** One `cdk deploy` and one `cdk destroy` must bring the entire demo up and down.
- **Per-component redeploy.** Tweaking a single component (e.g. scaling Valkey up) should be redeployable in isolation without touching unrelated resources.
- **CloudFormation headroom.** Staying under the 500-resources-per-stack limit, and keeping individual template sizes small, benefits both deploy speed and review clarity.
- **Single cloud (AWS).** No multi-cloud requirement; CDK's AWS-native abstractions are a straight win here.

## Decision

Use **AWS CDK in Python** with a **nested-stack** pattern. A single root stack (`LeaderboardApp`) composes six child stacks:

```
LeaderboardApp (root)
├── NetworkStack   ← VPC, Subnets, Security Groups
├── DataStack      ← Valkey (ElastiCache), DynamoDB
├── IngestStack    ← SQS + DLQ, Processor Lambda, ESM
├── ApiStack       ← API Gateway HTTP API, Reader Lambda
├── LoadGenStack   ← Step Functions, Trigger Lambda, Load-Gen Lambda
└── WebStack       ← S3, CloudFront OAC, Demo UI assets
```

Workflow:

- **One-time:** `cdk bootstrap aws://<account>/us-east-1` to provision the CDK toolkit stack.
- **Preview:** `cdk synth` (generate CloudFormation templates locally), `cdk diff` (show planned changes).
- **Deploy:** `cdk deploy --all` or `cdk deploy LeaderboardApp` — nested stacks cascade.
- **Targeted redeploy:** `cdk deploy LeaderboardApp/DataStack` to touch one component.
- **Teardown:** `cdk destroy --all` (or `cdk destroy LeaderboardApp`) — nested stacks cascade.

Dependencies: typed cross-stack references (e.g., `NetworkStack` passes its VPC construct into `DataStack`) rather than string parameter-stitching.

## Consequences

**Positive**

- **Python throughout.** Same language for Lambda handlers and IaC; shared linters, formatters, type checkers.
- **Nested stacks isolate concerns.** Each stack has one responsibility; reviewers can read one file at a time.
- **Single-command deploy/teardown.** Matches the demo's "one command brings it up, one command tears it down" requirement.
- **Strong typing via `aws-cdk-lib`.** Construct methods return typed objects; misuse fails at `cdk synth` time rather than deploy time.
- **CDK constructs abstract boilerplate.** `aws_ec2.Vpc` with subnets + NAT is ~5 lines; raw CloudFormation is ~150 lines for the same result.
- **Room to grow.** Each nested stack has its own 500-resource budget; we can add resources in `DataStack` without affecting `IngestStack`.

**Negative**

- **CDK bootstrap is a one-time extra step.** Easy to miss in a fresh account; we'll document it prominently in the RUNBOOK.
- **Nested stacks synthesize to CloudFormation.** When a deploy fails mid-way, debugging sometimes requires reading the synthesized template under `cdk.out/` rather than the Python source.
- **Smaller community than Terraform for non-AWS issues.** Mitigated by the fact that this project is 100% AWS.
- **CDK upgrades occasionally break synth.** Pin `aws-cdk-lib` in `requirements.txt` and bump deliberately.

## Alternatives Considered

| Option | Why not |
|---|---|
| **Terraform** | Larger ecosystem and HCL is familiar to many engineers. Rejected because this demo is single-cloud (AWS), the team is Python-first, and CDK's Python integration makes fan-out over many small resources (six stacks × dozens of resources each) more ergonomic than templating in HCL. |
| **CDK TypeScript** | Fully valid — CDK's richest language support. Rejected because the team is Python-biased and the demo SPA can be plain JS without introducing TypeScript for just the infra layer. |
| **AWS SAM** | Good for simple serverless apps (Lambda + API GW + DynamoDB). Rejected because SAM doesn't cover ElastiCache, VPCs, or Step Functions with the same elegance — we'd end up dropping to raw CloudFormation for half of the system. |
| **Raw CloudFormation** | Most verbose option; no abstraction, no typing, no local testing. Rejected immediately. |
| **Pulumi (Python)** | Similar developer experience to CDK. Rejected because CDK is the AWS-native choice; using Pulumi would require introducing a separate state backend and an extra SaaS dependency for an open-source demo repo. |

## References

- [AWS CDK for Python](https://docs.aws.amazon.com/cdk/v2/guide/work-with-cdk-python.html)
- [CDK Nested Stacks](https://docs.aws.amazon.com/cdk/v2/guide/stacks.html#nested_stacks)
- `/docs/ARCHITECTURE.md` — component-level detail for each nested stack
- `/docs/PLAN.md` — Phase 1 CDK bootstrap + stack scaffolding sequence
- `/docs/RUNBOOK.md` — `cdk synth` / `cdk diff` / `cdk deploy` / `cdk destroy` commands
