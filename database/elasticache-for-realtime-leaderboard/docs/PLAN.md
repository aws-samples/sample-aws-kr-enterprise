# Project Plan

Delivery plan for building the real-time leaderboard demo system. This is a living document — as customer input arrives and implementation uncovers constraints, phases and tasks are refined in place.

The team's scope is **development of the demo system only**. Customer-facing walkthrough and delivery of the live demo are out of scope and handled separately by the user.

## Goals

- Build a credible, AWS-native real-time leaderboard pipeline that a Game Platform engineer can fork, deploy, and extend.
- Hit the agreed performance envelope: 5,000 TPS sustained, p95 end-to-end latency under 2 seconds, zero message loss.
- Produce a public-quality open-source reference with first-class IaC, runbook, and ADRs.
- Keep total running cost bounded via a single-command teardown path (`cdk destroy`).

## Non-Goals

- Production hardening for a live title (multi-region, per-game sharding, auth, anti-cheat).
- Cross-account or cross-organization integration.
- Performance beyond 5,000 TPS in the demo environment.
- Score reset automation until the customer confirms cadence (daily / weekly / season).
- A mobile-native client — the demo page is browser-only.
- Customer-facing walkthrough materials (scripts, slides, delivery logistics).

## Scope

### In Scope

- Write pipeline: SQS Standard → Lambda (Python 3.12, ESM, batch size 10, batching window 1s) → ElastiCache for Valkey 8.0 `ZINCRBY` + DynamoDB conditional write for idempotency.
- Read API: API Gateway HTTP API → Lambda (Python 3.12) → Valkey `ZREVRANGE` / `ZREVRANK`.
- Demo web page: S3 + CloudFront (OAC), default CloudFront domain, live leaderboard view, load-generator control buttons, embedded CloudWatch metrics.
- Load generator: Step Functions orchestrating a Lambda injector for ramp/sustain/burst traffic patterns.
- Observability: CloudWatch dashboard, structured logs (AWS Lambda Powertools for Python), key alarms.
- IaC for everything: AWS CDK in Python with nested stacks (root `LeaderboardApp` + 6 child stacks — see ADR-007).
- Developer documentation: README, ARCHITECTURE, ADRs, RUNBOOK, CONTRIBUTING.

### Out of Scope

- Real game-client integration (the demo uses a synthetic injector).
- Authentication on the read API (listed as a production-hardening item in ARCHITECTURE.md, not implemented).
- Anti-cheat / score validation logic beyond idempotency.
- Multi-region failover.
- Custom CloudWatch dashboards beyond the one embedded in the demo web page.
- Customer-facing demo execution, timing, and walkthrough content.

### Deferred (Customer-Dependent)

These are flagged for the customer to confirm; defaults are used until we hear otherwise.

- **Leaderboard reset cadence.** Default: permanent accumulation.
- **Score metric definition.** Default: integer points, `ZINCRBY` delta.
- **Actual peak TPS and burst shape.** Default: 5,000 TPS sustained, short bursts to 2× allowed.
- **Event schema fields beyond `gameId` / `userId` / `scoreDelta` / `eventId` / `timestamp`.**
- **Data retention on DynamoDB raw events.** Default: 90 days via TTL.

## Acceptance Principles

All phase exit criteria follow these principles:

1. **Numbers only.** Every criterion is a numeric threshold (latency, throughput, count, bytes).
2. **Automated.** A script decides pass/fail via its exit code. No human judgment in the loop.
3. **Reproducible.** Same command, same result. Explicit parameters; random seeds logged.
4. **Specific failure output.** A failing script prints the actual observed value and the target it missed.
5. **No "close enough".** Missing a threshold by any amount is a fail unless the criterion explicitly states a tolerance.
6. **Two consecutive runs.** A criterion is considered met only when two independent runs both pass. One-off passes don't count.
7. **No competing load.** The measurement account must be free of other workloads during the run.

## Phases

The project is delivered in two development phases. "Done" is defined by Phase 2 exit criteria. Anything after Phase 2 (customer walkthrough, timing, teardown scheduling) is out of the team's scope.

### Phase 1 — Core Pipeline (Week 1)

Goal: end-to-end write and read path working in a developer account, validated by a simple script.

| Day | Tasks |
|---|---|
| 1 | CDK bootstrap (`cdk bootstrap` once per account/region in `us-east-1`); Python CDK app scaffolding; root `LeaderboardApp` stack with stubs for the six nested stacks (Network, Data, Ingest, Api, LoadGen, Web); CI placeholder |
| 2 | `NetworkStack` (VPC + 2 private subnets across 2 AZs + security groups); `DataStack` — SQS queue + DLQ, DynamoDB table (PK `gameId`, SK `ts#eventId`, TTL attribute), ElastiCache for Valkey 8.0 `cache.r7g.large`, 1 primary + 1 replica, Multi-AZ (matches production topology from day one) |
| 3 | `IngestStack` — Processor Lambda (Python 3.12): SQS ESM (batch=10, window=1s), idempotency via DynamoDB `ConditionExpression`, `ZINCRBY` to Valkey. Must emit custom CloudWatch EMF metrics `end_to_end_latency_ms` (event `ts` → processor receive time) and `duplicate_event_count` (increment on `ConditionalCheckFailedException`) — required instrumentation for Phase 1 exit criteria P1-002 and P1-003. |
| 4 | `ApiStack` — Reader Lambda (Python 3.12) + API Gateway HTTP API; top-N and user-rank endpoints; IAM least privilege |
| 5 | End-to-end smoke test with a local script producing 100 events; verify leaderboard accuracy and idempotency under duplicate events |

**Exit criteria for Phase 1**

Phase 1 is complete when all seven automated verification scripts below exit 0 in **two consecutive independent runs**. The wrapper `make phase1-exit` runs them twice and fails on any non-zero exit. No manual judgment — numbers decide. No "close enough" tolerance unless explicitly noted.

| ID | Script | Behavior | Pass condition | Failure message format |
|---|---|---|---|---|
| P1-001 | `app/scripts/test_p1_smoke.py` | Inject 1,000 events with unique `eventId`, 3–5 games (`arena-shooter`, `puzzle-01`, `racing-mini`), 100 users × random `scoreDelta`. Wait 30 s, then compare DDB + Valkey aggregates. | DDB item count = 1,000 exactly. For every `(gameId, userId)`: `ZSCORE` = `SUM(scoreDelta)` in DDB, exact. | `FAIL[P1-001]: expected 1000 DDB items, got 997 (missing eventIds: [...])` |
| P1-002 | `app/scripts/test_p1_idempotency.py` | Send the same `eventId` 100 times. Wait 10 s. | DDB has exactly 1 item for that `eventId`. `ZSCORE` increment = `scoreDelta` × 1. CloudWatch `duplicate_event_count` metric = 99. | `FAIL[P1-002]: ZSCORE drift: expected 50, got 4950 (duplicates applied)` |
| P1-003 | `app/scripts/test_p1_write_latency.py` | Inject 500 TPS for 3 minutes. Processor Lambda emits `end_to_end_latency_ms` via CloudWatch EMF. Query the 3-minute window. | p50 < 800 ms, **p95 < 2,000 ms**, p99 < 3,000 ms. | `FAIL[P1-003]: p95=2340ms > 2000ms over 180s window (2026-05-06T10:30:00Z)` |
| P1-004 | `app/scripts/test_p1_read_latency.py` | Seed 500 users, then drive 100 req/s for 60 s against `GET /leaderboard`. Measure both API GW `Latency` and client-side RTT. | API GW `Latency` p95 < 100 ms. Client RTT p95 < 200 ms. | `FAIL[P1-004]: API GW p95=142ms > 100ms` |
| P1-005 | `app/scripts/test_p1_dlq_clean.py` | After P1-001 completes, wait 2 minutes, then read the DLQ. | DLQ `ApproximateNumberOfMessages` = 0. | `FAIL[P1-005]: DLQ depth=3, expected 0` |
| P1-006 | `app/scripts/test_p1_rebuild.py` | Seed 1,000 events. Run `FLUSHDB` on Valkey. Run `rebuild_from_ddb.py`. Compare `ZREVRANGE 0 -1 WITHSCORES` before and after. | All ZSET snapshots byte-identical (rank, userId, score) before and after rebuild. | `FAIL[P1-006]: lb:arena-shooter drift at rank 7: before=user_88/91250, after=user_91/91200` |
| P1-007 | `app/scripts/test_p1_scale_100k.py` | Inject events for **100,000 unique users** in `arena-shooter` (1–5 events each, random `scoreDelta`). Verify ZSET shape and sizing. | `ZCARD lb:arena-shooter` = 100,000 exactly. `ZREVRANGE 0 99` p95 < 10 ms. 10 random `ZREVRANK` samples all match DDB aggregate. Valkey `used_memory` < 50 MB. | `FAIL[P1-007]: ZCARD=99987, expected 100000 (missing 13 members)` |

**Gate**: `make phase1-exit` runs P1-001 through P1-007 twice end-to-end. Any non-zero exit from any script in either run fails the gate.

**Prerequisite**: The processor Lambda must emit the custom CloudWatch EMF metrics `end_to_end_latency_ms` and `duplicate_event_count` — this is a required part of Day 3 work (see the Phases table above) and is the instrumentation basis for P1-002 and P1-003.

### Phase 2 — Demo Surface (Week 2)

Goal: the demo web page is usable by a non-engineer against the deployed backend, and the load generator can drive the system through ramp, sustain, and burst patterns.

| Day | Tasks |
|---|---|
| 6 | `WebStack` — Demo web page skeleton (plain React or vanilla TS — decide in kickoff); S3 bucket (all-public-blocked) + CloudFront with OAC on the default CloudFront domain |
| 7 | Live leaderboard view, per-game selector, polling loop against read API |
| 8 | `LoadGenStack` — Load-generator Lambda (Python 3.12) injector + `load-gen-trigger`; Step Functions state machine for ramp/sustain/burst patterns |
| 9 | Demo web page control panel — buttons that start/stop each traffic pattern via API Gateway → Step Functions |
| 10 | Embedded CloudWatch dashboard (embed code or pre-signed dashboard URL). Widgets: SQS `ApproximateNumberOfMessagesVisible`, Lambda `Invocations`, Lambda `Errors`, Valkey `EngineCPUUtilization`, end-to-end latency (custom EMF from Day 3). Widget inventory is tested by Phase 2 exit criterion P2-006 — the script fails if any of these five widgets has no recent datapoints. |

**Exit criteria for Phase 2**

Phase 2 is complete when all eight automated verification scripts below exit 0 in **two consecutive independent runs**. The wrapper `make phase2-exit` runs them twice. Numbers decide. No manual judgment.

| ID | Script | Behavior | Pass condition | Failure message format |
|---|---|---|---|---|
| P2-001 | `app/scripts/test_p2_sustained_5k.py` | Start Step Functions "5K × 5min" pattern. Sample `NumberOfMessagesSent` every second for 5 minutes. | Over the best **4-minute continuous window**, average ≥ 4,800 TPS (4% tolerance). Within that window, write p95 < 2,000 ms. | `FAIL[P2-001]: sustained TPS=4612 over best 4min window, below 4800 threshold` |
| P2-002 | `app/scripts/test_p2_burst.py` | 500 TPS baseline → ramp to 5,000 TPS over 10 s → sustain 60 s → back to baseline. Track every sent event against DDB commits. | Sent count = DDB item count (exact). SQS depth returns < 100 within 2 min of burst end. DLQ increase = 0. | `FAIL[P2-002]: sent=305000, DDB items=304987 (13 lost)` |
| P2-003 | `app/scripts/test_p2_ui_freshness.py` | Playwright (or equivalent) opens the demo page. Inject 100 TPS. Measure wall-clock from `event.ts` to DOM render of that `userId` score. Collect 30 samples. | `event.ts → DOM render` **p95 < 2,500 ms**. | `FAIL[P2-003]: UI freshness p95=3120ms > 2500ms` |
| P2-004 | `app/scripts/test_p2_button_reproducibility.py` | Invoke the demo UI's "Sustain 5K × 1min" button API twice. Compare DDB item counts for each run. | Difference in injected count between the two runs < 1%. | `FAIL[P2-004]: run1=298432 events, run2=281204 events, delta=5.8% > 1%` |
| P2-005 | `app/scripts/test_p2_page_load.py` | Lighthouse CI cold-load the CloudFront URL 3 times (after CloudFront invalidation). | LCP p95 < 2,500 ms. FCP < 1,800 ms. TTI < 3,500 ms. | `FAIL[P2-005]: LCP p95=3150ms > 2500ms` |
| P2-006 | `app/scripts/test_p2_dashboard.py` | Query the embedded CloudWatch dashboard's backing metrics: SQS depth, Lambda `Invocations`, Lambda `Errors`, Valkey `EngineCPUUtilization`, end-to-end latency. | All 5 widgets return ≥ 1 datapoint in the last 5 minutes. | `FAIL[P2-006]: widget 'Valkey EngineCPU' returned 0 datapoints in last 5min` |
| P2-007 | `app/scripts/test_p2_scale_and_load.py` | Seed 100,000 users in `arena-shooter`. **While that ZSET is populated**, drive 5K TPS for 3 minutes mixing revisits and new users. | Write latency p95 < 2,000 ms. Read latency p95 < 100 ms. Valkey `EngineCPU` peak < 70%. | `FAIL[P2-007]: read p95=128ms > 100ms at 100K ZSET cardinality` |
| P2-008 | `app/scripts/test_p2_top_response.py` | With 100K users seeded, call `GET /leaderboard?limit=100&userId=<random>` and validate shape. | Response body < 20 KB. `top[]` strictly descending by score. All 100 `userId`s unique. If `me.rank < 100`, the corresponding `top[]` entry matches. | `FAIL[P2-008]: rank inversion at index 47 (score=8120) vs index 48 (score=8145)` |

**Gate**: `make phase2-exit` runs P2-001 through P2-008 twice end-to-end. Any non-zero exit fails the gate. Project is "done" when this gate passes.

**Prerequisites**: The dashboard widgets referenced in P2-006 must be the ones wired up in Day 10's CloudWatch dashboard deliverable. The `/leaderboard` response schema tested in P2-008 must match the API contract fixed in Day 4 (`ApiStack`).

## Success Criteria

Developer-verifiable metrics; all measurable from CloudWatch or local instrumentation.

| Metric | Target | Measurement Method |
|---|---|---|
| Sustained ingestion throughput | 5,000 TPS for 5 minutes | Injector reports sent count; CloudWatch SQS `NumberOfMessagesSent` |
| End-to-end latency (p50) | < 800 ms | Custom CloudWatch metric: processor ingest timestamp minus event timestamp |
| End-to-end latency (p95) | < 2,000 ms | Custom CloudWatch metric, p95 aggregation |
| Message loss | 0% | Injector sent count equals processor committed count (minus intentional DLQ during failure-injection tests) |
| Read API latency (p95) | < 100 ms | API Gateway CloudWatch metric |
| Recovery from processor failure | < 60 s to resume draining after restart | SQS `ApproximateNumberOfMessagesVisible` trend |
| Idempotency under duplicate delivery | Duplicate `eventId` produces exactly one `ZINCRBY` | Inspect DDB `ConditionalCheckFailedException` count; compare with processor metric |
| Valkey rebuild correctness | Post-rebuild `ZSCORE` matches DDB-aggregated sum per user | Compare rebuild script output against expected sums for a seeded dataset |

## Risks and Mitigations

Technical and development-side risks only. Customer walkthrough and venue-specific risks are out of scope.

| Risk | Impact | Mitigation |
|---|---|---|
| Valkey becomes the bottleneck before 5K TPS | Misses performance target | Use `cache.r7g.large`; pre-flight load test at Phase 1 exit; fall back to `r7g.xlarge` if needed |
| Lambda cold starts amplify p95 during burst onset | p95 latency misses target on traffic ramp | Pre-warm with scheduled invocations before sustained-load runs; provisioned concurrency as a fallback |
| DynamoDB on-demand scaling lag at 5K TPS | Throttled writes, idempotency check fails | Pre-warm the table with a short warmup run before the load test; switch to provisioned with auto-scaling if recurrent |
| CloudFront invalidation delay hides web changes | Latest build not served to the browser | Use versioned asset filenames; invalidate `/index.html` specifically |
| Customer changes score metric or reset cadence late | Rework during Phase 2 | Keep metric and reset logic isolated to one module to minimize blast radius |
| IAM policies too loose for a public repo | Security issue in a public project | Review all IAM before public push; never commit real account IDs; `.gitignore` tuned for CDK outputs (`cdk.out/`) and local environment files |

## Customer-Decision-Pending List

These items block final implementation of small but visible parts of the system. Defaults are in place so the project can move forward.

| Item | Current Default | Needed From Customer |
|---|---|---|
| Leaderboard reset cadence | Permanent accumulation | Daily / weekly / season / never — their product's real behavior |
| Score metric | Integer points, additive | Is it points? Time? Rank-tier? Is it additive or replace-max? |
| Peak TPS and burst shape | 5K sustained, 2× short bursts | Their observed peak, event-per-second at launch, typical spike pattern |
| Event schema | `{eventId, gameId, userId, scoreDelta, timestamp}` | Additional fields (region, device, matchId?) |
| DynamoDB retention | 90 days TTL | Compliance / analytics requirement |
