.PHONY: deploy seed demo-url phase1-exit phase2-exit destroy fmt lint

# --- Deploy ---
deploy: ## Full stack deploy (CDK)
	cd infra && cdk deploy --all --require-approval never

# --- Seed ---
seed: ## Inject 1,000 sample events across 3 games
	python app/scripts/seed.py

# --- Demo URL ---
demo-url: ## Print the CloudFront demo URL
	@cd infra && cdk outputs -O /dev/stdout | python -c \
	  "import sys,json; d=json.load(sys.stdin); print(d.get('LeaderboardApp/Web',{}).get('DemoUrl','(not yet deployed)'))"

# --- Exit Criteria ---
phase1-exit: ## Run P1-001..P1-007 twice; fail on any non-zero exit
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

phase2-exit: ## Run P2-001..P2-008 twice; fail on any non-zero exit
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
destroy: ## Destroy all stacks (non-interactive)
	cd infra && cdk destroy --all --force

# --- Formatting ---
fmt: ## Format Python (black+isort) and TypeScript (prettier)
	black infra/ app/
	isort infra/ app/
	cd web && npx prettier --write src/

# --- Linting ---
lint: ## Lint Python (ruff) and TypeScript (eslint)
	ruff check infra/ app/
	cd web && npx eslint src/
