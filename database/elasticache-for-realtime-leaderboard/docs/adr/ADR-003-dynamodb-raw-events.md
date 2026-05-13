# ADR-003: Persist raw events in DynamoDB with conditional writes

- **Status:** Accepted
- **Date:** 2026-05-06
- **Deciders:** Solutions Architect, Leaderboard Demo

## Context

The demo needs three things beyond the hot-path Valkey board:

1. **Idempotency** — SQS Standard is at-least-once (ADR-001), so duplicates must not double-count a user's score.
2. **Durability** — if Valkey is lost (node failure, accidental `FLUSHALL`, scheduled patch window), the leaderboard must be recoverable.
3. **An analytics base** — future work (DAU/MAU, cheat detection, season rollover) needs the raw event stream. We don't want to bolt this on later.

The system must not require a second operational database (Aurora/RDS) or a second streaming system just to satisfy these.

## Decision

Write every successfully-processed event to **DynamoDB** with the following schema and semantics:

- **Table:** `raw-events`
- **PK:** `gameId` (String)
- **SK:** `ts#eventId` (String) — ISO-8601 timestamp + `#` + globally-unique `eventId`
- **Attributes:** `eventId`, `userId`, `score`, `ingestedAt`, `sourceGame` (optional), `ttl`
- **Billing:** On-Demand (`PAY_PER_REQUEST`) — no capacity planning for the demo
- **TTL attribute:** `ttl` = epoch seconds, set to 90 days in the future

The write is performed with a **conditional `PutItem`**:

```
ConditionExpression = "attribute_not_exists(eventId)"
```

On `ConditionalCheckFailedException`, the processor treats the event as a duplicate: it logs a `duplicate_event_count` metric, **skips the Valkey `ZINCRBY`**, and acks the SQS message. This is the only thing that makes the at-least-once SQS delivery safe.

## Consequences

**Positive**

- Duplicates are impossible to double-count — the conditional check is atomic.
- DDB is the source of truth for Valkey rebuilds: `Query PK=gameId`, aggregate scores in code, bulk-`ZADD` back into Valkey.
- `PK=gameId`, `SK=ts#eventId` supports the natural replay-per-game access pattern. Adding a GSI later (e.g. `userId` for per-user history) is mechanical.
- On-demand billing makes cost predictable for the demo: roughly $1.25 per million write-request-units.
- TTL auto-prunes storage at 90 days; Valkey rebuild is still feasible within that window.

**Negative**

- Every event costs ~1 WCU plus the Valkey write — doubles the per-event cost vs. Valkey-only. Accepted as the price of correctness and durability.
- Hot-partition risk if one `gameId` receives the entire write traffic. At 5K TPS per game, DDB's adaptive capacity handles it, but an extreme single-game spike > 1000 WCU sustained would require either write-sharding (e.g. `gameId#shard`) or a different PK scheme. Flagged in the Production hardening checklist.
- DynamoDB Streams are not enabled by default in this ADR; enabling them later (for analytics or Valkey invalidation) is a one-flag change.

## Alternatives Considered

| Option | Why not |
|---|---|
| **No idempotency layer (rely on SQS FIFO)** | SQS FIFO's exactly-once is within a 5-minute deduplication window and is scoped to a message group. It does not protect against retries from the producer or cross-window duplicates, and (ADR-001) the 3,000 TPS per group ceiling is limiting. |
| **Idempotency key in Valkey (`SETNX eventId`)** | Works, but couples correctness to a non-durable store. A Valkey restart would lose the dedup state and re-apply all in-flight events. |
| **S3 as the raw event log** | Cheapest storage, but Put-per-event at 5K TPS creates a million tiny objects per day and Rebuild queries become a Glue/Athena exercise. DDB is operationally simpler at this scale. |
| **Kinesis Data Firehose → S3** | Great batching semantics, but adds a second ingestion system in parallel to SQS and doesn't give us conditional writes for idempotency. |
| **Provisioned DDB capacity** | Marginally cheaper if traffic is perfectly flat. Demo traffic is spiky (load buttons trigger bursts); on-demand's instant scaling is worth the ~15% premium. |

## References

- [DynamoDB Conditional Writes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html)
- [DynamoDB on-demand capacity mode](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/on-demand-capacity-mode.html)
- `/diagrams/sequence-write-path.drawio.png` — idempotency branch
- `/docs/ARCHITECTURE.md` — Valkey rebuild procedure
