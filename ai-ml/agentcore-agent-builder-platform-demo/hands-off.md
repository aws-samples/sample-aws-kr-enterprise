# Session Handoff — AgentCore Agent Builder Platform Demo

## 현재 작업 상태

**프로젝트 목적**: 소스 프로젝트(`/Users/ymjoung/workspace/claude-code-project/26y-aiops-platform-v2`)를 aws-samples GitHub repo에 **Reference Architecture**로 public 공유하기 위한 정제 작업.

## 결정된 사항 (Design Brainstorming 완료)

### 1. 공유 전략: Option A (Clean Copy)
- 소스에서 필요한 파일만 선별 복사 + 정제
- 불필요한 내부 개발 산출물 완전 제외

### 2. 디렉토리 구조

```
agentcore-agent-builder-platform-demo/
├── README.md                 # 이중언어 (EN/KR)
├── LICENSE                   # MIT-0
├── .gitignore
├── docs/
│   ├── images/               # 아키텍처 다이어그램 등 이미지
│   ├── architecture.md
│   ├── deployment-guide.md
│   └── demo-scenario.md      # YouTube 링크 포함
├── code/
│   ├── control-plane/
│   │   ├── api/              # FastAPI (Platform API)
│   │   └── ui/               # Next.js 14 (Platform UI)
│   ├── agent-runtime/        # Strands SDK Base Image
│   └── tools/                # Pre-built MCP Lambda (샘플 3개)
├── iac/
│   ├── README.md             # 모듈 구조 + dependency + 사용법
│   ├── modules/              # Terraform 모듈화 (7개)
│   │   ├── network/
│   │   ├── data/
│   │   ├── auth/
│   │   ├── compute/
│   │   ├── cdn/
│   │   ├── registry/
│   │   └── iam/
│   └── envs/dev/             # Root module (모듈 조합)
└── scripts/
    ├── deploy-all.sh         # Scratch → Full deploy (one-shot)
    ├── deploy-agents.sh
    ├── deploy-gateways.sh
    ├── seed-dynamodb.sh
    └── register-gateway-targets.py
```

### 3. 언어
- README: 이중언어 (EN/KR 병기)
- 코드 주석: 영어

### 4. 민감정보 처리
- **코드 내**: 모든 설정값은 **환경변수** 참조 (placeholder 금지)
- **필수 환경변수**: ACCOUNT_ID, AWS_REGION, DOMAIN_NAME, CLOUDFRONT_SECRET, PROJECT_PREFIX, COGNITO_DOMAIN, DYNAMODB_TABLE, INCIDENTS_TABLE, REPORT_BUCKET, ECR_PREFIX, AGENTCORE_ROLE_ARN
- **README에 강조**: 사용자 입력 필수 항목 (CLOUDFRONT_SECRET, DOMAIN_NAME 등)

### 5. MCP Lambda Tools
- 전체 130개 중 **샘플 3개만** 포함: `aws_cloudwatch_mcp.py`, `aws_eks_mcp.py`, `cross_account.py`
- Gateway 연동 패턴을 README로 설명

### 6. 데모 영상
- .mov 파일은 .gitignore
- YouTube 업로드 후 링크 삽입

### 7. AgentCore API
- 현재 Preview API 그대로 유지
- GA 시점에 업데이트 버전 별도 생성 예정
- Agent Registry, Harness 등 GA 기능 추가 필요

### 8. Terraform
- 모듈화 필요 (현재 단일 폴더 → 7개 모듈)
- `deploy-all.sh`에서 scratch 배포 자동화
- **⚠️ 미완료**: Terraform MCP 연동 후 모듈 설계 재진행 필요

### 9. deploy-all.sh
- Phase별 구분 (Infra → Images → Seed → ECS → AgentCore → Gateway)
- 환경변수 미설정 시 early-exit + 안내
- Terraform output 자동 추출 → 후속 단계 전달

## 제외 대상 (복사하지 않음)
- `parking_lot/`, `.claude/`, `.parallel-dev/`, `.superpowers/`
- `CLAUDE.md`, `hands-off.md`
- `docs/superpowers/` (specs, plans)
- `docs/feedback/`
- `assets/*.mov`
- `terraform.tfstate*`, `terraform.tfvars`
- `.playwright-mcp/`

## Change Log

| Timestamp | 변경사항 |
|-----------|----------|
| 2026-05-20_16:49 | `feat(iac)`: CloudFront VPC Origin + Internal ALB (`25f3edb`) |
| 2026-05-20_13:14 | `feat`: App-level Cognito JWT auth middleware (`46c0c3d`) |
| 2026-05-19_16:07 | `feat(iac)`: domain_name optional (`d615bc6`) |
| 2026-05-19_15:13 | `feat`: Agent Observability + Trace Viewer UX (`28a283c`) |
| 2026-05-19_15:13 | `docs`: architecture, deployment-guide, demo-scenario, LICENSE (`5c1844e`) |
| 2026-05-19_15:07 | `feat`: control-plane, agent-runtime, tools, scripts, README (`909cb48`) |
| 2026-05-19_14:16 | `feat(iac)`: 7개 모듈 Terraform 구조 완성 (`d04e3fb`) |

### Commit: `25f3edb` feat(iac): use CloudFront VPC Origin with internal ALB
- ALB → `internal = true`, private subnet 배치
- CloudFront VPC Origin으로 ALB 연결 (AWS backbone)
- cloudfront_secret 완전 제거
- ALB SG: VPC CIDR only

### Commit: `46c0c3d` feat: replace ALB Cognito auth with app-level JWT authentication
- FastAPI CognitoAuthMiddleware (JWT 검증)
- /api/auth/* endpoints: signup, verify, login, refresh
- Email 인증 코드 MFA, PW 복잡성 강제
- ALB HTTPS listener 제거

### Commit: `d615bc6` feat(iac): make domain_name optional for zero-prereq deployment
- domain_name = "" → CloudFront 기본 도메인, ACM/Route53 skip

### Commit: `28a283c` feat: add Agent Observability + Trace Viewer UX enhancement
- AgentSpanProcessor (span_filter.py) — context enrichment
- SpanDetailPanel — timing, LLM metrics, cost estimation
- Waterfall + Critical Path + Error cascade

### 인프라 검증 결과 (2026-05-20, ram-test / us-west-2)
- `terraform apply` ✅ 성공 (67 resources)
- CloudFront: `d3vbsh4l7fifca.cloudfront.net`
- Internal ALB + VPC Origin 정상 연결
- S3 bucket 이름 글로벌 충돌 → account_id suffix 추가로 해결
- Cache Policy `cookie_behavior = "all"` + TTL=0 조합 불가 → `"none"` 수정
- SG description non-ASCII 문자 → ASCII only로 수정
- **Infra destroy 완료** (2026-05-21)

## 다음 단계 (다음 세션)

### E2E 배포 검증 (ram-test / us-west-2)
1. `terraform apply` — 인프라 재생성
2. **Docker 이미지 빌드 + ECR push** (platform-api, frontend, base-image, report-image)
3. **DynamoDB seed** (`scripts/seed-dynamodb.sh`)
4. **ECS 서비스 재배포** (force-new-deployment → services-stable 대기)
5. **AgentCore Agent 등록** (`scripts/deploy-agents.sh`)
6. **MCP Gateway 생성** (`scripts/deploy-gateways.sh`)
7. **E2E 테스트**:
   - CloudFront URL 접근 → Frontend 렌더링 확인
   - /api/auth/signup → 이메일 인증 → /api/auth/login → JWT 발급
   - /api/agents (Bearer token) → 에이전트 목록 조회
   - Agent Playground → 실시간 SSE 스트리밍 확인
   - Trace Viewer → OTEL spans 조회
   - CloudWatch Alarm → EventBridge → RCA 자동 실행

### 검증 완료 후
8. 수정 사항 commit (발견된 이슈 해결)
9. PR 생성 및 push

## 배포 대상
- **Account**: ram-test (`355720153146`) / Profile: `ram-test`
- **Region**: `us-west-2`
- **Config**: `domain_name = ""` (CloudFront default domain)

## 소스 프로젝트 위치
- Source: `/Users/ymjoung/workspace/claude-code-project/26y-aiops-platform-v2`
- Target: `/Users/ymjoung/workspace/claude-code-project/team-github-repo/sample-aws-kr-enterprise/ai-ml/agentcore-agent-builder-platform-demo`
