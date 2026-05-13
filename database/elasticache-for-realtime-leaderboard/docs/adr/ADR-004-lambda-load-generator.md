# ADR-004: Generate demo load with Lambda + Step Functions (not from the browser)

- **Status:** Accepted
- **Date:** 2026-05-06
- **Deciders:** Solutions Architect, Leaderboard Demo

## Context

The demo must be convincing: pressing a "Start 5K TPS" button should move the leaderboard visibly within 1–2 s and sustain that throughput for the duration of the demo segment (tens of seconds to a few minutes).

There are two obvious places to run the load generator:

1. **Client-side (browser JS)** — fire N HTTP requests/second from the demo UI.
2. **Server-side (Lambda orchestrated by Step Functions)** — the browser fires one "start" API call, and a cloud-side pipeline sends synthetic events into SQS.

The production Game Platform is a server-to-server producer into our pipeline. The demo should simulate the **actual production shape**, not a browser traffic pattern.

## Decision

The demo UI exposes a "Start Load Generator" button. Clicking it calls `POST /demo/start-load` on API Gateway, which invokes a trigger Lambda (`load-gen-trigger`) that starts a **Step Functions Standard Workflow** (`load-generator-sm`).

The state machine uses a **Map state** with parallel branches; each branch invokes the `load-generator` Lambda. Each Lambda instance uses the SQS `SendMessageBatch` API to push up to 10 synthetic events per call in a tight loop for its assigned duration. The state machine parameters (TPS, duration, game IDs, user pool size) are passed in from the trigger.

Events are written to the **same `score-events-queue`** as production events — no separate code path, no sampling.

## Consequences

**Positive**

- The producer is server-to-server into SQS, which is the real production shape. The demo exercises the actual ingestion path.
- Step Functions gives observable load runs: you can see in the console which branches ran, how long each took, and where failures happened.
- Lambda concurrency makes 5K TPS trivial: one Lambda sending `SendMessageBatch` every ~50 ms from 25 concurrent instances hits the target. No browser CORS, no client network caps, no flaky Wi-Fi during the demo.
- Clean shutdown and restart — no "refresh the page to stop" problem.
- Stops costing money the moment the state machine terminates.

**Negative**

- More moving parts than a browser loop: Lambda, Step Functions, IAM. Worth it for the realism.
- Step Functions Standard is billed per state transition (~$0.025 / 1000). At the demo's scale (hundreds of transitions per run) the cost is under $0.01/run — negligible but non-zero.
- Requires the operator to be comfortable running simulation load through the same queue as production. Mitigation: synthetic events are tagged (`sourceGame="loadgen"`) and the demo environment is isolated per the brief.

## Alternatives Considered

| Option | Why not |
|---|---|
| **Browser JS in the demo UI** | Hits the client's egress bandwidth, not AWS's. CORS, throttling, and flaky connectivity make the demo brittle. Doesn't simulate the production producer. |
| **EC2 instance with a long-running load tool (k6, Artillery)** | Works, but now there's an EC2 instance to patch, secure, and remember to stop. Step Functions + Lambda is serverless and zero-idle-cost. |
| **AWS Fargate task** | Similar issues to EC2, slightly better (no patching). Overkill for 5K TPS; Lambda handles this easily and the start-up latency for Fargate tasks (seconds) is a worse demo experience than Lambda cold starts (tens of ms at this concurrency). |
| **Pre-populated static leaderboard** | Fastest to set up, but defeats the purpose of a "real-time" leaderboard demo. |
| **Lambda invoked directly from API Gateway, no Step Functions** | Works for short runs, but a single Lambda is capped at 15 min and you lose the observability / parallel-fan-out that Step Functions gives you for free. |

## References

- [Step Functions Map state](https://docs.aws.amazon.com/step-functions/latest/dg/amazon-states-language-map-state.html)
- [SQS SendMessageBatch](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_SendMessageBatch.html)
- `/diagrams/architecture-overview.drawio.png` — demo control path (right side)
