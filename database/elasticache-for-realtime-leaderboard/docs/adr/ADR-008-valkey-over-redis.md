# ADR-008: Use ElastiCache for Valkey over ElastiCache for Redis

- **Status:** Accepted
- **Date:** 2026-05-06
- **Deciders:** Solutions Architect, Leaderboard Demo

## Context

The leaderboard needs an in-memory sorted-set store for `ZINCRBY` / `ZREVRANGE` / `ZREVRANK` on a per-game basis (see [ADR-002](./ADR-002-sorted-set-leaderboard.md) for the data-structure decision). Historically the reflexive choice on AWS would be **ElastiCache for Redis**.

As of 2024+, the landscape shifted:

- The Redis project relicensed away from BSD for versions 7.4+ onto a dual-source-available license (RSAL / SSPL). This creates friction for cloud providers offering a managed Redis service indefinitely.
- AWS forked Redis 7 to create **Valkey**, a BSD-licensed open-source project governed under the Linux Foundation. Valkey 8.0 is **wire-compatible** with Redis 7 — same RESP protocol, same commands, same client libraries — with additional performance and operational improvements.
- AWS introduced **Amazon ElastiCache for Valkey** at a lower price point than ElastiCache for Redis on the same node type (~20% savings on engine-hour charges).

For a new demo being built in 2026, the question is: start on Redis (familiar) or Valkey (AWS-preferred, cheaper, permissively licensed)?

## Decision

Use **Amazon ElastiCache for Valkey 8.0** as the in-memory leaderboard store.

- **Engine:** Valkey 8.0.
- **Node type:** `cache.r7g.large` (unchanged — Valkey uses the same ElastiCache node types as Redis).
- **Topology:** 1 primary + 1 replica, Multi-AZ with automatic failover.
- **Wire protocol:** Identical to Redis 7. All commands used by this system (`ZINCRBY`, `ZREVRANGE`, `ZREVRANK`, `ZSCORE`, `ZADD`, `ZCARD`, `EXPIRE`, `DEL`) behave identically.
- **Client library:** `valkey-glide` (AWS-official Python client) is preferred. `redis-py` is an acceptable fallback — it works unchanged against a Valkey endpoint because of wire compatibility.

This applies to the demo default and to any forked-for-production starting point.

## Consequences

**Positive**

- **~20% cheaper.** On `cache.r7g.large` × 2 nodes, Valkey saves roughly $24/mo vs Redis on ElastiCache (engine-hour pricing). For this demo: ~$96/mo Valkey vs ~$120/mo Redis.
- **AWS-aligned strategic direction.** New demos and reference architectures on AWS default to Valkey in 2026; staying on Redis would mean swimming upstream.
- **Open-source license (BSD).** No license anxiety for long-term use, forking, or redistribution.
- **Zero code changes to migrate client libraries later.** If we switch from `valkey-glide` back to `redis-py` (or vice versa), commands and connection semantics are identical.
- **Redis tutorials, benchmarks, and community knowledge still apply.** Anything that runs on Redis 7 runs on Valkey 8.0.

**Negative**

- **Newer managed offering.** Valkey has been available on ElastiCache for less time than Redis, so tooling integrations and third-party dashboards may still label connections as "Redis".
- **Engineers must mentally map "Valkey" ↔ "Redis-compatible".** First-time contributors to the repo may wonder why we're using `ZINCRBY` on something not called Redis. Documentation addresses this prominently.
- **Slightly less StackOverflow history** specifically tagged "Valkey". Compensated by the fact that every Redis 7 answer applies directly.
- **Some AWS documentation still references Redis by default** in older pages. Mitigation: use [Amazon ElastiCache for Valkey docs](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/WhatIs.html) as primary.

## Alternatives Considered

| Option | Why not |
|---|---|
| **ElastiCache for Redis OSS** | Mature and well-understood. Rejected as the default because of (a) ~20% higher engine-hour cost, (b) license friction for Redis versions 7.4+, and (c) AWS is steering new demos toward Valkey. Remains a viable fallback if an operator is required to use Redis for external reasons — the architecture and code port trivially. |
| **Amazon MemoryDB (Redis or Valkey flavor)** | Durable, Multi-AZ with strong consistency and a transaction log. Rejected because it costs 2–3× ElastiCache, and we already have DynamoDB as the durable source of truth — paying for a second durable store is redundant at this scale. |
| **Self-managed Redis/Valkey on EC2** | Cheapest in raw compute, but operational burden (patching, backups, failover tooling, monitoring) is entirely on us. Rejected immediately for a demo. |
| **ElastiCache Serverless (Valkey)** | Scales to zero between runs, so it would save money at the idle ceiling. Rejected for the *initial* build only because we want predictable node-level performance during load tests; noted as a cost-optimization swap for later. |

## References

- [Valkey project](https://valkey.io/)
- [Amazon ElastiCache for Valkey](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/WhatIs.html)
- [`valkey-glide` Python client](https://github.com/valkey-io/valkey-glide)
- [ADR-002: Sorted Set leaderboard store on ElastiCache for Valkey](./ADR-002-sorted-set-leaderboard.md)
- `/docs/ARCHITECTURE.md` — Valkey configuration, cost estimate, scaling math
