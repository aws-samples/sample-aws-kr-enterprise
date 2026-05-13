# ADR-005: Poll the API every 1s instead of WebSocket push for the demo UI

- **Status:** Accepted
- **Date:** 2026-05-06
- **Deciders:** Solutions Architect, Leaderboard Demo

## Context

The demo UI needs to reflect leaderboard changes in "near real-time" — the brief specifies 1–2 s is acceptable. Two mechanisms were considered:

1. **Client polls** the read API every 1 s.
2. **WebSocket push** — API Gateway WebSocket API with Lambda fan-out on Valkey Pub/Sub or DynamoDB Streams.

Expected viewership is small (tens of concurrent viewers at most), and the primary metric is "does it look live?" — a 1 s refresh already satisfies that.

## Decision

Use **HTTP polling** from the demo UI:

- Fetch `GET /leaderboard?gameId=G1&limit=100&userId=U42` every **1 s** via `setInterval`.
- The request returns both the top-N slice and the current user's rank/score in a single JSON response (batched read — see read-path sequence).
- Stop polling on tab visibility change (`document.hidden`) to avoid waste.

Abandon polling (stop the interval) cleanly on navigation away.

## Consequences

**Positive**

- **Demo simplicity.** A `setInterval` + `fetch` is three lines of JavaScript. No WebSocket reconnection state machine, no dropped-message-on-reconnect edge case, no "why is my graph empty" stalls during the demo.
- Cacheable at CloudFront if needed (keeping 1 s TTL would cap Valkey reads at 1 rps regardless of viewer count, though for this demo we don't enable it — the freshness matters).
- API Gateway HTTP API costs $1.00/M requests. At 1 req/s for 20 minutes of demo with 20 viewers = 24,000 requests = $0.024. Trivial.
- Same API can be hit by `curl` during the demo walkthrough — no WebSocket client in the browser devtools.

**Negative**

- At 1 Hz, the UI can show up to 999 ms of staleness. Acceptable per the brief. A real-time video game HUD would not accept this.
- For a production 100K-viewer deployment, 100K req/s is a significant load on Valkey (though still within `cache.r7g.large` capacity of hundreds of thousands of ops/s). That's a production hardening concern, not a demo concern — see checklist in ARCHITECTURE.md.

## Alternatives Considered

| Option | Why not |
|---|---|
| **API Gateway WebSocket API + Valkey Pub/Sub** | Lower latency and lower steady-state request count at high viewer counts. But: (1) reconnection/backoff complexity on the client; (2) state machine for "which connections subscribe to which gameIds"; (3) operational risk — a connection drop is harder to recover from transparently than a polling retry. Not worth it at a viewer count under 50. |
| **DynamoDB Streams → Lambda → WebSocket fanout** | Same trade-off, plus a separate pipeline to maintain. |
| **AWS AppSync subscriptions (GraphQL)** | Elegant DX, but adds another service and a schema surface we don't otherwise need. |
| **Server-Sent Events (SSE)** | A middle ground (one-way push, plain HTTP), but API Gateway does not natively support long-lived SSE streams without workarounds (CloudFront buffers responses). Back to polling. |
| **Longer poll interval (5 s)** | Cheaper, but violates "it looks live" — the leaderboard wouldn't visibly move in sync with the load generator, which is the entire demo point. |

## Superseded when

- Viewer count exceeds ~100 concurrent viewers AND per-viewer freshness requirement is sub-second → revisit WebSocket push.
- The system is productionized for a consumer-facing leaderboard with 1M+ concurrent viewers → almost certainly switch to WebSocket or an edge-cached feed.

## References

- [API Gateway HTTP API vs WebSocket API](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-vs-rest.html)
- `/diagrams/sequence-read-path.drawio.png`
- `/docs/ARCHITECTURE.md` — "Production hardening checklist"
