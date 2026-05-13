# ADR-001: Ingest score events with SQS Standard + Lambda Event Source Mapping

- **Status:** Accepted
- **Date:** 2026-05-06
- **Deciders:** Solutions Architect, Leaderboard Demo

## Context

The Game Platform emits score events at up to ~5,000 TPS sustained during demo scenarios and short bursts above that during load tests. The ingestion layer must:

- Absorb bursts without back-pressuring the producer.
- Guarantee **0% message loss** (target in the project brief).
- Deliver events to a processor that writes to both Valkey (hot path) and DynamoDB (durable).
- Run inside the customer's existing single AWS account — no cross-account fan-out required today.
- Be operable by a small team; primary owner is on-call during the demo window.

Near-real-time delivery (1–2 s end-to-end) is acceptable; sub-second is not required.

## Decision

Use **Amazon SQS Standard** as the ingest buffer and **AWS Lambda via Event Source Mapping (ESM)** with `BatchSize=10` and `MaximumBatchingWindowInSeconds=1` as the consumer.

- One `score-events-queue` with a companion `score-events-dlq` (redrive policy `maxReceiveCount=5`).
- Visibility timeout = 6× the Lambda timeout (AWS recommended ratio).
- Lambda reports partial batch failures via `batchItemFailures` so a single bad record does not re-drive the whole batch.

## Consequences

**Positive**

- At-least-once delivery + conditional write in DynamoDB (see ADR-003) = exactly-once effect.
- Native Lambda ESM handles polling, scaling, and DLQ plumbing — no custom consumer.
- Batch of 10 with 1 s window amortizes Lambda invocation cost and stays within the 1–2 s latency budget.
- Cheapest AWS-native option that meets the requirements (see cost note in ARCHITECTURE.md).

**Negative**

- SQS Standard is **not FIFO**: events can arrive out of order. Acceptable because the scoring semantics are additive (`ZINCRBY`); order of `+5` and `+3` does not change the final score.
- At-least-once implies duplicates — mitigated by the idempotency key in ADR-003.
- Per-message overhead (SQS $0.40/M + Lambda invocation) scales linearly with traffic; batching keeps this bounded.

## Alternatives Considered

| Option | Why not |
|---|---|
| **Amazon Kinesis Data Streams** | Great for ordered, replayable stream processing and fan-out to many consumers. Here we have one consumer and no replay requirement beyond what DDB already gives us. Shard management and per-shard provisioned throughput add operational burden the demo does not need. |
| **Amazon EventBridge** | Excellent for event-driven routing across many targets, but targets are invoked one event at a time (no native batch of 10 to Lambda), and at $1.00/M events it's 2.5× the cost of SQS at the same volume. Fan-out is not needed today. |
| **Amazon MSK (Managed Kafka)** | Overkill for a 5K TPS single-consumer demo. Broker cluster cost, Zookeeper/KRaft ops burden, and topic governance do not pay back here. |
| **Direct API Gateway → Lambda (no queue)** | Loses the buffer. A burst above reserved concurrency would 429 the producer and violate the 0% loss target. Also couples the Game Platform's availability to Lambda cold starts. |
| **SQS FIFO** | 3,000 TPS per message group ceiling without batching. Ordering is not required (see "Negative" above), so paying the throughput tax is wasteful. |

## References

- [AWS Lambda: Using Lambda with Amazon SQS](https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html)
- [Reporting batch item failures for Lambda](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html)
- `/docs/ARCHITECTURE.md` — scaling math
- `/docs/adr/ADR-003-dynamodb-raw-events.md` — idempotency
