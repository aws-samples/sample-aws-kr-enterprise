# Architecture Decision Records

This directory contains the key architectural decisions for the real-time leaderboard demo, in the format proposed by Michael Nygard.

Each ADR captures:

- **Status** — proposed / accepted / superseded
- **Context** — what constraint or forcing function drove the decision
- **Decision** — what we chose
- **Consequences** — what this makes easier, what it makes harder
- **Alternatives Considered** — what else was on the table and why it lost

ADRs are numbered sequentially and are immutable once accepted. When a decision is reversed, a new ADR supersedes the old one (do not edit history).

## Index

| ID | Title | Status |
|----|-------|--------|
| [ADR-001](./ADR-001-ingestion-with-sqs.md) | Ingest score events with SQS Standard + Lambda ESM | Accepted |
| [ADR-002](./ADR-002-sorted-set-leaderboard.md) | Sorted Set leaderboard store on ElastiCache for Valkey | Accepted |
| [ADR-003](./ADR-003-dynamodb-raw-events.md) | Persist raw events in DynamoDB with conditional writes | Accepted |
| [ADR-004](./ADR-004-lambda-load-generator.md) | Generate demo load with Lambda + Step Functions (not browser) | Accepted |
| [ADR-005](./ADR-005-polling-over-websocket.md) | Poll the API every 1s instead of WebSocket push for the demo UI | Accepted |
| [ADR-006](./ADR-006-single-account-demo.md) | Run the entire demo in a single AWS account (`us-east-1`) | Accepted |
| [ADR-007](./ADR-007-cdk-python-nested-stacks.md) | Use AWS CDK (Python) with nested stacks | Accepted |
| [ADR-008](./ADR-008-valkey-over-redis.md) | Use ElastiCache for Valkey over ElastiCache for Redis | Accepted |

## How to add a new ADR

1. Copy the format of an existing file.
2. Use the next sequential number (`ADR-009-...`).
3. Add a row to the index table above.
4. Open a PR; reviewers should pay special attention to the **Alternatives Considered** section.
