# 02. 시작하기

이 문서에서는 AWS Transform Custom을 사용하기 위한 환경 설정 방법을 안내합니다.

## 사전 요구사항

### AWS 계정 및 권한

- AWS 계정
- Transform Custom 서비스 접근 권한
- AWS 자격 증명 설정 (AWS CLI 또는 환경 변수)

### 로컬 환경

| 도구 | 버전 | 용도 |
|------|------|------|
| AWS CLI | v2.x | AWS 자격 증명 |
| atx CLI | 1.1.x | Transform Custom 실행 |
| Java | 17+ | 샘플 앱 빌드/실행 |
| Maven | 3.8+ | 빌드 도구 |
| Docker | 20.10+ | 컨테이너 빌드 |
| Git | 2.x | 버전 관리 (필수) |

## ATX CLI 설치

AWS Transform Custom은 `atx` CLI를 사용합니다 (`aws transform` 명령어가 아님).

### 1. ATX CLI 설치

AWS 공식 설치 script를 사용합니다. 설치 방법은 [Getting started with AWS Transform custom](https://docs.aws.amazon.com/transform/latest/userguide/custom-get-started.html) 문서를 기준으로 합니다.

> **주의**: AWS Transform CLI는 PyPI로 배포되지 않습니다. `pip install`로 설치하지 마세요. PyPI에 있는 비슷한 이름의 package는 AWS가 배포한 것이 아닙니다.

```bash
# macOS/Linux
curl -fsSL https://transform-cli.awsstatic.com/install.sh | bash

# 버전 확인
atx --version
```

### 2. AWS 자격 증명 설정

```bash
aws configure
# AWS Access Key ID: [입력]
# AWS Secret Access Key: [입력]
# Default region name: ap-northeast-2
# Default output format: json
```

### 3. 자격 증명 확인

```bash
aws sts get-caller-identity
```

## ATX CLI 기본 명령어

### 사용 가능한 Transformation 목록 확인

```bash
# AWS 제공 및 사용자 정의 Transformation 목록
atx custom def list
```

**출력 예시**:
```
Found 8 transformations:
  - 8 AWS Managed transformations

🏢 AWS Managed Transformations:
┌──────────────────────────────────────────┬───────────────────────────────────────┐
│ Transformation Name                      │ Description                           │
├──────────────────────────────────────────┼───────────────────────────────────────┤
│ 🏢 AWS/early-access-java-x86-to-graviton │ Validates Java application compatibi- │
│                                          │ lity with Arm64 architecture...       │
├──────────────────────────────────────────┼───────────────────────────────────────┤
│ 🏢 AWS/java-version-upgrade              │ Upgrade Java applications...          │
├──────────────────────────────────────────┼───────────────────────────────────────┤
│ 🏢 AWS/java-aws-sdk-v1-to-v2             │ Upgrade the AWS SDK from V1 to V2...  │
└──────────────────────────────────────────┴───────────────────────────────────────┘
```

### 명령어 도움말

```bash
# 전체 도움말
atx --help

# Custom Definition 명령어 도움말
atx custom def --help

# 실행 명령어 도움말
atx custom def exec --help
```

## 프로젝트 설정

### 1. 이 저장소 클론

```bash
git clone <repository-url>
cd x86_to_arm64
```

### 2. 디렉토리 구조 확인

```
.
├── README.md
├── docs/                              # 가이드 문서
├── sample-app/                        # 전환 대상 샘플 앱
├── transformation-definition/         # 사용자 정의 변환 규칙 (참고용)
│   └── x86-to-graviton/
└── examples/                          # 사용 예제
```

### 3. 샘플 애플리케이션 확인

```bash
cd sample-app

# Git 초기화 (필수 - atx는 git 저장소에서만 동작)
git init
git add -A
git commit -m "Initial commit"

# 빌드 테스트
mvn clean package -DskipTests

# 로컬 실행
mvn spring-boot:run
```

### 4. 아키텍처 확인

```bash
# 현재 시스템 아키텍처 확인
uname -m
# x86_64 또는 arm64

# 샘플 앱의 아키텍처 정보 API 호출
curl http://localhost:8080/api/system-info
```

예상 응답:
```json
{
  "osArch": "aarch64",
  "architectureType": "ARM64 (Graviton)"
}
```

## 첫 번째 변환 테스트

### 1. 사용 가능한 Transformation 확인

```bash
atx custom def list
```

x86→Graviton 변환의 경우 `AWS/early-access-java-x86-to-graviton` 사용.

### 2. 변환 실행

```bash
cd sample-app

# 기본 실행 (대화형)
atx custom def exec \
  -p . \
  -n "AWS/early-access-java-x86-to-graviton" \
  -c "mvn clean package -DskipTests"

# 비대화형 실행 (자동화용)
atx custom def exec \
  -p . \
  -n "AWS/early-access-java-x86-to-graviton" \
  -c "mvn clean package -DskipTests" \
  -x -t
```

### 3. 옵션 설명

| 옵션 | 전체 옵션 | 설명 |
|------|----------|------|
| `-p` | `--code-repository-path` | 변환 대상 코드 경로 (`.` = 현재 디렉토리) |
| `-n` | `--transformation-name` | Transformation Definition 이름 |
| `-c` | `--build-command` | 빌드 명령어 |
| `-x` | `--non-interactive` | 비대화형 모드 |
| `-t` | `--trust-all-tools` | 모든 도구 자동 승인 |
| `-d` | `--do-not-learn` | 지식 추출 비활성화 |

### 4. 결과 확인

변환 완료 후 생성되는 파일들:
- `ARM64-COMPATIBILITY-REPORT.md` - 호환성 보고서
- `README.md` - 배포 가이드
- `CI-CD-INTEGRATION-GUIDE.md` - CI/CD 통합 가이드
- 검증 스크립트 (`.sh` 파일들)
- Git 커밋 히스토리에 단계별 변경 기록

```bash
# Git 커밋 히스토리 확인
git log --oneline

# 변경된 파일 확인
git diff HEAD~8..HEAD --stat
```

## 문제 해결

### atx 명령어를 찾을 수 없는 경우

```bash
# PATH 확인
which atx

# 직접 경로로 실행
~/.local/bin/atx --version
```

### Git 저장소 오류

```bash
# 오류: "must be managed by git for atx to operate"
# 해결: git 초기화
git init
git add -A
git commit -m "Initial commit"
```

### 권한 오류

```bash
# AWS 자격 증명 확인
aws sts get-caller-identity

# 자격 증명 재설정
aws configure
```

## 다음 단계

[03. Transformation Definition 작성](03-create-transformation.md)에서 사용자 정의 변환 규칙 작성 방법을 확인하세요.

또는 바로 [04. 변환 실행](04-run-transformation.md)에서 AWS 제공 Transformation을 사용하는 방법을 확인하세요.
