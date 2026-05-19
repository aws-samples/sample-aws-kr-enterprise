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
| 2026-05-19_14:16 | `feat(iac)`: 7개 모듈 Terraform 구조 완성 (`d04e3fb`) — terraform validate 통과 |

### Commit: `d04e3fb` feat(iac): add modularized Terraform infrastructure (7 modules)
- 소스 프로젝트 단일 Terraform → 7개 모듈 분할 (network, data, auth, registry, iam, compute, cdn)
- deprecated `forwarded_values` → `cache_policy_id` 마이그레이션
- EventBridge endpoint URL 버그 수정
- Circular dependency 해소 (data↔cdn, iam↔compute)
- Provider에서 `shared_credentials_files` 제거

## 다음 단계
1. ~~**Terraform MCP 연동 확인** → 모듈 설계 확정~~ ✅ 완료
2. Design 문서 최종 작성 + 커밋
3. ~~writing-plans 스킬로 Implementation Plan 생성~~ ✅ `docs/superpowers/plans/2026-05-19-terraform-modularization.md`
4. ~~실행 (Clean Copy — Terraform IaC 부분)~~ ✅ 완료
5. 나머지 코드 정제 (control-plane, agent-runtime, tools, scripts)
6. README.md (이중언어) 작성
7. deploy-all.sh 작성 (Terraform output → 후속 단계 연결)

## 소스 프로젝트 위치
- Source: `/Users/ymjoung/workspace/claude-code-project/26y-aiops-platform-v2`
- Target: `/Users/ymjoung/workspace/claude-code-project/team-github-repo/sample-aws-kr-enterprise/ai-ml/agentcore-agent-builder-platform-demo`
