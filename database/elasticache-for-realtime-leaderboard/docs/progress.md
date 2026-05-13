# 프로젝트 진행 상황

## 현재 상태

**마지막 업데이트**: 2026-05-07 15:00
**현재 단계**: Phase 2 — Demo Surface (Week 2)
**진행률**: 60% (Milestone 1 완료 / Phase 2 Day 6–9 완료, Day 10 남음)

## 완료된 작업

### 2026-05-07

- LoadGenStack 구현 완료 (load-generator Lambda + Step Functions state machine)
  - `leaderboard-load-generator` Lambda: Python 3.12, 256MB, 300s timeout, TPS 페이싱 루프, 재시도(3회) + 지수 백오프
  - `leaderboard-prepare-workers` Lambda: 인라인 코드, TPS를 200 TPS/worker 단위로 분할
  - `leaderboard-load-generator-sm` Step Functions: PrepareWorkers -> FanOutWorkers (Map, max concurrency 25)
- WebStack 구현 완료 (S3 + CloudFront OAC + BucketDeployment)
  - S3: all public access blocked, SSE AES256, RemovalPolicy.DESTROY
  - CloudFront: OAC (sigv4 signing), HTTPS redirect, error page fallback to `/index.html`
  - BucketDeployment: `web/dist/` 자동 배포 + CloudFront invalidation
- ApiStack 업데이트 — `POST /demo/start-load` 라우트 추가
  - `leaderboard-load-gen-trigger` Lambda: pattern 유효성 검증, Step Functions StartExecution
  - CORS에 POST 메서드 포함 확인
  - IAM: `states:StartExecution` 특정 state machine ARN만 허용
- Web SPA 구현 완료 (vanilla TypeScript + Vite)
  - `api.ts`: API 클라이언트 (fetchLeaderboard, startLoad), 타입 인터페이스 정의
  - `leaderboard.ts`: 테이블 렌더링, XSS 방어(escapeHtml), 빈 상태 처리
  - `controls.ts`: 부하 테스트 버튼(4 패턴), 실행 중 중복 방지, 상태 표시
  - `dashboard.ts`: CloudWatch 대시보드 링크 (Day 10 embed 예정)
  - `main.ts`: 1초 폴링 루프, 게임 선택 드롭다운 연동
  - Vite build: `dist/` 생성 (index.html 2.65KB, CSS 3.90KB, JS 2.99KB)
- CDK synth 통과 확인 (6개 nested stack 템플릿 정상 생성)
- TypeScript 컴파일 (`tsc --noEmit`) 에러 없음
- QA PASS: 25/25 테스트 항목 통과 (`docs/qa/2026-05-07-phase2-infra-app.md`)

### 2026-05-06

- Phase 1 exit criteria 검증 완료 (P1-001 ~ P1-006 PASS)
- P1-007 (100K 스케일 테스트) 완료
- Phase 1 전체 PASS 확정 후 Phase 2 진입

## 진행 중인 작업

- Phase 2 Milestone 1 완료 (Day 6–9 범위 구현 및 검증 완료)
- 아직 배포(deploy) 미실행 — CDK synth만 확인된 상태

## 다음 단계

1. **CDK deploy** — LoadGenStack + WebStack + ApiStack(업데이트) 배포
2. **Phase 2 Day 10** — CloudWatch 대시보드 생성 및 임베드
   - 위젯: SQS depth, Lambda Invocations, Lambda Errors, Valkey EngineCPUUtilization, end-to-end latency
   - `dashboard.ts`에 CloudWatch embed 또는 pre-signed URL 연동
3. **Phase 2 exit criteria 스크립트 작성 및 실행**
   - P2-001 ~ P2-008 자동화 검증 스크립트 구현
   - 2회 연속 PASS 확인

## 블로커 / 이슈

| 이슈 | 영향 | 해결 방안 | 상태 |
|------|------|----------|------|
| CloudWatch 대시보드 미구현 | P2-006 exit criterion 불충족 | Day 10에서 대시보드 스택 추가 | 진행 예정 |
| CDK deploy 미실행 | WebStack/LoadGenStack 실 동작 미확인 | 다음 세션에서 deploy 실행 | 진행 예정 |

## 의사결정 로그

### 2026-05-07: Web SPA 기술 선택 — vanilla TypeScript + Vite

**배경**: PLAN.md에서 "plain React or vanilla TS" 중 결정 필요
**선택지**: 1) React SPA 2) vanilla TypeScript + Vite
**결정**: vanilla TypeScript + Vite
**이유**: 데모 UI 범위가 단순(테이블 + 버튼 + 폴링)하여 프레임워크 오버헤드 불필요. 번들 크기 최소화(JS 2.99KB gzip 1.43KB). LCP/FCP 기준 충족에 유리.

### 2026-05-07: Load generator fan-out 전략 — Step Functions Map state

**배경**: 5,000 TPS를 단일 Lambda에서 달성 불가 (SQS batch 처리 한계)
**선택지**: 1) 단일 Lambda 다중 호출 2) Step Functions Map 기반 fan-out
**결정**: Step Functions Map state (max concurrency 25, worker당 200 TPS)
**이유**: ADR-004 결정 준수. 25 workers x 200 TPS = 5,000 TPS 달성. 각 worker는 독립적 타이밍 제어.

## 내일 이어서 할 일

> 이 섹션만 읽으면 바로 작업 시작 가능

1. **CDK deploy 실행** (LoadGenStack + WebStack + ApiStack 업데이트)
   - 파일: `infra/`
   - 할 일: `cd infra && cdk deploy --all` 실행 후 CloudFront URL 및 state machine ARN 확인
   - 예상 소요: 10–15분 (CloudFront distribution 생성이 가장 오래 걸림)

2. **CloudWatch 대시보드 구현** (Phase 2 Day 10)
   - 파일: 새 스택 또는 기존 스택에 대시보드 리소스 추가
   - 할 일: 5개 위젯 생성 (SQS depth, Lambda Invocations, Lambda Errors, Valkey EngineCPU, end-to-end latency)
   - P2-006 exit criterion 충족 필수 — 5분 내 1개 이상 datapoint 반환

3. **Web SPA에 CloudWatch embed 연동** (dashboard.ts 업데이트)
   - 파일: `web/src/dashboard.ts`
   - 할 일: CloudWatch embed URL 또는 대시보드 링크 완성

4. **Phase 2 exit criteria 스크립트** (P2-001 ~ P2-008)
   - 파일: `app/scripts/test_p2_*.py`
   - 할 일: 배포 후 자동화 검증 스크립트 작성 및 실행
   - P2-003은 Playwright 필요 — 설치 확인 필수

### 참고 컨텍스트

- API URL: `https://pijtf5xn90.execute-api.us-east-1.amazonaws.com`
- State Machine 이름: `leaderboard-load-generator-sm`
- 웹 빌드 명령: `cd web && npm run build` (tsc + vite build)
- CDK 작업 시 반드시 `.venv` 활성화 후 진행: `source .venv/bin/activate && cd infra`
- Phase 2 exit criteria 전체 목록: `docs/PLAN.md` 113–127행
- QA 보고서: `docs/qa/2026-05-07-phase2-infra-app.md`

## 아카이브

<!-- 14일 이상 지난 완료 작업은 여기로 이동 -->
