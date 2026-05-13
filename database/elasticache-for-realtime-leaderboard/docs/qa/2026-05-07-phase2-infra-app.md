# QA Report: Phase 2 Infrastructure and App Code

**Date**: 2026-05-07 14:30
**Milestone**: Phase 2 Infrastructure and App Code (Early Stage QA)
**Status**: PASS
**Project Type**: Full-stack (API + Frontend)

## Evaluation Scores

| Axis | Score | Threshold | Verdict |
|------|-------|-----------|---------|
| Functionality (기능 완성도) | 4/5 | >= 4 | PASS |
| Spec Fidelity (스펙 충실도) | 5/5 | >= 4 | PASS |
| User Experience (사용자 경험) | 4/5 | >= 4 | PASS |
| Edge Cases (경계 조건) | 4/5 | >= 3 | PASS |
| Design Quality (디자인 품질) | N/A | N/A | N/A (Early stage, no deployed UI) |

## Summary

| Category | Tests | Passed | Failed |
|----------|-------|--------|--------|
| Spec Fidelity | 10 | 10 | 0 |
| Code Review | 8 | 8 | 0 |
| Infrastructure Verification | 7 | 7 | 0 |
| **Total** | **25** | **25** | **0** |

## Spec Fidelity Checklist (Acceptance Criteria)

| # | Requirement | Implemented | Verified | Notes |
|---|-------------|:-----------:|:--------:|-------|
| 1 | CDK synth passes without errors | PASS | PASS | `cdk synth --quiet` exits 0; all 4 Lambda assets bundled. Only warnings: node version and pip attrs version (non-blocking). |
| 2 | LoadGenStack creates load-generator Lambda | PASS | PASS | `leaderboard-load-generator` in CF template (AWS::Lambda::Function), Python 3.12, 256MB, 300s timeout. |
| 3 | LoadGenStack creates prepare-workers Lambda | PASS | PASS | `leaderboard-prepare-workers` inline Lambda, 128MB, 10s timeout. |
| 4 | LoadGenStack creates Step Functions state machine | PASS | PASS | `leaderboard-load-generator-sm` (AWS::StepFunctions::StateMachine). Definition: PrepareWorkers -> FanOutWorkers (Map, max concurrency 25). |
| 5 | WebStack creates S3 bucket with all public access blocked + CloudFront with OAC | PASS | PASS | S3: BlockPublicAcls=true, BlockPublicPolicy=true, IgnorePublicAcls=true, RestrictPublicBuckets=true. CloudFront: OAC resource (AWS::CloudFront::OriginAccessControl), SigningBehavior=always, SigningProtocol=sigv4. |
| 6 | ApiStack has POST /demo/start-load route wired to load-gen-trigger Lambda | PASS | PASS | Route: `POST /demo/start-load` -> `LoadGenTriggerFunction` via AWS_PROXY integration. |
| 7 | load-generator Lambda handles TPS pacing with error handling and retry | PASS | PASS | Per-second loop with `time.sleep()` pacing, `_send_batch()` with MAX_RETRIES=3, exponential backoff (0.1s * attempt). |
| 8 | load-gen-trigger Lambda validates input and starts Step Functions execution | PASS | PASS | Validates: pattern (enum), game_ids (list + regex), user_pool_size (int 1-1M). Handles `ExecutionAlreadyExists`. Returns structured JSON. |
| 9 | CORS includes POST method | PASS | PASS | CF template: `AllowMethods: ["GET", "POST", "DELETE"]` on the HttpApi. |
| 10 | IAM is least-privilege (no * in Actions/Resources) | PASS | PASS | Only wildcard: `cloudfront:CreateInvalidation/GetInvalidation` on `Resource: "*"` -- CDK BucketDeployment construct limitation (CF invalidation API doesn't support resource-level ARNs). All custom policies use specific ARNs. |
| 11 | No Lambda Function URLs used | PASS | PASS | Zero `AWS::Lambda::Url` resources across all templates (grep confirmed). |
| 12 | Web SPA builds with TypeScript (`tsc --noEmit` passes) | PASS | PASS | Exit code 0, no output (no errors). |
| 13 | Web SPA Vite build produces dist/ | PASS | PASS | `dist/index.html` (2.65 KB), `dist/assets/index-CJ7PkMVI.css` (3.90 KB), `dist/assets/index-BKNsmjco.js` (2.99 KB). Built in 44ms. |

## Detailed Test Results

### Infrastructure Verification

#### Test 1: CDK Synth
```bash
cd infra && source ../.venv/bin/activate && cdk synth --quiet
```
**Result**: PASS -- synth completes, all 6 nested stack templates generated in `cdk.out/`. Bundled all 4 Lambda assets.

#### Test 2: LoadGenStack Resources
**Evidence**: `LeaderboardAppLoadGenD65C849B.nested.template.json` contains:
- `LoadGeneratorFunction3904145F` (AWS::Lambda::Function): `leaderboard-load-generator`, python3.12, 256MB, 300s
- `PrepareWorkersFunctionCAD541BC` (AWS::Lambda::Function): `leaderboard-prepare-workers`, python3.12, 128MB, 10s
- `LoadGeneratorSME26DFC0A` (AWS::StepFunctions::StateMachine): `leaderboard-load-generator-sm`, 600s timeout
- State machine definition: PrepareWorkers -> FanOutWorkers (Map, MaxConcurrency=25)

**Result**: PASS

#### Test 3: WebStack Resources
**Evidence**: `LeaderboardAppWeb74DB66E7.nested.template.json` contains:
- S3 Bucket: `PublicAccessBlockConfiguration` all 4 flags = true, SSE AES256
- CloudFront Distribution: OAC configured (SigningBehavior=always, SigningProtocol=sigv4)
- BucketDeployment: deploys web assets, invalidates `/index.html`
- Bucket policy: only allows `s3:GetObject` from CloudFront via condition `AWS:SourceArn`

**Result**: PASS

#### Test 4: ApiStack POST /demo/start-load Route
**Evidence**: `LeaderboardAppApiBA242B94.nested.template.json`:
- Route: `POST /demo/start-load`, target = TriggerIntegration (AWS_PROXY to LoadGenTriggerFunction)
- Lambda: `leaderboard-load-gen-trigger`, python3.12, 128MB, 30s timeout
- Env var: `STATE_MACHINE_ARN` correctly referenced from LoadGen stack output

**Result**: PASS

#### Test 5: IAM Least Privilege Audit
**Evidence from all templates**:
- LoadGenerator role: `sqs:GetQueueAttributes`, `sqs:GetQueueUrl`, `sqs:SendMessage` on specific queue ARN only.
- PrepareWorkers role: only `AWSLambdaBasicExecutionRole` (no custom policy needed).
- StepFunctions role: `lambda:InvokeFunction` on 2 specific Lambda ARNs (+ `:*` for versioned aliases -- standard CDK pattern).
- LoadGenTrigger role: `states:StartExecution` on specific state machine ARN only.
- Reader role: `secretsmanager:DescribeSecret`, `secretsmanager:GetSecretValue` on specific secret ARN only.
- The only `Resource: "*"` is `cloudfront:CreateInvalidation/GetInvalidation` in CDK's BucketDeployment (unavoidable -- CloudFront API limitation).

**Result**: PASS -- no custom wildcard actions or resources.

#### Test 6: Lambda Function URLs
```bash
grep -c "AWS::Lambda::Url" infra/cdk.out/*.json
```
**Result**: PASS -- all templates return 0.

#### Test 7: CORS Configuration
**Evidence**: ApiGatewayV2 `CorsConfiguration`:
```json
{
  "AllowHeaders": ["Content-Type"],
  "AllowMethods": ["GET", "POST", "DELETE"],
  "AllowOrigins": ["*"]
}
```
**Result**: PASS -- POST method included.

### Code Review

#### Test 8: load-generator Lambda TPS Pacing
**File**: `app/lambdas/load_generator/handler.py`
- Per-second loop calculates `batches_per_second = tps / BATCH_SIZE`
- Sends integer batches + handles fractional remainder
- Sleeps for `1.0 - elapsed` to maintain pacing
- Returns actual TPS in response for observability

**Result**: PASS

#### Test 9: load-generator Lambda Error Handling and Retry
**File**: `app/lambdas/load_generator/handler.py`
- `_send_batch()`: try/except with `MAX_RETRIES=3`, exponential backoff (`0.1 * (attempt + 1)`)
- Handles partial batch failures (SQS `Failed` list)
- Logs warning on retry, error on final failure
- Returns `total_failed` count

**Result**: PASS

#### Test 10: load-generator Lambda Input Validation
- Validates `tps > 0`, `duration_sec > 0`, `game_ids` non-empty
- Returns 400 with descriptive error message on invalid input

**Result**: PASS

#### Test 11: load-gen-trigger Lambda Validation
**File**: `app/lambdas/load_gen_trigger/handler.py`
- Validates `pattern` against known enum (4 patterns)
- Validates `game_ids`: list, non-empty, regex `^[a-zA-Z0-9_-]{1,64}$`
- Validates `user_pool_size`: int, 1-1,000,000
- Structured error responses with specific field messages

**Result**: PASS

#### Test 12: load-gen-trigger Step Functions Start
- Starts execution with name = `{pattern}-{timestamp}`
- Handles `ExecutionAlreadyExists` by appending request ID suffix
- Catches all exceptions, returns 500 with type name (no stack trace)
- Returns executionArn, pattern, and config in success response

**Result**: PASS

#### Test 13: TypeScript Compilation
```bash
cd web && npx tsc --noEmit
```
**Result**: PASS -- exit 0, zero errors. Strict mode enabled (strict: true, noUnusedLocals, noUnusedParameters).

#### Test 14: Vite Build
```bash
cd web && npx vite build
```
**Output**:
```
dist/index.html                 2.65 kB | gzip: 0.97 kB
dist/assets/index-CJ7PkMVI.css  3.90 kB | gzip: 1.32 kB
dist/assets/index-BKNsmjco.js   2.99 kB | gzip: 1.43 kB
built in 44ms
```
**Result**: PASS -- dist/ produced with HTML, CSS, JS.

#### Test 15: Web SPA Architecture
- Vanilla TypeScript + Vite (no framework bloat -- matches spec "plain React or vanilla TS")
- Modules: `api.ts` (API client), `controls.ts` (load generator buttons), `leaderboard.ts` (table rendering), `dashboard.ts` (placeholder for CW embed), `main.ts` (bootstrap + polling)
- XSS-safe: `escapeHtml()` in leaderboard rendering
- Typed interfaces for all API responses
- 1-second polling interval for leaderboard updates

**Result**: PASS

## User Experience Evaluation

### API Design Quality
- POST /demo/start-load: clear endpoint name, POST semantics correct for triggering action
- Error responses: structured JSON with specific `error` field, actionable messages (e.g., "Invalid pattern: X. Valid patterns: [...]")
- Field naming: consistent snake_case across load-gen Lambda interfaces
- CORS configured for cross-origin web SPA access
- No stack traces or internal details leaked in error responses

### Web SPA DX
- TypeScript types defined for API interactions (`LeaderboardEntry`, `StartLoadResponse`, etc.)
- Build script: `tsc && vite build` -- type-safe builds
- Vite config: compile-time `__API_URL__` injection via `define`

**Score**: 4/5 -- Clean API design, helpful error messages, consistent patterns.

## Edge Cases Evaluation

### load-generator Lambda
- Handles `tps=0` and `duration_sec=0` gracefully (returns 400)
- Handles empty `game_ids` (returns 400)
- Handles `send_message_batch` failures with retry + logging
- Handles partial SQS batch failures (counts failed messages)

### load-gen-trigger Lambda
- Handles missing body (returns 400 "Request body is required")
- Handles invalid JSON (returns 400 "Invalid JSON in request body")
- Handles missing pattern field (returns 400 "pattern field is required")
- Handles invalid game_id characters (regex validation)
- Handles `user_pool_size` boundary: rejects 0 and > 1,000,000
- Handles `ExecutionAlreadyExists` race condition

### Web SPA
- `escapeHtml()` prevents XSS in user-rendered data
- Debounces polling status indicator
- Disables buttons while load test is running (prevents double-start)
- Empty state handled: "No data yet. Start a load test!"

**Score**: 4/5 -- Good coverage of input validation and error paths.

## IAM Findings Detail

The one `Resource: "*"` in the WebStack is for `cloudfront:CreateInvalidation` and `cloudfront:GetInvalidation`. This is a well-known AWS API limitation -- CloudFront invalidation does not support resource-level permissions. The CDK BucketDeployment construct generates this automatically. This is NOT a custom code issue and is acceptable per AWS best practices documentation.

**Severity**: N/A (accepted CDK behavior, not a custom IAM policy)

## Conclusion

All 10 acceptance criteria verified and passing. The implementation is faithful to the Phase 2 spec requirements for LoadGenStack, WebStack, API update, and Web SPA. Code quality is high -- proper error handling, input validation, TPS pacing logic, and least-privilege IAM throughout.

**Findings**: 0 CRITICAL, 0 HIGH, 0 MEDIUM, 0 LOW

**RECOMMENDATION**: PROCEED
