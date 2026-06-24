# Mobile Designer — AI 기반 Android UI 디자인 도구

자연어로 모바일 앱 UI를 설계하고, 단계별로 리뷰·수정한 뒤, Android Studio에서 바로 열 수 있는 Jetpack Compose 프로젝트로 핸드오프하는 웹 애플리케이션입니다.

## 핵심 워크플로우

프로젝트는 4단계로 진행되며, 각 단계는 자연어 채팅으로 수정하고 다음 단계로 변경을 전파할 수 있습니다.

| 단계 | 이름 | 설명 |
|------|------|------|
| 1 | **요구사항(Requirements)** | 자연어 대화 + 파일 업로드(PDF/DOCX/MD/TXT)로 앱 요구사항을 수집하고 구조화 문서로 정리 |
| 2 | **와이어프레임(Wireframe)** | 요구사항 기반으로 화면 구조·내비게이션을 생성, 웹에서 디바이스 목업으로 리뷰 |
| 3 | **디자인(Design)** | 컬러·타이포·스페이싱이 적용된 풀 디자인 생성, 디자인 토큰을 실시간 트윅 |
| 4 | **핸드오프(Handoff)** | Android Studio에서 바로 빌드 가능한 Compose 프로젝트(ZIP) + 디자인 토큰 생성 |

- **단계 전파**: 이전 단계를 수정하면 "다음 단계 업데이트" 버튼으로 하위 단계에 반영
- **실시간 트윅**: 디자인 단계에서 색상/다크모드/회전 등을 웹에서 즉시 미리보기
- **협업**: 코멘트, 공유 링크, 팀 단위 권한 관리

## 아키텍처

```
┌──────────────────────────────────────────────────────────────────┐
│                        CloudFront (CDN)                          │
└──────────────────────────────┬───────────────────────────────────┘
                               │  (단일 오리진: /api/* → API, 그 외 → Web)
                       ┌───────▼────────┐
                       │  ALB (private) │
                       └───┬─────────┬──┘
                  ┌────────▼───┐   ┌─▼──────────────┐
                  │ Web        │   │ API            │
                  │ Next.js    │   │ FastAPI        │
                  │ ECS Fargate│   │ ECS Fargate    │
                  └────────────┘   └───┬────────────┘
                          ┌────────────┼─────────────┐
                  ┌───────▼──────┐ ┌───▼─────┐ ┌─────▼────────┐
                  │  DynamoDB    │ │   S3    │ │  Bedrock     │
                  │ (11 tables)  │ │(files,  │ │ (Claude via  │
                  │              │ │ exports)│ │  inference   │
                  └──────────────┘ └─────────┘ │  profiles)   │
                                               └──────────────┘
```

- **Web**: Next.js (App Router, TypeScript) — ECS Fargate
- **API**: FastAPI (Python 3.12) — ECS Fargate, 라우터→서비스→모델→DB 레이어링
- **AI**: AWS Bedrock + Strands Agents. 모델은 관리자 화면에서 슬롯별 선택(기본 Claude Sonnet/Opus 계열, 최신 Opus 4.8 포함)
- **데이터**: DynamoDB 11개 테이블, S3(업로드·스냅샷·핸드오프 산출물), AI 작업 상태는 DynamoDB에 영속화되어 다중 인스턴스 폴링에도 진행 추적 유지

## Prerequisites

| 도구 | 버전 | 용도 |
|------|------|------|
| Node.js | 18+ | Frontend |
| pnpm | 9+ | 패키지 매니저 (워크스페이스) |
| Python | 3.12+ | Backend |
| Poetry | 1.8+ | Python 의존성 |
| Docker / Podman | 20.10+ | 컨테이너 빌드 (배포 시) |
| AWS CLI | v2+ | AWS 리소스 관리 (배포 시) |

## 로컬 개발

저장소 루트는 pnpm + Turbo 모노레포입니다.

### Backend (API)

```bash
cd apps/api
poetry install

# 로컬 인프라(DynamoDB Local + LocalStack S3)
docker compose up -d
poetry run python scripts/setup_tables.py   # 테이블 생성
cp .env.example .env

poetry run uvicorn src.main:app --reload --port 8080
```

### Frontend (Web)

```bash
cd apps/web
pnpm install
pnpm dev          # http://localhost:3000
```

### 품질 게이트

```bash
# Backend
cd apps/api
poetry run ruff check src
poetry run mypy src
poetry run pytest          # 단위 테스트 (integration은 DynamoDB Local 필요)

# Frontend
cd apps/web
pnpm tsc --noEmit
pnpm build
```

## 배포

단일 스크립트가 ECR 생성, Docker 이미지 빌드/푸시, CloudFormation(중첩 스택) 배포, 프롬프트 시딩까지 처리합니다.

### Linux / macOS

```bash
chmod +x infrastructure/deploy.sh
./infrastructure/deploy.sh \
  --stack-name mdesigner \
  --admin-email admin@example.com \
  --admin-password 'YourSecurePassword123!'
```

### Windows (PowerShell 7+)

```powershell
.\infrastructure\deploy.ps1 `
  -StackName mdesigner `
  -AdminEmail admin@example.com `
  -AdminPassword 'YourSecurePassword123!'
```

### 주요 배포 옵션

| Parameter (bash) | Default | 설명 |
|------------------|---------|------|
| `--stack-name` | (필수) | CloudFormation 스택 이름 |
| `--admin-email` | (필수) | 관리자 이메일 (SES 검증 발신자로도 사용) |
| `--admin-password` | (필수) | 관리자 초기 비밀번호 (첫 로그인 시 변경 강제) |
| `--region` | ap-northeast-2 | AWS 리전 |
| `--environment` | production | 환경 (production/staging) |
| `--table-prefix` | MDesigner | DynamoDB 테이블 접두사 |
| `--domain` | (none) | 커스텀 도메인 |
| `--skip-build` | false | Docker 빌드 건너뛰기 (`--api-image`/`--web-image` 필요) |

> Bedrock은 us-west-2의 cross-region inference profile을 사용합니다(`global.anthropic.claude-*`). 배포 리전과 무관하게 IAM 정책이 이를 허용하도록 구성되어 있습니다.

## 배포 후

배포 완료 시 출력되는 CloudFront URL로 접속합니다.

1. **로그인**: `--admin-email` / `--admin-password`로 로그인 → 첫 로그인 시 비밀번호 변경
2. **모델 설정**: 관리자 → 설정에서 단계별(chat/wireframe/designer/modify/codegen) AI 모델 선택
3. **프롬프트 관리**: 관리자 → 프롬프트에서 8개 슬롯의 시스템 프롬프트 버전 관리

## 설정 (환경 변수)

환경 변수는 `MDESIGNER_` 접두사를 사용합니다. 전체 목록은 `apps/api/.env.example` 참고.

| Variable | 설명 |
|----------|------|
| `MDESIGNER_ENVIRONMENT` | `development` / `staging` / `production` |
| `MDESIGNER_TABLE_PREFIX` | DynamoDB 테이블 접두사 |
| `MDESIGNER_S3_BUCKET_NAME` | 파일/산출물 S3 버킷 |
| `MDESIGNER_JWT_SECRET_SOURCE` | `env`(로컬) / `secretsmanager`(프로덕션) |
| `MDESIGNER_JWT_SECRET_NAME` | JWT 서명 키(또는 Secrets Manager 시크릿 이름) |
| `MDESIGNER_AWS_BEDROCK_REGION` | Bedrock 리전 (기본 us-west-2) |
| `MDESIGNER_CORS_ORIGINS` | CORS 허용 오리진 (JSON 배열) |
| `MDESIGNER_FRONTEND_URL` | 비밀번호 재설정 링크용 웹 베이스 URL |

## 프로젝트 구조

```
.
├── apps/
│   ├── api/                 # FastAPI backend (Python)
│   │   ├── src/
│   │   │   ├── auth/        # 인증·인가 (JWT, 팀 멤버십 기반 접근 제어)
│   │   │   ├── projects/    # 프로젝트·버전·스테이지
│   │   │   ├── ai/          # AI 오케스트레이션 (Bedrock/Strands), 디자인 검증
│   │   │   ├── files/       # 업로드·파싱 (PDF/DOCX/MD/TXT)
│   │   │   ├── handoff/     # Compose 코드 생성·핸드오프 (Gradle wrapper 번들)
│   │   │   ├── collaboration/ # 코멘트·공유·팀
│   │   │   ├── admin/       # 시스템 설정·모델·프롬프트 관리
│   │   │   ├── prompts/     # 프롬프트 로더/슬롯
│   │   │   └── common/      # 설정, DB/S3 클라이언트, 미들웨어, rate limit
│   │   └── scripts/         # 테이블 생성·프롬프트 시딩·관리자 생성
│   └── web/                 # Next.js frontend (TypeScript)
│       └── src/
│           ├── app/         # App Router 페이지 (auth/protected/admin)
│           ├── components/  # design/common/collaboration/project
│           └── lib/         # API 클라이언트, hooks, contexts
├── packages/                # 공유 타입
└── infrastructure/          # CloudFormation 중첩 스택 + 배포 스크립트
    ├── deploy.sh / deploy.ps1
    ├── template.yaml        # 루트 스택
    ├── networking.yaml      # VPC, Subnets, ALB, NAT(이중화)
    ├── compute.yaml         # ECS Fargate 서비스(HA 2-task) + 오토스케일
    ├── storage.yaml         # DynamoDB(11), S3
    ├── security.yaml        # IAM(최소권한), Secrets Manager
    ├── bootstrap.yaml       # 초기 관리자·프롬프트 시딩 Lambda
    └── cdn.yaml             # CloudFront
```

## 리소스 정리

```bash
REGION=ap-northeast-2
# 1. S3 버킷 비우기 (버전 포함) 후 스택 삭제
aws cloudformation delete-stack --stack-name mdesigner --region $REGION
aws cloudformation wait stack-delete-complete --stack-name mdesigner --region $REGION

# 2. ECR 리포지토리 삭제 (CloudFormation 외부)
aws ecr delete-repository --repository-name mdesigner-api --region $REGION --force
aws ecr delete-repository --repository-name mdesigner-web --region $REGION --force
```

> 프로덕션 환경에서는 DynamoDB 테이블에 삭제 보호(DeletionProtection)가 켜져 있어, 스택 삭제 전 해제가 필요할 수 있습니다. S3 버전 관리 버킷은 모든 객체 버전을 비워야 삭제됩니다.
