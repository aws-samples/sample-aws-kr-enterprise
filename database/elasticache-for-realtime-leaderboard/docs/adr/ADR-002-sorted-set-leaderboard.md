# ADR-002: Sorted Set leaderboard store on ElastiCache for Valkey

- **Status:** Accepted
- **Date:** 2026-05-06
- **Deciders:** Solutions Architect, Leaderboard Demo

## Context

The demo surfaces two read patterns for ~100K concurrent gamers:

1. **Top-N by game** — "give me the top 100 players for game X", polled every 1 s by the UI.
2. **Rank-of-user** — "what's user U42's rank and score in game X".

Target p95 end-to-end latency is < 2 s; the storage layer must return in single-digit milliseconds to leave budget for Lambda, API GW, and network. Writes happen continuously at up to ~5K TPS.

Accumulation is **permanent** for the current board; period-based resets (daily / weekly / season) are likely future requirements but out of scope for the initial demo.

Since mid-2024, AWS has promoted **Valkey** (an open-source, BSD-licensed fork of Redis 7) as the preferred engine on ElastiCache. Valkey 8.0 is wire-compatible with Redis 7 — same RESP protocol, same commands, same client libraries — at roughly 20% lower engine-hour pricing.

## Decision

Use **ElastiCache for Valkey 8.0** with a **Sorted Set (ZSET)** as the primary read path:

- **Key:** `lb:{gameId}` — one ZSET per game.
- **Member:** `userId` (string).
- **Score:** cumulative score (float).
- **Write op:** `ZINCRBY lb:{gameId} <delta> <userId>` — O(log N).
- **Read ops:** `ZREVRANGE lb:{gameId} 0 <limit-1> WITHSCORES`, `ZREVRANK lb:{gameId} <userId>`, `ZSCORE lb:{gameId} <userId>` — all O(log N + M) or better.

Cluster: `cache.r7g.large` with 1 primary + 1 replica in Multi-AZ. Valkey 8.0. In private subnets; no public endpoint.

**Client library:** `valkey-glide` (AWS-official Python client) is the preferred choice. `redis-py` is an acceptable fallback — Valkey 8.0 speaks the same wire protocol as Redis 7, so any Redis 7 client works without code changes.

Extensibility key pattern: `lb:{gameId}:{period}` (e.g. `lb:arena-shooter:2026-W19`) — same operations, with an `EXPIRE` set at rollover.

The choice of Valkey over Redis is documented separately in [ADR-008](./ADR-008-valkey-over-redis.md); this ADR covers the sorted-set data-structure decision.

## Consequences

**Positive**

- `ZREVRANGE 0 99` and `ZREVRANK` return in ~1–3 ms from a same-VPC Lambda — comfortably under the read-latency budget.
- `ZINCRBY` is atomic; no read-modify-write race between concurrent events for the same user.
- Memory footprint at demo scale is trivial: ~100K users × ~64 B/member ≈ 6.4 MB per game — `cache.r7g.large` (13 GB) holds thousands of boards.
- Pattern is the canonical leaderboard use case; documented by Redis, AWS, and the Valkey project. Wire compatibility means any Redis leaderboard tutorial or benchmark applies directly.
- ~20% engine-hour saving vs Redis on the same node type (see ADR-008).

**Negative**

- Valkey is in-memory: **a node loss without a replica = data loss**. Mitigated by the 1-primary-1-replica topology and by being able to rebuild from DynamoDB raw events (ADR-003, § "Rebuild procedure" in ARCHITECTURE.md).
- Operational burden: failover handling, AUTH rotation, patch windows. Managed by ElastiCache but not zero.
- Single ZSET per game does not shard. If any single game exceeds ~1M concurrent writers or ~50 MB key size, we'd need to add cluster mode (planned in the Production hardening checklist, not today).
- Engineers familiar with Redis must mentally map "Valkey" ↔ "Redis-compatible"; some third-party tooling still references "Redis" even when talking to a Valkey endpoint.

## Alternatives Considered

| Option | Why not |
|---|---|
| **DynamoDB only (with GSI sorted by score)** | GSI on score requires either a single hot partition (`gameId` as PK, score as SK) or careful write-sharding. Top-N reads cost RCUs proportional to page size, and rank-of-user queries require a Scan+count or a maintained rank counter. Works at low scale, painful at 5K TPS with 100K users. Latency is also higher than an in-memory sorted set. |
| **Valkey on ElastiCache without a replica (single node)** | Cheaper (~$48/mo vs ~$96/mo on Valkey) but any node event is instant data loss until the DDB replay finishes. Unacceptable during a live demo. |
| **MemoryDB for Redis / MemoryDB for Valkey** | Durable, Multi-AZ, strong consistency. Nice properties, but 2–3× the cost of ElastiCache and we already have DDB as the durable source of truth — paying for a second durable store is redundant. |
| **ElastiCache for Redis (OSS)** | Fully viable. Rejected as the default because it costs ~20% more per engine-hour than Valkey, the Redis license situation post-7.4 is non-permissive, and AWS is steering new demos toward Valkey. Decision rationale in full is in [ADR-008](./ADR-008-valkey-over-redis.md). |
| **Amazon RDS / Aurora with window functions** | Row-lock contention under 5K TPS writes to a few game rows would crater performance. Not the right tool. |

## References

- [Valkey project](https://valkey.io/)
- [Amazon ElastiCache for Valkey](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/WhatIs.html)
- [Redis Sorted Set commands (wire-compatible with Valkey 8.0)](https://redis.io/docs/latest/commands/?group=sorted-set)
- [`valkey-glide` Python client](https://github.com/valkey-io/valkey-glide)
- [ADR-008: Valkey over Redis](./ADR-008-valkey-over-redis.md)
- `/docs/ARCHITECTURE.md` — Valkey key schema, memory sizing, scaling math
- `/diagrams/data-model.drawio.png` — key layout + examples
