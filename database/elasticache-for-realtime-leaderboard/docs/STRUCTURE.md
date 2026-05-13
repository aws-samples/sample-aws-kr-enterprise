# Implementation Artifact Structure

Concrete file layout, command targets, and development environment for the real-time leaderboard. This document is the blueprint that every implementation task references — do not create files that contradict this layout without updating this document first.

---

## 1. CDK Infrastructure (`infra/`)

### 1.1 File layout

```
infra/
├── app.py                     CDK app entry point
├── cdk.json                   CDK CLI configuration
├── stacks/
│   ├── __init__.py            Re-exports all stack classes
│   ├── root_stack.py          LeaderboardApp — wires 6 child stacks
│   ├── network_stack.py       NetworkStack — VPC, subnets, SGs
│   ├── data_stack.py          DataStack — SQS, DLQ, DynamoDB, ElastiCache Valkey
│   ├── ingest_stack.py        IngestStack — Processor Lambda + ESM
│   ├── api_stack.py           ApiStack — Reader Lambda + API Gateway HTTP API
│   ├── loadgen_stack.py       LoadGenStack — Load generator + trigger + Step Functions
│   └── web_stack.py           WebStack — S3, CloudFront, OAC
└── config.py                  Shared constants (region, node type, table name, etc.)
```

### 1.2 Root stack wiring (`root_stack.py`)

`LeaderboardApp` is a single `cdk.App` with one root stack that instantiates six nested stacks in dependency order:

```python
class LeaderboardApp(Stack):
    def __init__(self, scope, id, **kwargs):
        super().__init__(scope, id, **kwargs)

        network = NetworkStack(self, "Network")
        data    = DataStack(self, "Data", vpc=network.vpc, sg=network.valkey_sg)
        ingest  = IngestStack(self, "Ingest",
                      queue=data.queue,
                      table=data.table,
                      valkey_endpoint=data.valkey_endpoint,
                      valkey_secret=data.valkey_secret,
                      vpc=network.vpc,
                      lambda_sg=network.lambda_sg)
        api     = ApiStack(self, "Api",
                      valkey_endpoint=data.valkey_endpoint,
                      valkey_secret=data.valkey_secret,
                      vpc=network.vpc,
                      lambda_sg=network.lambda_sg)
        loadgen = LoadGenStack(self, "LoadGen",
                      queue=data.queue)
        web     = WebStack(self, "Web",
                      api_url=api.api_url)
```

### 1.3 Cross-stack prop passing

Each child stack exposes its outputs as public properties (e.g., `self.vpc`, `self.queue`, `self.api_url`). Downstream stacks receive them as constructor arguments. No `CfnOutput` look-ups or SSM indirection — keep it explicit and type-checked.

### 1.4 Naming conventions

- Stack classes: `PascalCase` (e.g., `NetworkStack`)
- Stack construct IDs: short PascalCase (e.g., `"Network"`, `"Data"`)
- Resource logical IDs: descriptive PascalCase (e.g., `"ScoreEventsQueue"`, `"RawEventsTable"`)
- Physical names: kebab-case prefix `leaderboard-` (e.g., `leaderboard-score-events`, `leaderboard-raw-events`)

---

## 2. Application Code (`app/`)

### 2.1 File layout

```
app/
├── lambdas/
│   ├── processor/
│   │   ├── handler.py         SQS ESM → DDB + Valkey write
│   │   └── requirements.txt   Lambda-specific deps (valkey-glide, powertools)
│   ├── reader/
│   │   ├── handler.py         API GW → Valkey read
│   │   └── requirements.txt
│   ├── load_gen_trigger/
│   │   ├── handler.py         Validates body → starts Step Functions
│   │   └── requirements.txt
│   └── load_generator/
│       ├── handler.py         SendMessageBatch loop
│       └── requirements.txt
├── shared/
│   ├── __init__.py
│   ├── valkey_client.py       Connection factory (valkey-glide preferred, redis-py fallback)
│   └── ddb_helpers.py         Conditional PutItem, query helpers, rebuild aggregator
└── scripts/
    ├── test_p1_smoke.py           P1-001
    ├── test_p1_idempotency.py     P1-002
    ├── test_p1_write_latency.py   P1-003
    ├── test_p1_read_latency.py    P1-004
    ├── test_p1_dlq_clean.py       P1-005
    ├── test_p1_rebuild.py         P1-006
    ├── test_p1_scale_100k.py      P1-007
    ├── test_p2_sustained_5k.py    P2-001
    ├── test_p2_burst.py           P2-002
    ├── test_p2_ui_freshness.py    P2-003
    ├── test_p2_button_reproducibility.py  P2-004
    ├── test_p2_page_load.py       P2-005
    ├── test_p2_dashboard.py       P2-006
    ├── test_p2_scale_and_load.py  P2-007
    ├── test_p2_top_response.py    P2-008
    ├── rebuild_from_ddb.py        Valkey rebuild procedure (§ 8.3 in ARCHITECTURE.md)
    └── seed.py                    Quick-start: inject sample events for manual dev
```

### 2.2 Lambda packaging

Each Lambda directory is self-contained. CDK `PythonFunction` (or `BundlingOptions` with pip) installs the per-Lambda `requirements.txt`. The `app/shared/` directory is included in every Lambda bundle via a symlink or CDK's `code` path that packages both the handler directory and `shared/`.

### 2.3 Shared module patterns

**`valkey_client.py`** — singleton-per-cold-start connection:

```python
import os
_client = None

def get_valkey_client():
    global _client
    if _client is None:
        endpoint = os.environ["VALKEY_ENDPOINT"]
        secret = _get_secret(os.environ["VALKEY_SECRET_ARN"])
        _client = connect(endpoint, password=secret)
    return _client
```

**`ddb_helpers.py`** — thin wrappers around boto3 `PutItem` with condition, `Query` with pagination, and the rebuild aggregation loop.

---

## 3. Web Code (`web/`)

### 3.1 Framework decision

**Vanilla TypeScript + Vite.** Rationale: the demo UI is a single page with a polling table, a game selector dropdown, control buttons, and an embedded CloudWatch widget. No routing, no complex state, no component tree — React would be overhead without benefit.

### 3.2 File layout

```
web/
├── package.json
├── tsconfig.json
├── vite.config.ts
├── index.html                 Entry point (Vite SPA)
└── src/
    ├── main.ts                Bootstrap, polling loop, DOM binding
    ├── api.ts                 Fetch wrapper for /leaderboard and /demo/start-load
    ├── leaderboard.ts         Render top-N table + "me" row
    ├── controls.ts            Load generator buttons + status indicator
    ├── dashboard.ts           Embedded CloudWatch dashboard iframe/widget
    └── style.css              Minimal styling
```

### 3.3 Build and deploy

- `npm run build` → `dist/` (Vite produces hashed filenames for cache-busting)
- CDK `WebStack` uses `BucketDeployment` to sync `web/dist/` to S3
- CloudFront invalidation on `/index.html` only (asset filenames are content-hashed)

### 3.4 API endpoint injection

At build time, `VITE_API_URL` environment variable is baked into the bundle. CDK outputs the API Gateway URL; the Makefile pipes it to the web build. Alternatively, `index.html` reads `window.__CONFIG__.apiUrl` injected by a CloudFront function (simpler for iterative deploys).

---

## 4. Makefile Targets

```makefile
.PHONY: deploy seed demo-url phase1-exit phase2-exit destroy fmt lint

# --- Deploy ---
deploy:                        ## Full stack deploy (CDK)
	cd infra && cdk deploy --all --require-approval never

# --- Seed ---
seed:                          ## Inject 1,000 sample events across 3 games
	python app/scripts/seed.py

# --- Demo URL ---
demo-url:                      ## Print the CloudFront demo URL
	cd infra && cdk outputs -O /dev/stdout | python -c \
	  "import sys,json; print(json.load(sys.stdin)['LeaderboardApp/Web']['DemoUrl'])"

# --- Exit Criteria ---
phase1-exit:                   ## Run P1-001..P1-007 twice; fail on any non-zero exit
	@echo "=== Phase 1 Exit Gate — Run 1 ===" && \
	python app/scripts/test_p1_smoke.py && \
	python app/scripts/test_p1_idempotency.py && \
	python app/scripts/test_p1_write_latency.py && \
	python app/scripts/test_p1_read_latency.py && \
	python app/scripts/test_p1_dlq_clean.py && \
	python app/scripts/test_p1_rebuild.py && \
	python app/scripts/test_p1_scale_100k.py && \
	echo "=== Phase 1 Exit Gate — Run 2 ===" && \
	python app/scripts/test_p1_smoke.py && \
	python app/scripts/test_p1_idempotency.py && \
	python app/scripts/test_p1_write_latency.py && \
	python app/scripts/test_p1_read_latency.py && \
	python app/scripts/test_p1_dlq_clean.py && \
	python app/scripts/test_p1_rebuild.py && \
	python app/scripts/test_p1_scale_100k.py && \
	echo "PASS: Phase 1 exit gate passed (2 consecutive runs)"

phase2-exit:                   ## Run P2-001..P2-008 twice; fail on any non-zero exit
	@echo "=== Phase 2 Exit Gate — Run 1 ===" && \
	python app/scripts/test_p2_sustained_5k.py && \
	python app/scripts/test_p2_burst.py && \
	python app/scripts/test_p2_ui_freshness.py && \
	python app/scripts/test_p2_button_reproducibility.py && \
	python app/scripts/test_p2_page_load.py && \
	python app/scripts/test_p2_dashboard.py && \
	python app/scripts/test_p2_scale_and_load.py && \
	python app/scripts/test_p2_top_response.py && \
	echo "=== Phase 2 Exit Gate — Run 2 ===" && \
	python app/scripts/test_p2_sustained_5k.py && \
	python app/scripts/test_p2_burst.py && \
	python app/scripts/test_p2_ui_freshness.py && \
	python app/scripts/test_p2_button_reproducibility.py && \
	python app/scripts/test_p2_page_load.py && \
	python app/scripts/test_p2_dashboard.py && \
	python app/scripts/test_p2_scale_and_load.py && \
	python app/scripts/test_p2_top_response.py && \
	echo "PASS: Phase 2 exit gate passed (2 consecutive runs)"

# --- Teardown ---
destroy:                       ## Destroy all stacks (non-interactive)
	cd infra && cdk destroy --all --force

# --- Formatting ---
fmt:                           ## Format Python (black+isort) and TypeScript (prettier)
	black infra/ app/ && isort infra/ app/
	cd web && npx prettier --write src/

# --- Linting ---
lint:                          ## Lint Python (ruff) and TypeScript (eslint)
	ruff check infra/ app/
	cd web && npx eslint src/
```

---

## 5. Local Development Environment

### 5.1 Python

| File | Purpose |
|---|---|
| `.python-version` | `3.12` — pyenv / asdf reads this |
| `requirements.txt` | All Python deps (CDK + app + test) |
| `requirements-dev.txt` | Dev-only deps (black, isort, ruff, pytest) |
| `.env.example` | Environment variables needed for local script runs |

### 5.2 `requirements.txt`

```
aws-cdk-lib>=2.140.0
constructs>=10.0.0
aws-lambda-powertools[all]>=2.35.0
valkey-glide>=1.0.0
boto3>=1.34.0
```

### 5.3 `requirements-dev.txt`

```
-r requirements.txt
black>=24.0
isort>=5.13
ruff>=0.3.0
pytest>=8.0
pytest-timeout>=2.3
requests>=2.31
playwright>=1.42
```

### 5.4 `.env.example`

```bash
# AWS
AWS_PROFILE=default
AWS_REGION=us-east-1

# Stack outputs (populated after first deploy)
VALKEY_ENDPOINT=
VALKEY_SECRET_ARN=
SQS_QUEUE_URL=
DDB_TABLE_NAME=leaderboard-raw-events
API_URL=
CLOUDFRONT_URL=

# Load test parameters
LOAD_TEST_TPS=500
LOAD_TEST_DURATION_SEC=60
LOAD_TEST_GAMES=arena-shooter,puzzle-01,racing-mini
LOAD_TEST_USER_POOL=1000
```

### 5.5 Virtualenv conventions

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

The virtualenv lives at `.venv/` (already in `.gitignore`). All `make` targets assume the venv is active.

### 5.6 Node.js (web only)

```bash
cd web && npm install
```

Node 20+ required. `web/node_modules/` is gitignored.

---

## 6. Directory Tree Summary

```
real-time-leaderboard/
├── .python-version
├── .env.example
├── requirements.txt
├── requirements-dev.txt
├── Makefile
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── .gitignore
├── docs/
│   ├── ARCHITECTURE.md
│   ├── PLAN.md
│   ├── RUNBOOK.md
│   ├── STRUCTURE.md           ← this file
│   ├── HANDOFF.md
│   └── adr/
├── diagrams/
├── infra/
│   ├── app.py
│   ├── cdk.json
│   ├── config.py
│   └── stacks/
│       ├── __init__.py
│       ├── root_stack.py
│       ├── network_stack.py
│       ├── data_stack.py
│       ├── ingest_stack.py
│       ├── api_stack.py
│       ├── loadgen_stack.py
│       └── web_stack.py
├── app/
│   ├── lambdas/
│   │   ├── processor/
│   │   │   ├── handler.py
│   │   │   └── requirements.txt
│   │   ├── reader/
│   │   │   ├── handler.py
│   │   │   └── requirements.txt
│   │   ├── load_gen_trigger/
│   │   │   ├── handler.py
│   │   │   └── requirements.txt
│   │   └── load_generator/
│   │       ├── handler.py
│   │       └── requirements.txt
│   ├── shared/
│   │   ├── __init__.py
│   │   ├── valkey_client.py
│   │   └── ddb_helpers.py
│   └── scripts/
│       ├── seed.py
│       ├── rebuild_from_ddb.py
│       ├── test_p1_smoke.py
│       ├── test_p1_idempotency.py
│       ├── test_p1_write_latency.py
│       ├── test_p1_read_latency.py
│       ├── test_p1_dlq_clean.py
│       ├── test_p1_rebuild.py
│       ├── test_p1_scale_100k.py
│       ├── test_p2_sustained_5k.py
│       ├── test_p2_burst.py
│       ├── test_p2_ui_freshness.py
│       ├── test_p2_button_reproducibility.py
│       ├── test_p2_page_load.py
│       ├── test_p2_dashboard.py
│       ├── test_p2_scale_and_load.py
│       └── test_p2_top_response.py
└── web/
    ├── package.json
    ├── tsconfig.json
    ├── vite.config.ts
    ├── index.html
    └── src/
        ├── main.ts
        ├── api.ts
        ├── leaderboard.ts
        ├── controls.ts
        ├── dashboard.ts
        └── style.css
```
