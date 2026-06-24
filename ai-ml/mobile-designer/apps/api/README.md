# Mobile Designer - Backend API

디자인 가이드에 따라 자연어로 Android 앱 UI를 설계하고 핸드오프 파일을 생성하는 백엔드 API.

## Tech Stack

- **Runtime**: Python 3.12+, FastAPI, Uvicorn
- **Database**: DynamoDB (8 tables)
- **Storage**: S3
- **AI**: AWS Bedrock AgentCore (Claude)
- **Auth**: JWT (PyJWT + bcrypt)
- **Package Manager**: Poetry

## Local Development

### Prerequisites
- Python 3.12+
- Poetry
- Docker & Docker Compose

### Setup

```bash
# 1. Install dependencies
cd apps/api
poetry install

# 2. Start local services (DynamoDB Local + LocalStack S3)
docker compose up -d

# 3. Create DynamoDB tables
poetry run python scripts/setup_tables.py

# 4. Copy env
cp .env.example .env

# 5. Run API server
poetry run uvicorn src.main:app --reload --port 8080
```

### Running Tests

```bash
poetry run pytest --cov=src
```

### Linting

```bash
poetry run ruff check .
poetry run mypy src/
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | /auth/register | 회원가입 |
| POST | /auth/login | 로그인 |
| POST | /auth/refresh | 토큰 갱신 |
| POST | /auth/password-reset/request | 비밀번호 재설정 요청 |
| POST | /auth/password-reset/confirm | 비밀번호 재설정 확인 |
| GET | /auth/me | 현재 사용자 정보 |
| POST | /projects | 프로젝트 생성 |
| GET | /projects | 프로젝트 목록 |
| GET | /projects/:id | 프로젝트 상세 |
| PATCH | /projects/:id | 프로젝트 수정 |
| DELETE | /projects/:id | 프로젝트 삭제 |
| POST | /projects/:id/advance-stage | 다음 Stage로 이동 |
| GET | /projects/:id/versions | 버전 히스토리 |
| POST | /projects/:id/revert | 버전 복원 |
| POST | /files/presign | 파일 업로드 URL 발급 |
| POST | /files/complete | 업로드 완료 알림 |
| POST | /ai/generate | AI 디자인 생성 (SSE) |
| POST | /ai/modify | AI 디자인 수정 (SSE) |
| POST | /ai/propagate | Stage 간 변경 전파 (SSE) |
| POST | /handoff/generate | 핸드오프 산출물 생성 |
| GET | /handoff/:project/:version/download | 핸드오프 다운로드 |
| POST | /handoff/build-verify | 빌드 검증 |
| POST | /collaboration/comments | 코멘트 작성 |
| GET | /collaboration/comments | 코멘트 조회 |
| POST | /collaboration/share | 공유 링크 생성 |
| GET | /collaboration/share/:token | 공유 링크 검증 |

## Project Structure

```
apps/api/
├── src/
│   ├── main.py              # FastAPI app entry
│   ├── auth/                # Authentication module
│   ├── projects/            # Project management
│   ├── ai/                  # AI orchestration (Bedrock AgentCore)
│   ├── files/               # File upload & parsing
│   ├── handoff/             # Code generation & handoff
│   ├── collaboration/       # Comments, sharing, teams
│   └── common/              # Shared utilities
├── tests/
├── templates/android/       # Jinja2 templates
├── scripts/                 # Setup scripts
├── pyproject.toml
├── Dockerfile
└── docker-compose.yml
```
