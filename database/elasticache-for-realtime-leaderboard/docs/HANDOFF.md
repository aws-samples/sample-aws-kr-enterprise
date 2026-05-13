# Handoff Notes

Read this first if you are resuming work on this project in a fresh session. This document is the single entry point to pick up where planning stopped.

Last updated: 2026-05-07

---

## Current Status

**Phase 2 Milestone 1 complete. LoadGenStack, WebStack, Web SPA implemented. CDK synth passes. Deploy pending.**

- Phase 1 fully complete — all exit criteria (P1-001 through P1-007) passed.
- Phase 2 Day 6–9 code implemented and QA-verified.
- LoadGenStack: load-generator Lambda + Step Functions state machine (fan-out Map, max concurrency 25).
- WebStack: S3 (private) + CloudFront with OAC + BucketDeployment.
- ApiStack updated: `POST /demo/start-load` route with load-gen-trigger Lambda.
- Web SPA: vanilla TypeScript + Vite, leaderboard polling, game selector, load generator controls.
- CDK synth passes cleanly. TypeScript compiles without errors.
- Repository is on GitHub, `main` branch.
- **Remaining**: CDK deploy, CloudWatch dashboard (Day 10), Phase 2 exit criteria scripts (P2-001 ~ P2-008).

---

## What Exists

### Root
- `README.md` — public-facing overview, tech stack, repo structure, roadmap
- `LICENSE` (MIT)
- `CONTRIBUTING.md` — commit style, PR checklist
- `.gitignore`

### `docs/`
- `ARCHITECTURE.md` — 12-section system spec (overview, components, data flow, key schema, DDB, scaling, config, failure modes, security, cost, hardening checklist, references)
- `PLAN.md` — 2-phase development plan with strict numeric exit criteria (P1-001 through P1-007, P2-001 through P2-008), Acceptance Principles, Success Criteria, Risks, Customer-Decision-Pending list
- `RUNBOOK.md` — developer operations reference (deploy, rebuild Valkey, purge leaderboard, scale, DLQ, teardown)
- `STRUCTURE.md` — implementation artifact structure (Block D output): CDK layout, app layout, web layout, Makefile targets, local dev environment
- `HANDOFF.md` — this file

### `docs/adr/`
- `README.md` — ADR index
- ADR-001: SQS Standard + Lambda ESM for ingestion
- ADR-002: Sorted Set leaderboard on ElastiCache for Valkey
- ADR-003: DynamoDB raw events with conditional writes
- ADR-004: Lambda + Step Functions load generator (not browser)
- ADR-005: 1-second polling over WebSocket for the demo UI
- ADR-006: Single AWS account, `us-east-1`, no cross-account
- ADR-007: AWS CDK (Python) with nested stacks
- ADR-008: ElastiCache for Valkey over ElastiCache for Redis

### `diagrams/`
- `architecture-overview.drawio` + `.drawio.png` — full system, VPC boundary, 4 colored zones
- `sequence-write-path.drawio` + `.drawio.png` — Game Platform → SQS → Lambda → DDB + Valkey
- `sequence-read-path.drawio` + `.drawio.png` — UI → API GW → Lambda → Valkey
- `data-model.drawio` + `.drawio.png` — Valkey ZSET schema + DDB table

---

## Locked Decisions (do not revisit without reopening the corresponding ADR)

| # | Decision | Value | Source of truth |
|---|---|---|---|
| 1 | IaC | AWS CDK (Python), nested stacks | ADR-007 |
| 2 | Lambda runtime | Python 3.12 | ARCHITECTURE.md § 7 |
| 3 | Region | `us-east-1` | ADR-006 |
| 4 | Account / budget | Existing authorized AWS account, no hard cap | PLAN.md, README.md |
| 5 | Demo domain | Default CloudFront domain (no Route 53, no ACM) | ARCHITECTURE.md § 2 |
| 6 | Cache engine | ElastiCache for Valkey 8.0 (wire-compatible with Redis 7) | ADR-008 |
| 7 | Ingestion | SQS Standard + Lambda ESM (batch=10, window=1s) | ADR-001 |
| 8 | Durable store | DynamoDB on-demand, PK=`gameId`, SK=`ts#eventId`, TTL 90d | ADR-003, ARCHITECTURE.md § 5 |
| 9 | Leaderboard store | Valkey ZSET, key `lb:{gameId}`, member `userId`, score cumulative | ADR-002, ARCHITECTURE.md § 4 |
| 10 | Load generator | Lambda + Step Functions (server-side, not browser) | ADR-004 |
| 11 | UI refresh | 1 s HTTP polling | ADR-005 |

---

## Performance Targets (contract with every exit criterion)

- Sustained ingest: **5,000 TPS**
- End-to-end write latency: **p95 < 2 s**
- Read API latency: **p95 < 100 ms** (API GW), **p95 < 200 ms** (client RTT)
- Message loss: **0%**
- Data volume assumption: **~100,000 users per game**
- Games in scope for acceptance tests: `arena-shooter`, `puzzle-01`, `racing-mini` (3–5 games)
- Monthly cost (24/7): **~$135**; on-demand (~10 h/month): **~$10**

---

## Next Step (pick up here)

**Phase 1 verification in progress.** P1-001, P1-002, P1-004, P1-005 PASS. P1-003 (write latency) running. P1-007 (100K scale) pending.

After all Phase 1 exit criteria pass, proceed to **Phase 2 Day 6** — Web SPA skeleton + S3/CloudFront.

## Deployed Stack Outputs (us-east-1)

| Output | Value |
|--------|-------|
| Demo URL | `https://d1tuanzhhkc3z5.cloudfront.net` |
| API URL | `https://pijtf5xn90.execute-api.us-east-1.amazonaws.com` |
| SQS Queue URL | `https://sqs.us-east-1.amazonaws.com/684778767920/leaderboard-score-events` |
| DDB Table | `leaderboard-raw-events` |
| Valkey Endpoint | `master.lev1wick2bj4mhc9.nchdl1.use1.cache.amazonaws.com` |
| Valkey Secret ARN | `arn:aws:secretsmanager:us-east-1:684778767920:secret:leaderboard/valkey-auth-token-da8jsM` |

---

## Work Completed So Far (context for anyone reading this cold)

- Iterated on ingestion architecture through several options (API Gateway → SQS direct → cross-account EventBridge → single-account SQS) and landed on SQS Standard.
- Anonymized the customer in all docs (do not name any specific company).
- Renamed `Gaming Hub` references to neutral `Game Platform` everywhere.
- Swapped engine from Redis to Valkey (ADR-008) after confirming the same AWS account is used.
- Removed all presenter-facing content: deleted `docs/DEMO-SCRIPT.md`, cut Phase 3 (Rehearsal + Polish), stripped scene / presenter / audience language from every doc. **Team scope is development only** — customer walkthrough, timing, teardown scheduling are owned by the user and out of scope.
- Defined strict numeric exit criteria following 7 Acceptance Principles (numbers only, automated pass/fail, reproducible, specific failure output, no tolerance, two consecutive runs, no competing load). See PLAN.md.
- Completed Block D: wrote `docs/STRUCTURE.md` (CDK layout, app layout, web layout — vanilla TS + Vite chosen for the SPA, Makefile targets, local dev environment). Created stub directory tree with `.gitkeep` files. Added root config files (`Makefile`, `.python-version`, `requirements.txt`, `requirements-dev.txt`, `.env.example`).

---

## How to Resume Efficiently

1. Read this file.
2. Read `README.md` (orients you to the repo).
3. Read `docs/ARCHITECTURE.md` § 1 Overview and § 7 Configuration values (the hard numbers).
4. Read `docs/PLAN.md` Acceptance Principles + both Exit Criteria tables.
5. Skim `docs/adr/README.md` and open any ADR whose decision you need to challenge or extend.
6. Then tackle the **Next Step** above.

Do not re-litigate locked decisions without going through the ADR process. Do not expand scope into customer-facing demo content — it is out of scope by design.
