# Architecture — Real-Time Leaderboard

This document is the detailed system specification for the real-time leaderboard demo. It is the single source of truth for how the system is put together and why. Diagrams live in `../diagrams/` and decision records in `./adr/`.

An engineer joining on Day 1 should be able to read this document end-to-end and start work on any component.

---

## 1. Overview

![Architecture overview](../diagrams/architecture-overview.drawio.png)

The system delivers two things:

1. **Near real-time leaderboards** for per-game score accumulation, targeted at ~100K gamers with 1–2 s end-to-end freshness.
2. **A self-contained demo experience**: a static web UI with a built-in load generator so stakeholders can drive the system live and watch the leaderboard move.

There are three logical flows:

- **Write path** — the Game Platform produces score events into SQS; a Lambda processor writes the event to DynamoDB (durable, idempotent) and increments the user's score in Valkey (hot path).
- **Read path** — the demo UI polls an HTTP API; a reader Lambda runs `ZREVRANGE` + `ZREVRANK` against Valkey and returns top-N + current user rank in a single response.
- **Demo control path** — the UI's "Start Load" button calls an API which starts a Step Functions workflow; a load-generator Lambda fans out `SendMessageBatch` calls into the same production SQS queue.

Everything runs in **a single AWS account, `us-east-1`** (see [ADR-006](./adr/ADR-006-single-account-demo.md)). Infrastructure is defined with AWS CDK (Python) using a nested-stack pattern (see [ADR-007](./adr/ADR-007-cdk-python-nested-stacks.md)). The cache engine is ElastiCache for Valkey 8.0 (see [ADR-008](./adr/ADR-008-valkey-over-redis.md)).

### Targets

| Metric | Target |
|---|---|
| Sustained ingest | **5,000 TPS** |
| End-to-end write latency (event produced → visible in `ZREVRANGE`) | **p95 < 2 s** |
| End-to-end read latency (API call → response) | **p95 < 200 ms** |
| Message loss | **0%** |
| Demo-mode monthly cost | **~$135/mo** running, **~$10/mo** spun up on demand |

---

## 2. Component responsibilities

| Service | Component | Responsibility |
|---|---|---|
| **Amazon SQS Standard** | `score-events-queue` | Absorbs producer bursts, decouples ingest from processing. Companion DLQ `score-events-dlq` captures poison messages after 5 redrive attempts. |
| **AWS Lambda (Python 3.12)** | `score-processor` | Consumes SQS in batches of 10 (window 1 s). For each event: conditional `PutItem` in DynamoDB (idempotency) then `ZINCRBY` in Valkey. Reports partial batch failures. |
| **AWS Lambda (Python 3.12)** | `leaderboard-reader` | Serves `GET /leaderboard` and `GET /rank/{user}`. Runs `ZREVRANGE`, `ZREVRANK`, `ZSCORE` against Valkey. Stateless. |
| **AWS Lambda (Python 3.12)** | `load-gen-trigger` | Validates the "Start Load" POST body from the UI and starts the Step Functions state machine. |
| **AWS Lambda (Python 3.12)** | `load-generator` | Sends synthetic score events to the SQS queue via `SendMessageBatch`. Invoked in parallel by the state machine's Map state. |
| **AWS Step Functions** | `load-generator-sm` | Orchestrates load runs. Map state fans out `load-generator` invocations with configurable TPS, duration, game IDs, user pool size. |
| **Amazon ElastiCache for Valkey** | `leaderboard-valkey` | In-memory sorted-set store. Valkey 8.0 (wire-compatible with Redis 7), `cache.r7g.large`, 1 primary + 1 replica, Multi-AZ. Holds `lb:{gameId}` ZSETs. In private subnets. |
| **Amazon DynamoDB** | `raw-events` table | Durable source of truth for all score events. Idempotency anchor (conditional `PutItem` on `eventId`). Valkey rebuild source. On-demand capacity. TTL 90 days. |
| **Amazon API Gateway (HTTP API)** | `leaderboard-api` | Public ingress for the demo UI. Routes `/leaderboard`, `/rank/*`, `/demo/start-load`. Lambda proxy integration. |
| **Amazon CloudFront** | CDN for demo UI | Serves the static demo UI from S3 on the default CloudFront domain (e.g. `dxxxx.cloudfront.net`). TLS at the edge with the default CloudFront certificate — no custom domain, no Route 53, no ACM. |
| **Amazon S3** | `demo-ui-bucket` | Stores the built demo UI (HTML/JS/CSS). Private; accessed only via CloudFront OAC. |
| **Amazon CloudWatch** | Logs, Metrics, Alarms | Lambda logs (via AWS Lambda Powertools for Python), API GW access logs, SQS queue depth, Valkey CPU/memory, DDB throttles. Single pane for demo operators. |

---

## 3. Data flow

### 3.1 Write path (step-by-step)

See [`../diagrams/sequence-write-path.drawio.png`](../diagrams/sequence-write-path.drawio.png).

1. The Game Platform calls `sqs:SendMessage` with a JSON payload: `{ eventId, userId, gameId, score, ts }`. `eventId` is a client-generated UUID — it is the idempotency key.
2. **Event Source Mapping** on `score-processor` polls the queue with `BatchSize=10`, `MaximumBatchingWindowInSeconds=1`. When either threshold hits, a Lambda invocation is created with up to 10 records.
3. The Lambda iterates the batch. For each record:
   - Call `DynamoDB.PutItem` on `raw-events` with `ConditionExpression = "attribute_not_exists(eventId)"`.
   - **If the write succeeds (new event):** call `Valkey.ZINCRBY lb:{gameId} <score> <userId>`. Ack the SQS record by returning success.
   - **If the write fails with `ConditionalCheckFailedException` (duplicate):** increment the `duplicate_event_count` metric, **skip the Valkey write**, and ack the SQS record. The event has already been applied by a previous attempt.
   - **If the write fails with any other exception (Valkey unreachable, DDB throttle, Lambda crash):** add the record's `itemIdentifier` to `batchItemFailures` in the response. SQS will re-drive just that record after the visibility timeout.
4. After 5 failed redrive attempts, SQS moves the record to `score-events-dlq`. An alarm wakes the on-call.

Latency budget (end-to-end write): SQS send ≤ 50 ms + ESM poll/batch window ≤ 1 s + Lambda invoke ≤ 50 ms + DDB PutItem ≤ 20 ms + Valkey ZINCRBY ≤ 5 ms → **p95 well under 2 s**.

### 3.2 Read path (step-by-step)

See [`../diagrams/sequence-read-path.drawio.png`](../diagrams/sequence-read-path.drawio.png).

1. Demo UI (browser) calls `GET /leaderboard?gameId=G1&limit=100&userId=U42` every 1 s via `setInterval`.
2. CloudFront routes the request to API Gateway HTTP API (or the browser hits API Gateway directly — both are fine).
3. API Gateway proxy-integrates to `leaderboard-reader` Lambda.
4. The Lambda runs, in parallel within a single Valkey connection:
   - `ZREVRANGE lb:G1 0 99 WITHSCORES` — top 100 players, sorted descending.
   - `ZREVRANK lb:G1 U42` — rank of the calling user (0-indexed, descending).
   - `ZSCORE lb:G1 U42` — score of the calling user.
5. The Lambda builds the response: `{ top: [{ userId, score }, ...], me: { rank, score } }` and returns.

Latency budget (read): API GW ≤ 20 ms + Lambda invoke ≤ 30 ms (warm) + 2× Valkey RTT ≤ 10 ms → **p95 well under 200 ms**.

### 3.3 Demo control path

1. Demo UI calls `POST /demo/start-load` with `{ tps, durationSec, gameIds, userPoolSize }`.
2. `load-gen-trigger` validates the body, constructs a Step Functions input, and calls `StartExecution` on `load-generator-sm`.
3. The state machine's Map state fans out to `load-generator` Lambda invocations. Concurrency is derived from `tps`: one Lambda producing ~200 events/second × N Lambdas = target TPS.
4. Each `load-generator` Lambda loops until its share of `durationSec` elapses, calling `SendMessageBatch` (up to 10 messages/call) into `score-events-queue`. Events are tagged `sourceGame="loadgen"`.
5. The Map state completes; execution ends. Cost stops accruing.

---

## 4. Valkey key schema (detailed)

See [`../diagrams/data-model.drawio.png`](../diagrams/data-model.drawio.png).

### 4.1 Primary pattern

| Element | Value |
|---|---|
| Key | `lb:{gameId}` |
| Type | `ZSET` (Sorted Set) |
| Member | `userId` (string) |
| Score | float — cumulative score for the user in the game |

### 4.2 Operations

```
# Ingest
ZINCRBY  lb:{gameId}  <delta>  <userId>            -- O(log N)

# Read
ZREVRANGE  lb:{gameId}  0  <limit-1>  WITHSCORES   -- O(log N + M) where M = limit
ZREVRANK   lb:{gameId}  <userId>                   -- O(log N)
ZSCORE     lb:{gameId}  <userId>                   -- O(1)
ZCARD      lb:{gameId}                             -- O(1)  (useful for "pct rank" calc)
```

### 4.3 Extensibility: period-based resets

Keep the lifetime board at `lb:{gameId}`. Add **parallel keys** for time-boxed boards:

```
lb:{gameId}                     -- lifetime (never expires)
lb:{gameId}:{period}            -- rolling window

Examples:
  lb:arena-shooter:2026-05-06   -- daily  (YYYY-MM-DD)
  lb:arena-shooter:2026-W19     -- weekly (ISO week)
  lb:arena-shooter:season-07    -- season (explicit label)
```

At rollover, set `EXPIRE` on the period key (e.g. 14 days past end of period) so old boards self-delete. The write path issues both ZINCRBYs in one `MULTI`/`EXEC` block when periods are enabled.

### 4.4 Memory sizing

- ~100K unique users per game × ~64 B per ZSET member ≈ **6.4 MB per game board**.
- `cache.r7g.large` = ~13 GB usable → headroom for ~2000 per-game boards (with all periods enabled, still well within capacity).
- Monitor `DatabaseMemoryUsagePercentage`. Alarm at 70%.

---

## 5. DynamoDB table design

| Property | Value |
|---|---|
| Table name | `raw-events` |
| Partition key | `gameId` (String) |
| Sort key | `ts#eventId` (String) — ISO-8601 timestamp + `#` + UUID |
| Billing mode | On-Demand (`PAY_PER_REQUEST`) |
| TTL attribute | `ttl` (Number, epoch seconds, set to 90 days in the future) |
| Encryption | AWS-managed KMS key (`aws/dynamodb`) |

### 5.1 Attributes

| Attribute | Type | Purpose |
|---|---|---|
| `eventId` | String | Globally unique; used for idempotency (`attribute_not_exists(eventId)`) |
| `userId` | String | Player identifier |
| `score` | Number | Score **delta** for this event |
| `ingestedAt` | String | ISO-8601 timestamp recorded by the Lambda |
| `sourceGame` | String | Optional: `"prod"` or `"loadgen"` — useful for filtering demo traffic |
| `ttl` | Number | Epoch seconds for DDB TTL auto-cleanup |

### 5.2 Access patterns

| # | Operation | Pattern |
|---|---|---|
| 1 | Ingest write | `PutItem` with `ConditionExpression` |
| 2 | Replay per game | `Query PK=gameId, SK BETWEEN t0 AND t1` |
| 3 | Rebuild Valkey | `Query PK=gameId`, stream results, aggregate scores per `userId` in memory, `ZADD` bulk into Valkey |
| 4 | Analytics export (deferred) | DDB Streams → Firehose → S3 → Athena / Glue |

### 5.3 Capacity expectations

At 5K TPS sustained write: ~5K WCU/s. On-demand scales instantly; cost is ~$1.25/M WRU. For a 30-minute demo at 5K TPS: 9M writes → ~$11.

---

## 6. Scaling characteristics

### 6.1 Why 5K TPS works

| Stage | Capacity at default settings | Headroom at 5K TPS |
|---|---|---|
| SQS Standard | Unlimited TPS (AWS docs: "nearly unlimited") | ∞ |
| Lambda ESM polling | Up to ~60 concurrent pollers per event source | 60 × 10 msgs/poll × 10 polls/s = 6,000 msgs/s ingested |
| `score-processor` concurrency | Reserved concurrency 50 | 50 × (10 msgs / ~100 ms invoke) = 5,000 msgs/s processed |
| DDB PutItem | On-demand; AWS pre-warms to 2× prior peak within 30 min | 5K WCU/s is within on-demand's default ceiling |
| Valkey `ZINCRBY` | `cache.r7g.large` sustains ~200K ops/s on a single node | 40× headroom |
| API GW HTTP API | 10,000 RPS default account quota | 5K TPS is half |

### 6.2 Lambda concurrency math

- Each `score-processor` invocation handles a batch of 10 events.
- Assume processing time (DDB + Valkey + overhead) = ~100 ms p50, ~200 ms p95.
- Throughput per concurrent Lambda = 10 / 0.2 = **50 events/s** (p95).
- To sustain 5,000 events/s → **~100 concurrent Lambdas** at p95, or ~50 at p50.
- Set **reserved concurrency = 50** as the floor (ensures capacity) and let Lambda burst to 100+ for p95 spikes. Account-level concurrency (1000 by default) gives plenty of headroom.

### 6.3 Valkey headroom

- `cache.r7g.large` = 2 vCPU / 13.07 GiB memory. Benchmarks: ~200K-300K ops/s single-thread with small ZSET payloads. Valkey 8.0 is wire-compatible with Redis 7, so published Redis benchmarks apply.
- At 5K TPS + 1 Hz polling from, say, 50 viewers × 3 Valkey ops/poll = 5,150 ops/s.
- CPU utilization under 5% during the demo. Memory utilization < 1% at demo scale.
- Bottleneck would be network bandwidth only at 100× this scale.

---

## 7. Configuration values

| Parameter | Value | Rationale |
|---|---|---|
| SQS `VisibilityTimeout` | `180s` | 6× the Lambda timeout (30 s) per AWS guidance |
| SQS `MaxReceiveCount` | `5` | Standard redrive policy before DLQ |
| SQS `MessageRetentionPeriod` | `4 days` | Plenty of buffer for recovery scenarios |
| Lambda runtime (all functions) | `python3.12` | Locked in per ADR — Python throughout (app + CDK) |
| Lambda `MemorySize` (score-processor) | `512 MB` | CPU scales with memory; 512 MB is the sweet spot for DDB+Valkey client overhead |
| Lambda `Timeout` (score-processor) | `30s` | Accommodates retry + backoff within a single invocation |
| Lambda `ReservedConcurrency` (score-processor) | `50` | Floor for 5K TPS per § 6.2; account quota provides burst |
| Lambda `MemorySize` (reader) | `256 MB` | Pure network I/O; 256 MB is sufficient |
| Lambda `Timeout` (reader) | `5s` | Fast-fail if Valkey is sluggish |
| Event Source Mapping `BatchSize` | `10` | Max for SQS; best amortization |
| Event Source Mapping `MaximumBatchingWindowInSeconds` | `1` | Caps latency at 1 s even when queue is nearly empty |
| Event Source Mapping `ReportBatchItemFailures` | `true` | Partial batch retry |
| ElastiCache engine | Valkey `8.0` | Wire-compatible with Redis 7 API (`ZINCRBY` / `ZREVRANGE` unchanged); ~20% cheaper than Redis on the same node type |
| ElastiCache node type | `cache.r7g.large` | Graviton3 — best price/perf for an in-memory sorted-set store at this size |
| ElastiCache topology | 1 primary + 1 replica, Multi-AZ | Survives a single-AZ failure with automatic failover |
| Valkey client library | `valkey-glide` (preferred) / `redis-py` (fallback) | `valkey-glide` is the AWS-official Python client; `redis-py` is wire-compatible and acceptable |
| DDB billing mode | On-Demand | No capacity planning; ~15% premium worth the simplicity |
| DDB TTL attribute | `ttl` | 90-day auto-prune |
| API GW type | HTTP API | 71% cheaper than REST API, sufficient feature set |
| CloudFront | Default domain + OAC | Default CloudFront domain (`dxxxx.cloudfront.net`) + default CloudFront cert. No custom domain / Route 53 / ACM. |

---

## 8. Failure modes & recovery

### 8.1 DLQ strategy

- A record lands in `score-events-dlq` after 5 redrive attempts. Root causes are almost always: malformed payload, Valkey extended outage, or a code bug in the processor.
- **Alarm:** `ApproximateNumberOfMessagesVisible > 0 for 1 minute` on the DLQ.
- **Operator playbook:** inspect messages in the console; fix the processor or the malformed producer; redrive the DLQ back to the main queue (`StartMessageMoveTask`) once the root cause is resolved.

### 8.2 Valkey node failure

- ElastiCache Multi-AZ with a replica performs automatic failover (typically < 60 s). During failover, `ZINCRBY` writes will retry (the processor surfaces them as `batchItemFailures` and SQS re-drives).
- If **both** nodes are lost (catastrophic): see § 8.3.

### 8.3 Valkey rebuild from DynamoDB

When Valkey state is lost or corrupted:

1. **Stop the processor** (or set reserved concurrency to 0) to freeze further writes to Valkey.
2. **Query DynamoDB** per `gameId`:
   ```
   aws dynamodb query \
     --table-name raw-events \
     --key-condition-expression "gameId = :g" \
     --expression-attribute-values '{":g":{"S":"<gameId>"}}'
   ```
3. **Aggregate** in memory: for each record, sum `score` per `userId` within the desired time window (lifetime, or a specific period key).
4. **Bulk-load** into Valkey with `ZADD` — one pipelined call per N users:
   ```
   ZADD lb:<gameId> <score1> <user1> <score2> <user2> ...
   ```
5. **Resume the processor** (restore reserved concurrency).
6. Any events that were queued in SQS during the outage will re-drive naturally; the DDB conditional write will skip anything already persisted.

At 100K users × 1000 games, a rebuild takes on the order of tens of seconds (DDB Query at ~1K items/s per partition). For the demo's scale it's under a minute.

### 8.4 SQS poison pill

- A malformed message that causes a parse exception will fail all 5 retries and land in the DLQ. Because we use `batchItemFailures`, one poison message does not block its batch-mates.

### 8.5 Lambda throttling

- If reserved concurrency is hit, SQS ESM backs off and retries. Events are not lost; latency increases.
- CloudWatch alarm: `Throttles > 0 for 5 min` on `score-processor`.

---

## 9. Security posture (demo)

This section describes the **demo** security posture. The Production hardening checklist (§ 11) tightens it further.

- **Network**
  - ElastiCache and the Lambdas that talk to it (`score-processor`, `leaderboard-reader`) run in **private subnets**. No public IPs.
  - Valkey AUTH enabled; credentials retrieved at cold start from AWS Secrets Manager.
  - TLS in-transit enabled on ElastiCache.
  - Security group on ElastiCache: ingress only from the Lambda SG on port 6379.
- **IAM — least privilege per Lambda**
  - `score-processor` — only `sqs:ReceiveMessage / DeleteMessage / ChangeMessageVisibility` on the single queue, `dynamodb:PutItem` on `raw-events`, `secretsmanager:GetSecretValue` on the Valkey secret.
  - `leaderboard-reader` — only `secretsmanager:GetSecretValue` on the Valkey secret.
  - `load-generator` — only `sqs:SendMessage` / `sqs:SendMessageBatch` on the single queue.
  - `load-gen-trigger` — only `states:StartExecution` on `load-generator-sm`.
- **API Gateway**
  - HTTP API over TLS 1.2+. This is the only public endpoint.
  - No authn on the demo API (public read-only leaderboard). Flagged for the Production hardening checklist.
  - Throttling: 10,000 RPS burst / 5,000 RPS steady at account level — unchanged defaults.
- **Storage**
  - DynamoDB at-rest encryption with AWS-managed KMS key.
  - S3 demo-ui bucket private, accessed only via CloudFront OAC. Server-side encryption (SSE-S3).
  - Valkey snapshot encryption enabled.
- **Observability**
  - CloudWatch Logs for every Lambda (structured logging via AWS Lambda Powertools for Python); API GW access logs enabled.
  - No PII in logs. Player IDs are opaque.

---

## 10. Cost estimate (demo-sized, monthly)

Pricing is for `us-east-1` (the demo's pinned region). Figures rounded. No hard budget cap is set for this demo environment — estimates are for cost awareness and teardown hygiene, not gating.

| Line | Quantity | Monthly |
|---|---|---|
| **ElastiCache for Valkey** `cache.r7g.large` × 2 nodes (primary + replica) | 24 × 30 × 2 × ~$0.066/hr (Valkey engine-hour, ~20% cheaper than Redis) | **~$96** |
| **Lambda** invocations + GB-s (processor 512 MB + reader 256 MB, Python 3.12, ~5K TPS for a handful of hours/month of active demo) | see § 10.1 | **~$10** |
| **API Gateway HTTP API** | ~5M requests/month during active demos | **~$5** |
| **SQS Standard** | ~5M messages + DLQ | **~$2** |
| **DynamoDB On-Demand** | ~5M writes, ~5M reads, 1 GB storage | **~$15** |
| **CloudFront + S3** | demo UI, low traffic, default CloudFront domain (no ACM / Route 53 cost) | **~$5** |
| **CloudWatch Logs + Metrics** | default retention | **~$3** |
| **Total** (left running 24/7) | | **~$135/mo** |
| **Spun up on demand** (only for scheduled demos, ~10 hr/month) | ElastiCache dominates; scale to zero = remove cluster when idle | **~$10/mo** |

### 10.1 Notes

- **ElastiCache is the only line that benefits from scheduled teardown.** A quick tear-down script can drop it during quiet periods and a restore script (`cdk deploy` + Valkey rebuild from DDB) can bring it back in ~5 minutes.
- Valkey 8.0 is the default engine (see ADR-008) and already delivers the ~20% saving over Redis on ElastiCache. If cost compresses further, **ElastiCache Serverless** is an alternative (≈30% additional savings for spiky workloads). Architecture unchanged.
- At production scale (permanent operation, 10× demo traffic), costs scale roughly linearly for Lambda/SQS/DDB and sub-linearly for ElastiCache (same cluster handles much more load).

---

## 11. Production hardening checklist

The items below are **explicitly deferred** for the demo. Before productionizing this system, each should be revisited.

- [ ] **AuthN / AuthZ** on the read API (Cognito JWT, signed URLs, or a service-to-service token). Currently open.
- [ ] **Rate limiting per caller** — throttling at the caller level, not just account level.
- [ ] **Multi-region** strategy: DynamoDB Global Tables for raw events, Valkey replication or per-region read-locals.
- [ ] **Multi-account** separation (producer in one account, consumer / analytics in others) per customer org standards — see [ADR-006](./adr/ADR-006-single-account-demo.md).
- [ ] **Valkey cluster mode** — if a single game board grows past ~50 MB or ~1M members, shard across slots.
- [ ] **Season / period rollover automation** — a scheduled Lambda that creates new period keys, sets EXPIRE on old ones, and archives finalized boards to DDB / S3.
- [ ] **Cheat detection / anti-abuse** — signed event tokens from the Game Platform, rate limits per user, anomaly detection on score deltas.
- [ ] **Multi-tenant isolation** — if the platform hosts multiple game publishers, partition keys and quotas per tenant.
- [ ] **Backup / DR** beyond the 90-day DDB TTL — point-in-time recovery on DDB, snapshot retention policy for ElastiCache.
- [ ] **Observability upgrades** — X-Ray end-to-end tracing, structured logging with correlation IDs, custom business metrics (events per game, p99 latency per route).
- [ ] **WAF** in front of API Gateway / CloudFront.
- [ ] **CI/CD** with staged rollouts and automated rollback on alarm.
- [ ] **Load test suite** that runs on every change to catch regressions before production.

---

## 12. References

- [ADR index](./adr/README.md)
- [Architecture overview diagram](../diagrams/architecture-overview.drawio.png)
- [Write-path sequence](../diagrams/sequence-write-path.drawio.png)
- [Read-path sequence](../diagrams/sequence-read-path.drawio.png)
- [Data model](../diagrams/data-model.drawio.png)
- [Deployment & ops runbook](./RUNBOOK.md)
