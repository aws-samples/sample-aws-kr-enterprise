# ADR-006: Run the entire demo in a single AWS account

- **Status:** Accepted
- **Date:** 2026-05-06
- **Deciders:** Solutions Architect, Leaderboard Demo

## Context

Large platforms typically have internal guidance around AWS Organizations, Control Tower, and environment separation for production workloads. For this demo, the stakeholders have explicitly confirmed:

- The producer (Game Platform) and the consumer (leaderboard system) live in the **same account**.
- No cross-account fan-out, no resource sharing to other OUs, no shared VPCs with peer accounts.
- An existing, authorized AWS account is used, with **no hard budget cap** for this demo environment (cost estimates are for awareness and teardown hygiene, not gating).
- The goal is to show the **architecture pattern** in the fastest, clearest way possible — account boundaries are a production hardening concern, not a demo concern.

## Decision

Deploy all resources (SQS, Lambda, ElastiCache, DynamoDB, API Gateway, CloudFront, S3, Step Functions) into a **single AWS account** in **`us-east-1`**.

- One VPC, two private subnets across two AZs for ElastiCache Multi-AZ.
- All IAM roles are scoped per Lambda (least privilege at the permission level, not at the account level).
- Infrastructure is defined with AWS CDK (Python) using a single root stack (`LeaderboardApp`) with six nested child stacks — see [ADR-007](./ADR-007-cdk-python-nested-stacks.md).

This is **explicitly a demo-time decision**. It is not a recommendation for production.

## Consequences

**Positive**

- Fastest path to a working demo. No cross-account role chaining, no Resource Access Manager shares, no inter-region latency.
- One CloudWatch account to look at during the demo — no "which account was that log in again?" moments.
- One billing boundary — costs are trivially attributable to this demo.
- Tear-down is `cdk destroy --all` (or `cdk destroy LeaderboardApp`) in one place — nested stacks cascade.

**Negative**

- No blast-radius isolation: a misconfigured IAM policy on a Lambda could (in principle) touch other resources in the account. Mitigated by putting the demo in a **dedicated sandbox account** if the customer has one — this ADR does not preclude that.
- Not representative of a real production setup, where the Game Platform would likely live in a different account from the analytics/leaderboard platform. A production version would introduce a cross-account SQS policy or EventBridge bus. Flagged in the Production hardening checklist.
- Data classification boundaries (if the customer's production environment treats gameplay telemetry as sensitive) are not modeled here.

## Alternatives Considered

| Option | Why not (for the demo) |
|---|---|
| **Multi-account: producer account → consumer account via SQS cross-account policy** | Closer to production reality, but adds IAM complexity and debugging surface area. The demo is about the leaderboard pattern, not about org-level access control. |
| **Multi-account via EventBridge bus-to-bus** | Nice pattern for event-driven architectures across OUs. Out of scope — we have one producer and one consumer. |
| **Multi-region (active/active)** | Addresses regional failure, but doubles the ElastiCache spend and adds replication lag to the demo narrative. Deferred to production hardening. |
| **AWS Organizations with dedicated OUs per environment** | Correct for production. Not a demo concern. |

## When this decision expires

Immediately upon productionization. The Production hardening checklist in `ARCHITECTURE.md` includes:

- Separate accounts for producer / consumer / observability.
- Cross-account SQS or EventBridge for ingestion.
- Dedicated analytics account with Lake Formation controls for the raw events table / export.
- Multi-region active/passive with Global Tables for the DDB source of truth.

## References

- [Organizing your AWS environment using multiple accounts](https://docs.aws.amazon.com/whitepapers/latest/organizing-your-aws-environment/organizing-your-aws-environment.html)
- [Cross-account access to an Amazon SQS queue](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-basic-examples-of-sqs-policies.html#grant-permissions-to-another-account)
- `/docs/ARCHITECTURE.md` — "Production hardening checklist"
