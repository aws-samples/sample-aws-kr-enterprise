# Incident to Pull Request on Kiro

AWS 환경에서 Kiro 기반 멀티 에이전트가 장애를 자동 감지하고, 근본 원인을 분석(RCA)하고, 원인이 된 코드 변경을 고치는 풀 리퀘스트(PR)를 여는 데모 플랫폼입니다. 사람이 PR을 리뷰하고 병합하면 워크로드의 배포 파이프라인이 반영합니다. 에이전트는 AWS 리소스를 직접 바꾸지 않습니다.

[agentcore-aiops-demo](../agentcore-aiops-demo)의 RCA 기능과 Web UI에서 출발했습니다. 에이전트는 Strands Agents와 Bedrock 모델 대신 같은 저장소의 [kiro-agent-sdk](../kiro-agent-sdk)(`ai-ml/kiro-agent-sdk`)와 Kiro CLI로 실행하고, Kiro 설정 파일(`.kiro/agents`, steering, skills)로 정의합니다.

## 개요

샘플 워크로드는 GitOps로 운영합니다. 인프라(CDK)와 애플리케이션 코드는 CodeCommit 저장소에 있고, main에 병합된 변경은 CodePipeline이 배포합니다.

1. 개발자가 잘못된 변경을 main에 병합하고, 파이프라인이 배포합니다.
2. CloudWatch 알람이 발생하면 EventBridge와 SQS를 거쳐 Orchestrator Lambda가 AgentCore Runtime의 RCA를 시작합니다.
3. Kiro 멀티 에이전트(Collector, Writer, Reviewer)가 로그, 메트릭, 리소스 상태, 최근 커밋과 배포 기록을 읽기 전용으로 조사해 한국어 RCA 리포트를 씁니다.
4. 리뷰를 통과한 리포트에 저장소에서 고칠 변경이 있으면 Fixer 에이전트가 원인을 고치는 수정 PR을 엽니다. PR에는 `cdk diff` 결과가 댓글로 붙습니다.
5. 사람이 CodeCommit에서 PR을 리뷰하고 병합하면, 파이프라인이 배포하고 알람이 정상으로 돌아옵니다. 리포트는 "배포 완료"로 바뀝니다.

별도의 Chatbot Agent로 알람 이력, 리포트, 인프라 상태, 커밋과 배포 기록을 자연어로 물어볼 수 있습니다.

## Kiro로 개발하고 AgentCore로 배포

이 데모의 에이전트는 Python 코드가 아니라 Kiro 프로젝트입니다. `src/agents/app/workspace/`를 Kiro IDE나 Kiro CLI로 열면 배포되는 것과 같은 에이전트를 데스크톱에서 바로 실행할 수 있습니다.

```
src/agents/app/workspace/.kiro/
|-- agents/                 # 에이전트 10개 (Markdown 프론트매터 + 프롬프트)
|   |-- collector.md        # 서브에이전트(logs, metrics, infrastructure, knowledge, repository)에 조사를 위임
|   |-- writer.md           # rca-report 스킬로 한국어 JSON 리포트 작성
|   |-- reviewer.md         # PASS/FAIL 품질 검증
|   |-- fixer.md            # 리뷰를 통과한 리포트로 수정 PR 1건을 엶
|   |-- chatbot.md          # 채팅 + 리포트/알람 조회 + RCA 요청
|   `-- logs.md, metrics.md, infrastructure.md, knowledge.md, repository.md
|-- steering/
|   `-- incident-operations.md # 모든 에이전트에 항상 적용되는 운영 규칙
`-- skills/
    `-- rca-report/SKILL.md # RCA 리포트 JSON 형식과 작성 규칙
```

개발 흐름은 다음과 같습니다.

1. Kiro 데스크톱에서 에이전트 프롬프트, steering, 스킬, MCP 도구를 만들고 채팅으로 시험합니다.
2. 같은 파일을 그대로 두고 `cdk deploy`를 실행합니다.
3. kiro-agent-sdk가 AgentCore Runtime 안에서 같은 Kiro 프로젝트를 실행합니다. 권한 정책, 세션, 인증, 백그라운드 실행은 SDK와 앱 코드(`main.py`, `agents.py`, `kiro_runtime.py`)가 맡습니다.

데스크톱에서 실행하려면 도구 패키지를 설치하고 AWS 자격 증명을 준비합니다.

```bash
pip install -e src/agents/app        # incident-tools MCP 서버 (boto3 조사 도구, 저장소 도구)
cd src/agents/app/workspace
AWS_REGION=ap-northeast-2 kiro-cli chat --agent collector
```

`chatbot`의 `rca_agent` 도구는 배포된 Runtime에서만 동작합니다. 수정 PR 도구(`open_fix_pull_request`)는 Runtime이 리포트마다 만드는 Fixer 세션에서만 동작하므로, 데스크톱에서는 호출을 거부합니다.

## 주요 기능

- **자동 RCA 분석**: CloudWatch 알람 발생 시 멀티 에이전트가 근본 원인을 추적하고 한국어 리포트 생성
- **Reflexion 패턴**: Collector, Writer, Reviewer 순환 구조로 리포트 품질을 자체 검증, 최대 3회 반복
- **배포 기록과 연결**: Repository 에이전트가 최근 커밋, 변경 내용, 파이프라인 실행을 읽어 장애 직전 배포와 원인을 연결
- **수정은 PR로**: Fixer 에이전트가 원인을 고치는 PR을 열고, 사람이 리뷰하고 병합하면 파이프라인이 배포. 에이전트의 실행 역할에는 AWS 리소스 쓰기 권한이 없음
- **PR 검사**: PR이 열리거나 갱신되면 CodeBuild가 AWS 쓰기 권한 없이 `cdk diff` 결과를 PR 댓글로 남김
- **Kiro 서브에이전트**: 5개 Specialist Agent(Logs, Metrics, Infrastructure, Knowledge, Repository)를 Kiro 서브에이전트로 호출하는 계층적 구조. 역할별로 호출할 수 있는 서브에이전트와 도구를 SDK 권한 정책으로 제한
- **웹 패스워드**: Web UI, REST API, WebSocket 모두 배포 전에 정한 공유 패스워드로 로그인해야 사용 가능
- **워크로드 독립**: 분석 플랫폼과 워크로드 간 결합 없음, 등록한 워크로드의 alarm prefix와 맞는 알람만 분석
- **지식 검색**: S3에 저장한 런북, 아키텍처 문서를 Knowledge Agent가 검색 (별도 벡터 DB 없음)
- **WebSocket 채팅**: REST API의 29초 제한 없이 최대 15분 걸리는 답변도 받는 WebSocket API, Kiro 세션과 Kiro memory로 대화 기억
- **실시간 알림**: RCA 완료 시 Slack(Webhook) 자동 발송

## 기술 스택

| 영역 | 기술 |
|------|------|
| Frontend | React 18, MUI 7, react-router-dom 7, react-markdown (GFM) |
| Backend API | API Gateway (REST + WebSocket), Lambda (Python 3.12) |
| Agent Runtime | kiro-agent-sdk, Kiro CLI 2.35.1, Amazon Bedrock AgentCore Runtime (Docker arm64) |
| Agent 정의 | Kiro custom agents, steering, skills, MCP 서버 (`incident-tools`) |
| LLM | Kiro 모델 `claude-opus-5.5` (Kiro 구독으로 사용) |
| GitOps | CodeCommit (워크로드 저장소), CodePipeline + CodeBuild (`cdk deploy`, PR의 `cdk diff`) |
| 지식 검색 | S3 문서 + BM25 검색 도구 |
| Data Store | DynamoDB (workloads, reports, chat-memory) |
| Storage | S3 (knowledge, build, web) |
| Messaging | EventBridge, SQS |
| CDN / Hosting | CloudFront + S3 (SPA 호스팅) |
| Container Build | ECR + CodeBuild (Agent 컨테이너 자동 빌드) |
| 인증 | Secrets Manager (Kiro API key 또는 Kiro 웹 로그인 토큰, Web UI 패스워드) |
| IaC | AWS CDK 2 (TypeScript). 플랫폼 2개 스택 + 파이프라인이 배포하는 워크로드 스택 |

## 아키텍처

```
Developer, or the Fixer agent through a pull request
  |
  v
CodeCommit (incident-to-pr-workload, main)
  |  merge
  v
CodePipeline --> CodeBuild: cdk deploy --> IncidentToPrSampleAppStack (ALB, EC2 x2, RDS)

CloudWatch alarm
  |
  v
EventBridge --> SQS --> Orchestrator Lambda (concurrency 5) --> DynamoDB (report: ANALYZING)
                               |
                               v
                  AgentCore Runtime (kiro-agent-sdk)
                  +----------------------------------+
                  | Collector -> Writer -> Reviewer  |
                  | Fixer: fix pull request          |
                  +----------------------------------+
                               |
                               v
                  DynamoDB (report, pull request) + Slack

User --> CloudFront --> S3 (React SPA)
            |
            +--> API Gateway (REST) ------> API Handler Lambda
            |                                 workloads, knowledge sync,
            |                                 reports with pull request and deployment state
            |
            +--> API Gateway (WebSocket) -> WS Handler Lambda --> Chatbot agent
```

### 데이터 흐름

1. **감지**: CloudWatch 알람, EventBridge, SQS Queue 순서로 이벤트 전달
2. **즉시 저장**: Orchestrator Lambda가 DynamoDB에 "분석 중" 리포트를 즉시 생성 (Web UI에 바로 표시)
3. **워크로드 매칭**: 알람 이름을 DynamoDB `alarm_prefixes`와 매칭하여 워크로드 컨텍스트 로드
4. **RCA 분석**: AgentCore Runtime에서 Kiro 에이전트 루프 실행 (Collector, Writer, Reviewer, 최대 3회 반복)
5. **수정 PR**: 리뷰를 통과하고 권장 조치가 있으면 Fixer 에이전트가 `incident/fix-<리포트 ID>` 브랜치로 PR을 엶. 리포트에 PR 번호를 저장하고 Slack 알림 발송
6. **리뷰와 배포**: 사람이 PR을 병합하면 파이프라인이 배포. 리포트 상태는 PR과 파이프라인 실행 기록에 따라 "PR 리뷰 대기", "병합됨, 배포 중", "배포 완료"로 바뀜

### 멀티 에이전트 구조

```
+------------------------------------------------------------------+
| AgentCore Runtime (kiro-agent-sdk)                               |
|                                                                  |
| /invocations --+-- agent=rca -----> RCA loop, then Fixer         |
|                +-- agent=chatbot -> Chatbot                      |
|                                                                  |
| RCA loop (Reflexion pattern, up to 3 rounds)                     |
|   Collector --> Writer --> Reviewer --+-- PASS --> report        |
|       ^         (rca-report skill)    +-- FAIL --> Collector     |
|       |                                                          |
|       +-- logs            CloudWatch Logs, CloudTrail, Config    |
|       +-- metrics         CloudWatch metrics and alarms          |
|       +-- infrastructure  EC2, ALB, RDS, SG, ASG, ECS            |
|       +-- knowledge       runbooks, system specs, uploads        |
|       +-- repository      commits, diffs, pipeline runs          |
|                                                                  |
| Fixer: repository tools + open_fix_pull_request                  |
|        (one pull request per report)                             |
|                                                                  |
| Chatbot: subagents logs, metrics, infrastructure, knowledge,     |
|          repository; tools rca_agent, query_reports,             |
|          query_alarms; Kiro session + Kiro memory                |
+------------------------------------------------------------------+
```

각 Specialist Agent는 자신의 도구 그룹만 제공하는 MCP 서버(`incident-tools <group>`)를 사용합니다. 수정 PR 도구(`changes` 그룹)는 Fixer만 쓸 수 있습니다. 이 제한은 프롬프트가 아니라 SDK 권한 콜백(`kiro_runtime.py`)과 IAM이 적용합니다.

## Kiro 인증

AgentCore Runtime의 Kiro CLI는 다음 두 방식 중 하나로 인증합니다.

| | API key (기본) | 웹 로그인 |
|---|---|---|
| 저장 위치 | Secrets Manager `incident-to-pr/kiro-api-key` | `incident-to-pr/kiro-web-login` (refresh token), `incident-to-pr/kiro-web-token` (access token) |
| 유효 기간 | 폐기할 때까지 | access token 약 1시간. 갱신 Lambda가 15분마다 refresh token으로 갱신 |
| 재인증 | 필요 없음 | IAM Identity Center 세션이 끝나면 로그인 스크립트를 다시 실행 |
| 사용 조건 | Kiro Pro, Pro+, Power 등 API key를 지원하는 구독. 관리형 구독은 관리자가 API key를 허용해야 함 | Builder ID 또는 IAM Identity Center로 Kiro에 로그인할 수 있는 사용자 |
| 주의 | 유출되면 폐기할 때까지 누구나 사용할 수 있습니다. 시크릿 접근을 Runtime 역할로 제한하고, 정기적으로 교체하며, 유출이 의심되면 즉시 폐기하세요 | refresh token도 자격 증명입니다. 서비스가 로그인한 사용자 권한으로 동작하므로 운영 전용 사용자로 로그인하세요 |

웹 로그인은 access token이 짧아 그대로는 상시 운영에 쓰기 어렵습니다. 이 데모는 refresh token을 갱신 Lambda만 읽을 수 있는 별도 시크릿에 두고, Runtime에는 짧은 access token만 전달합니다. 그래서 IAM Identity Center 세션이 유지되는 동안 재로그인 없이 동작합니다. IAM Identity Center는 [Kiro용 확장 세션](https://docs.aws.amazon.com/singlesignon/latest/userguide/90-day-extended-session-duration.html)을 최대 90일까지 설정할 수 있습니다. 외부 IdP를 사용하면 IdP 세션이 더 짧은 경우 그 기간이 적용됩니다. 갱신 Lambda가 실패하면 RCA가 시작되지 않으므로 Lambda 오류 알람을 설정하세요.

## Kiro 이용 조건

Kiro를 이용한 자동화는 소프트웨어 개발 분야로 한정됩니다(예: CI/CD 중 코드 리뷰). Kiro 사용에 대한 자세한 내용은 [Kiro 요금 FAQ](https://kiro.dev/pricing/)를 참고하세요.

## 보안 경계

이 프로젝트는 데모입니다. 다음을 전제로 설계했습니다.

- **전용 샌드박스 계정**에 배포합니다. 운영 리소스가 있는 계정에는 배포하지 마세요.
- 웹 패스워드를 아는 사람은 모두 **같은 관리자 권한**을 가집니다. 워크로드 수정, 문서 업로드를 누구나 할 수 있습니다.
- 데모가 끝나면 [정리](#정리)에 따라 스택과 시크릿을 삭제합니다.

에이전트의 실행 역할에는 AWS 리소스를 바꾸는 권한이 없고, 워크로드 저장소에는 `incident/` 브랜치에만 파일을 쓸 수 있습니다. 병합과 main 변경은 IAM으로 거부합니다. 병합 전 PR 코드를 실행하는 PR 검사도 AWS 쓰기 권한 없이 실행하며, 배포 권한은 main을 배포하는 파이프라인에만 있습니다. 데모는 PR 승인 규칙을 만들지 않으므로 운영에서는 CodeCommit 승인 규칙으로 리뷰어 승인을 강제하세요. 웹 인증, PR 검사, PR 범위, 결과 판정, 알람 처리, 지식 문서, Slack, 브라우저 통제와 남는 한계는 [docs/security.md](docs/security.md)에 정리했습니다.

## 시작하기

### 사전 요구사항

- AWS CLI v2 설정 완료 (`aws configure`)
- Node.js 20+ (CDK는 `npx aws-cdk`로 실행하므로 따로 설치하지 않아도 됩니다)
- Python 3.12+
- Kiro 구독 (API key 또는 웹 로그인, [Kiro 인증](#kiro-인증) 참고)
- CodeCommit을 지원하는 리전 (기본 `ap-northeast-2`)

이 데모는 같은 저장소의 [kiro-agent-sdk](../kiro-agent-sdk)(`ai-ml/kiro-agent-sdk`)로 Agent 컨테이너를 빌드하므로, 저장소 전체를 받아 이 디렉터리에서 시작합니다. 아래 명령은 모두 이 디렉터리(`ai-ml/agentcore-kiro-incident-to-pr`)에서 실행합니다.

```bash
git clone https://github.com/aws-samples/sample-aws-kr-enterprise.git
cd sample-aws-kr-enterprise/ai-ml/agentcore-kiro-incident-to-pr
```

CDK는 kiro-agent-sdk 소스의 SHA-256이 `infra/cdk.json`의 `kiroAgentSdkSha256`과 같은지 확인합니다. 이 데모는 그 SDK로 테스트했습니다. SDK를 다른 위치에 두었다면 배포 명령에 `-c kiroAgentSdkPath=<infra 기준 상대 경로>`를, 다른 버전의 SDK로 배포하려면 `-c allowUntestedSdk=true`를 추가합니다.

### 배포

#### 1. Kiro 인증 정보 저장

API key 방식:

```bash
aws secretsmanager create-secret --region ap-northeast-2 \
  --name incident-to-pr/kiro-api-key --secret-string "$KIRO_API_KEY"
```

API key 발급과 사용 조건은 [Kiro 인증 문서](https://kiro.dev/docs/getting-started/authentication/)를 참고하세요. 셸 기록에 키가 남지 않도록 환경 변수나 파일로 전달하세요.

웹 로그인 방식:

```bash
pip install boto3
python scripts/kiro_web_login.py --start-url https://my-org.awsapps.com/start --idc-region us-east-1
# AWS Builder ID라면: python scripts/kiro_web_login.py --builder-id
```

출력된 URL을 브라우저에서 열어 승인하면 두 시크릿이 저장됩니다. 4단계 배포 명령에는 `-c kiroAuthMode=web`을 추가합니다.

인증 정보는 배포 후에 저장해도 됩니다. 저장하기 전에 발생한 알람은 "Kiro 인증 정보를 읽지 못했습니다" 리포트로 끝납니다.

#### 2. Web UI 패스워드 저장

Web UI와 API는 이 패스워드로 로그인해야 사용할 수 있습니다. 패스워드는 16자 이상, `signing_key`는 32자 이상이어야 하며 무작위 값을 권장합니다.

```bash
python3 -c 'import json, secrets; print(json.dumps({"password": secrets.token_urlsafe(18), "signing_key": secrets.token_urlsafe(48)}))' > web-password.json
aws secretsmanager create-secret --region ap-northeast-2 \
  --name incident-to-pr/web-password --secret-string file://web-password.json
cat web-password.json   # password 값으로 로그인합니다
rm web-password.json
```

패스워드를 바꾸려면 같은 방법으로 새 값을 만들어 `aws secretsmanager put-secret-value --secret-id incident-to-pr/web-password --secret-string file://web-password.json`을 실행합니다. 기존 로그인은 60초 안에 만료됩니다.

#### 3. GitOps 스택 (워크로드 저장소와 파이프라인)

```bash
cd infra
npm ci
AWS_REGION=ap-northeast-2 npx aws-cdk bootstrap
AWS_REGION=ap-northeast-2 npx aws-cdk deploy IncidentToPrGitOpsStack --require-approval never
cd ..
```

- CodeCommit 저장소 `incident-to-pr-workload`를 만들고 이 데모의 `workload/` 디렉터리로 첫 커밋을 채웁니다.
- 파이프라인 `incident-to-pr-workload`가 바로 실행되어 샘플 워크로드 스택 `IncidentToPrSampleAppStack`을 배포합니다(ALB, EC2 2대, RDS, CloudWatch 알람 9개, 약 10분).
- 파이프라인은 CDK bootstrap 역할로 `cdk deploy`를 실행합니다.
- 이후 워크로드 변경은 이 저장소의 main에 병합하는 방식으로만 합니다.
- 첫 배포에서 새 인스턴스가 헬스체크를 통과하기 전에 `incident-to-pr-alb-unhealthy` 알람이 1~2분 울릴 수 있습니다. 5단계에서 워크로드를 등록하기 전의 알람은 분석하지 않습니다.

출력:

```
Outputs:
  IncidentToPrGitOpsStack.WorkloadRepositoryCloneUrl = codecommit::ap-northeast-2://incident-to-pr-workload
  IncidentToPrGitOpsStack.WorkloadRepositoryConsole = https://ap-northeast-2.console.aws.amazon.com/codesuite/codecommit/repositories/incident-to-pr-workload/browse?region=ap-northeast-2
  IncidentToPrGitOpsStack.WorkloadPipelineConsole = https://ap-northeast-2.console.aws.amazon.com/codesuite/codepipeline/pipelines/incident-to-pr-workload/view?region=ap-northeast-2
```

#### 4. 분석 플랫폼 + Web UI

```bash
cd infra
AWS_REGION=ap-northeast-2 npx aws-cdk deploy IncidentToPrPlatformStack --require-approval never
# 웹 로그인 방식: AWS_REGION=ap-northeast-2 npx aws-cdk deploy IncidentToPrPlatformStack -c kiroAuthMode=web --require-approval never
cd ..
```

CDK가 Web UI 빌드(`npm ci`, `npm run build`), S3 배포, CloudFront invalidation까지 수행하고, Agent 컨테이너는 CodeBuild(arm64)가 kiro-agent-sdk와 함께 빌드합니다(약 15분). `package-lock.json` 파일들은 이 데모를 테스트한 의존성 버전을 고정합니다. 재현성을 위한 것이며 취약점 점검을 대신하지 않습니다.

출력:

```
Outputs:
  IncidentToPrPlatformStack.WebUrl = https://xxxx.cloudfront.net
  IncidentToPrPlatformStack.ApiUrl = https://xxxx.execute-api.ap-northeast-2.amazonaws.com/prod/
  IncidentToPrPlatformStack.WsUrl = wss://xxxx.execute-api.ap-northeast-2.amazonaws.com/prod
  IncidentToPrPlatformStack.KnowledgeBucketName = incident-to-pr-knowledge-xxxx
  IncidentToPrPlatformStack.AgentRuntimeArn = arn:aws:bedrock-agentcore:ap-northeast-2:xxxx:runtime/incident_to_pr_agent-xxxx
```

#### 5. 워크로드 등록

`WebUrl`의 Web UI에 로그인해 워크로드를 등록하고(Alarm Prefix `incident-to-pr-`), 워크로드 상세 페이지에서 **지식 동기화**를 누릅니다. 동기화하면 `knowledge-base/`의 런북과 시스템 스펙이 에이전트 검색 대상이 됩니다. [워크로드 등록 방법](#워크로드-등록-방법)을 참고하세요.

## 장애 시나리오 실행 예시

개발자가 ALB 헬스체크 경로를 `/health`에서 `/healthz`로 잘못 바꾼 커밋을 main에 넣는 상황입니다. 애플리케이션은 `/health`만 응답하므로 헬스체크가 404를 받습니다.

```bash
pip install boto3
python scripts/break_workload.py --region ap-northeast-2
```

1. 파이프라인이 커밋을 배포합니다(약 2~5분). 배포가 끝나면 ALB 타겟이 unhealthy(`Target.ResponseCodeMismatch`)가 되고 몇 분 뒤 `incident-to-pr-alb-unhealthy` 알람이 발생합니다. 대시보드에 "분석 중" 리포트가 나타납니다.
2. 5분 안팎에 리포트가 완성됩니다. 에이전트가 타겟 상태, 최근 파이프라인 실행, 원인 커밋(`lib/sample-app-stack.ts`의 `healthCheck.path`를 `/healthz`로 바꾼 변경)을 근거로 원인을 설명합니다.
3. Fixer 에이전트가 경로를 `/health`로 되돌리는 PR을 엽니다. 리포트 화면의 **CodeCommit에서 PR 열기**로 PR과 `cdk diff` 댓글을 확인합니다.
4. CodeCommit 콘솔에서 PR을 병합합니다. 파이프라인이 배포하면 ALB 타겟이 healthy로 돌아오고 알람이 OK가 됩니다. 리포트는 "배포 완료"로 바뀝니다.

## Web UI 페이지

### 로그인
- 배포 전에 저장한 웹 패스워드로 로그인 (탭을 닫거나 12시간이 지나면 다시 로그인)

### 대시보드 (`/`)
- 알람 상태 요약 (OK / ALARM 카운트)
- 최근 RCA 리포트 목록과 상태 (분석 중, PR 리뷰 대기, 병합됨, 배포 중, 배포 완료, 분석 완료, PR 없음, 실패)

### 워크로드 (`/workloads`)
- 워크로드 등록/수정/삭제
- Alarm Prefix 매핑, 알림 설정 (Slack)

### 워크로드 상세 (`/workloads/:id`)
- 워크로드 정보 편집
- 지식 문서 관리 (업로드/목록/삭제)
- 지식 동기화 버튼 (동기화 결과 + 처리 건수 표시)

### RCA 리포트 (`/reports/:id`)
- RCA 분석 결과 (요약, 타임라인, 근본 원인, 영향 범위)
- 수정 PR: PR 번호, 브랜치, 바뀐 파일, 상태(열림, 병합됨, 배포 완료, 닫힘), CodeCommit PR 링크
- 권장 조치: 설명과 코드 변경 내용 (참고용, 실제 변경은 PR)

### 채팅 (`/chat`)
- WebSocket 기반 실시간 채팅 (답변 최대 15분)
- GFM Markdown, 테이블 렌더링 지원
- Kiro 세션으로 대화 유지 (새 microVM에서는 최근 6턴을 이어서 전달), Kiro memory로 장기 기억

## API 엔드포인트

`/api/login`을 제외한 모든 요청은 `Authorization: Bearer <token>` 헤더가 필요합니다. WebSocket은 연결 URL의 `token` 쿼리 파라미터와 모든 `sendMessage` 메시지의 `token` 필드로 같은 토큰을 보냅니다.

| Method | Path | 설명 |
|--------|------|------|
| POST | `/api/login` | 웹 패스워드로 로그인, 토큰 발급 |
| GET | `/api/workloads` | 워크로드 목록 조회 |
| POST | `/api/workloads` | 워크로드 등록 |
| GET | `/api/workloads/{id}` | 워크로드 상세 조회 |
| PUT | `/api/workloads/{id}` | 워크로드 수정 |
| DELETE | `/api/workloads/{id}` | 워크로드 삭제 |
| POST | `/api/workloads/{id}/upload-url` | 문서 업로드용 presigned POST 발급 (1MB 이하) |
| GET | `/api/workloads/{id}/documents` | 업로드된 문서 목록 조회 |
| DELETE | `/api/workloads/{id}/documents` | 문서 삭제 |
| POST | `/api/workloads/{id}/sync` | 지식 문서 동기화 |
| GET | `/api/reports` | RCA 리포트 목록 조회 (PR 상태 포함) |
| GET | `/api/reports/{id}` | RCA 리포트 상세 조회 (PR 상태, PR 링크 포함) |
| DELETE | `/api/reports/{id}` | RCA 리포트 삭제 |
| GET | `/api/status` | 알람 상태 요약 + WebSocket URL |
| POST | `/api/chat` | 채팅 (REST fallback) |
| WebSocket | `sendMessage` | 채팅 (WebSocket, 권장) |

## 프로젝트 구조

```
agentcore-kiro-incident-to-pr/
|-- infra/                          # AWS CDK 인프라 (TypeScript)
|   |-- bin/app.ts                  # CDK 앱 엔트리포인트 (IncidentToPrGitOpsStack, IncidentToPrPlatformStack)
|   `-- lib/
|       |-- gitops-stack.ts         # 워크로드 저장소, 배포 파이프라인, PR 검사
|       `-- platform-stack.ts       # 분석 플랫폼 스택
|-- workload/                       # 워크로드 저장소의 첫 내용 (파이프라인이 배포하는 CDK 앱)
|   |-- bin/app.ts, cdk.json        # IncidentToPrSampleAppStack
|   |-- lib/sample-app-stack.ts     # VPC, ALB, EC2 ASG, RDS, CloudWatch 알람
|   `-- app/app.py                  # Flask 헬스체크 앱
|-- src/
|   |-- lambdas/
|   |   |-- web_auth.py             # 웹 패스워드 로그인과 토큰 확인 (REST, WebSocket 공용)
|   |   |-- orchestrator/           # 알람, 워크로드 매칭, AgentCore 호출
|   |   |-- api_handler/            # REST API (워크로드 CRUD, 리포트와 PR 상태, 지식 동기화)
|   |   |-- ws_handler/             # WebSocket 채팅 핸들러
|   |   `-- kiro_token_refresher/   # Kiro 웹 로그인 토큰 갱신 (웹 로그인 방식)
|   `-- agents/app/                 # AgentCore Runtime 컨테이너
|       |-- workspace/.kiro/        # Kiro 프로젝트: agents, steering, skills
|       |-- incident_tools/         # MCP 서버: 조사(aws.py), 저장소와 수정 PR(repository.py), 지식, 리포트
|       |-- main.py                 # /invocations, /ping (rca, chatbot 라우팅), RCA 후 Fixer 실행
|       |-- agents.py               # RCA 루프, Fixer, Chatbot 기억
|       |-- kiro_runtime.py         # kiro-agent-sdk 설정: 인증, 권한 정책, 세션
|       |-- pyproject.toml          # incident-tools 패키지
|       `-- Dockerfile              # Python 3.12 slim + Kiro CLI 2.35.1, arm64
|-- scripts/
|   |-- kiro_web_login.py           # Kiro 웹 로그인 (device code) 후 시크릿 저장
|   `-- break_workload.py           # 장애 시나리오: 잘못된 헬스체크 경로 커밋
|-- web-ui/                         # React SPA (MUI, 한국어)
|-- knowledge-base/                 # 런북, 시스템 스펙 (시드 데이터)
|-- docs/security.md                # 보안 통제와 남는 한계
|-- tests/                          # 보안 통제 로컬 테스트 (moto, Kiro 없이 실행)
`-- README.md
```

## Agent 도구 목록

### 조사 도구 (Read-only)

| 도구 | 에이전트 | 설명 |
|------|---------|------|
| `query_cloudwatch_logs` | logs | CloudWatch Logs Insights 쿼리 |
| `lookup_cloudtrail_events` | logs | CloudTrail API 변경 이력 (누가 무엇을 변경했는지) |
| `get_ec2_console_output` | logs | EC2 인스턴스 시스템 로그 |
| `get_config_history` | logs | AWS Config 리소스 설정 변경 이력 |
| `get_metric_data` | metrics | CloudWatch 메트릭 데이터 조회 |
| `describe_alarms` | metrics | CloudWatch 알람 상태 조회 |
| `list_metrics` | metrics | CloudWatch 메트릭 목록 조회 |
| `describe_instances` | infrastructure | EC2 인스턴스 상태 |
| `describe_target_health` | infrastructure | ALB Target Group 헬스 체크 |
| `check_security_groups` | infrastructure | Security Group 규칙 조회 |
| `describe_db_instances` | infrastructure | RDS 인스턴스 상태 |
| `describe_auto_scaling_groups` | infrastructure | ASG 상태 및 인스턴스 목록 |
| `describe_ecs_services` | infrastructure | ECS 서비스/태스크 상태 |
| `describe_lambda_functions` | infrastructure | Lambda 함수 설정 |
| `describe_nat_gateways` | infrastructure | NAT Gateway 상태 |
| `describe_vpcs` | infrastructure | VPC 상태 |
| `list_recent_commits` | repository, fixer | 워크로드 저장소의 최근 커밋 |
| `get_commit_changes` | repository, fixer | 커밋이 바꾼 파일과 diff |
| `read_repository_file`, `list_repository_files` | repository, fixer | 저장소 파일 읽기, 목록 |
| `list_pipeline_executions` | repository, fixer | 배포 파이프라인 실행 기록과 배포한 커밋 |
| `search_knowledge` | knowledge | 런북, 시스템 스펙, 업로드 문서 검색 (BM25) |
| `query_reports`, `query_alarms` | chatbot | RCA 리포트, 알람 이력 조회 |

### 수정 PR 도구

| 도구 | 에이전트 | 설명 |
|------|---------|------|
| `open_fix_pull_request` | fixer | `incident/fix-<리포트 ID>` 브랜치에 파일 변경을 올리고 main으로 PR을 엶. Fixer 세션당 1회 |

## DynamoDB 스키마

| 테이블 | Partition Key | 용도 | 비고 |
|--------|--------------|------|------|
| `incident-to-pr-workloads` | `workload_id` | 워크로드 등록 정보 (이름, 설명, alarm prefix, 알림 설정) | 즉시 조회 |
| `incident-to-pr-reports` | `report_id` | RCA 리포트와 수정 PR (`ANALYZING`, `PENDING`, `MERGED`, `RESOLVED`, `REPORTED`, `FAILED`) | GSI: `workload-created-index` |
| `incident-to-pr-chat-memory` | `pk` + `sk` | 채팅 최근 6턴(`session#<id>`), Kiro memory 보존(`memory#user`) | 대화와 장기 기억 모두 30일 후 만료 (장기 기억은 마지막 변경 기준) |

리포트 상태는 PR 상태에서 계산합니다. PR이 열려 있으면 `PENDING`, 병합됐지만 배포 전이면 `MERGED`, 병합 커밋을 포함한 파이프라인 실행이 성공하면 `RESOLVED`입니다. 리뷰를 통과했지만 고칠 코드 변경이 없거나 PR이 병합 없이 닫히면 `REPORTED`(분석 완료, PR 없음)입니다.

## CDK 스택 구성

### IncidentToPrGitOpsStack

- **CodeCommit**: 워크로드 저장소 `incident-to-pr-workload` (첫 커밋은 `workload/`)
- **CodePipeline**: main 변경을 감지해 CodeBuild로 `cdk deploy IncidentToPrSampleAppStack`
- **CodeBuild**: 배포 프로젝트(CDK bootstrap 역할로 `cdk deploy`), PR 검사 프로젝트(bootstrap 역할 없이 템플릿 `cdk diff`를 PR 댓글로)
- **EventBridge**: PR 생성과 갱신 이벤트로 PR 검사 시작

### IncidentToPrPlatformStack

- **DynamoDB**: workloads, reports, chat-memory (On-Demand, DESTROY)
- **S3**: knowledge (시드 데이터 자동 배포), build (Agent, SDK 소스), web (SPA 호스팅)
- **AgentCore**: Runtime (kiro-agent-sdk 컨테이너, 호출은 DEFAULT 엔드포인트). 역할: 조사 API 읽기, 저장소 읽기, `incident/` 브랜치와 PR 생성
- **Secrets Manager**: Kiro 인증 정보, Web UI 패스워드 (배포 전에 사용자가 생성, 스택은 읽기 권한만 부여)
- **ECR + CodeBuild**: Agent 컨테이너 자동 빌드/배포 (Custom Resource)
- **Lambda**: Orchestrator (30s, concurrency 5), API Handler (120s), WS Handler (900s), 토큰 갱신 (웹 로그인 방식, 15분 주기)
- **API Gateway**: REST (CORS 전체 허용, 웹 패스워드 토큰 필요, stage throttling) + WebSocket (sendMessage 라우트, 토큰 필요)
- **EventBridge, SQS**: CloudWatch 알람 ALARM 상태 변경 이벤트 캡처
- **CloudFront**: S3 Origin (SPA) + API Gateway Origin (`/api/*` 프록시), 보안 응답 헤더(CSP, HSTS 등)

### IncidentToPrSampleAppStack (파이프라인이 배포)

- **VPC**: 2 AZ, Public/Private 서브넷, NAT Gateway 1개
- **ALB + ASG**: t3.micro x 2, Flask 헬스체크 앱
- **RDS**: PostgreSQL 16, t3.micro, 20GB, 저장소 암호화
- **CloudWatch 알람 9개** (`incident-to-pr-` prefix):
  - ALB: unhealthy targets, 5XX, 4XX, high latency
  - EC2: CPU high, status check failed
  - RDS: CPU high, storage low, read latency

## 환경 변수

### AgentCore Runtime

| 변수 | 설명 |
|------|------|
| `KIRO_MODEL` | Kiro 모델 ID (`claude-opus-5.5`) |
| `KIRO_AUTH_MODE` | `apikey` 또는 `web` |
| `KIRO_SECRET_ID` | Kiro 인증 시크릿 이름 |
| `KNOWLEDGE_BUCKET` | S3 knowledge 버킷명 |
| `REPORTS_TABLE` | DynamoDB reports 테이블명 |
| `WORKLOADS_TABLE` | DynamoDB workloads 테이블명 |
| `MEMORY_TABLE` | DynamoDB chat-memory 테이블명 |
| `WORKLOAD_REPOSITORY`, `WORKLOAD_PIPELINE` | 워크로드 저장소와 파이프라인 이름 (`incident-to-pr-workload`) |
| `FIX_BRANCH_PREFIX` | 수정 PR 브랜치 접두어 (`incident/`) |
| `WEB_URL` | CloudFront Web UI URL |
| `AWS_REGION` | AWS 리전 (`ap-northeast-2`) |

### Lambda

| 변수 | 대상 Lambda | 설명 |
|------|------------|------|
| `AGENT_RUNTIME_ARN` | Orchestrator, API Handler, WS Handler | AgentCore Runtime ARN |
| `REPORTS_TABLE` | Orchestrator, API Handler | DynamoDB reports 테이블명 |
| `WORKLOADS_TABLE` | Orchestrator, API Handler | DynamoDB workloads 테이블명 |
| `KNOWLEDGE_BUCKET` | API Handler | S3 knowledge 버킷명 |
| `WORKLOAD_REPOSITORY`, `WORKLOAD_PIPELINE` | API Handler | PR 상태와 배포 여부 확인 |
| `WEB_PASSWORD_SECRET_ID` | API Handler, WS Handler | Web UI 패스워드 시크릿 이름 |
| `WS_ENDPOINT` | WS Handler | WebSocket API 관리 엔드포인트 |
| `LOGIN_SECRET_ID`, `TOKEN_SECRET_ID` | 토큰 갱신 | 웹 로그인 시크릿 이름 |

## 워크로드 등록 방법

1. **워크로드** 페이지에서 **워크로드 등록** 클릭
2. 워크로드 정보 입력:
   - **Workload ID**: 고유 식별자, 영문자, 숫자, `-`, `_`만 사용 (예: `my-web-app`)
   - **이름**: 표시 이름
   - **설명**: 아키텍처, 리소스, 의존성 등 요약 (에이전트가 초기 컨텍스트로 사용)
   - **Alarm Prefix**: CloudWatch 알람 이름 prefix (쉼표 구분, 3자 이상, 예: `my-app-,myapp-`)
   - **Slack Webhook**: Slack incoming webhook URL (`https://hooks.slack.com/services/...`). 저장 후에는 화면에 다시 표시하지 않습니다
3. 워크로드 상세 페이지에서 **문서 업로드** 후 **지식 동기화**를 누르면 Knowledge Agent 검색 대상에 반영됩니다 (텍스트 파일, 1MB 이하, 업로드 중인 파일을 포함해 워크로드당 20개)

## 알려진 제한사항

- **RCA 소요 시간**: Collector, Writer, Reviewer 순차 실행과 Fixer까지 수 분 소요. 복잡한 장애는 feedback loop로 더 길어질 수 있음
- **배포 시간**: 수정 PR을 병합한 뒤 파이프라인 배포에 몇 분이 걸리고, 그동안 알람은 계속 ALARM 상태
- **동시 요청 제한**: 같은 `runtimeSessionId`로 동시 요청 불가. RCA는 매번 새 UUID 사용, Chatbot은 세션 ID 유지
- **Kiro CLI 버전**: kiro-agent-sdk가 테스트한 Kiro CLI 2.35.1을 이미지에 고정합니다. 다른 버전은 SDK 호환성을 확인한 뒤 변경하세요
- **CloudTrail 지연**: CloudTrail 이벤트는 발생 후 5~15분 늦게 조회될 수 있음. 에이전트는 파이프라인 실행 기록과 커밋을 먼저 확인하고, CloudTrail에 아직 보이지 않는 변경은 "확인 대기"로 표시하도록 steering 규칙에 정의
- **수정 PR 정확도**: AI가 생성하므로 100% 정확하지 않을 수 있음. 병합 전에 PR의 파일 변경을 반드시 확인 (`cdk diff` 댓글은 PR 코드가 만든 참고 자료)

## 정리

`IncidentToPrSampleAppStack`은 파이프라인이 배포하므로 먼저 지웁니다. 그다음 플랫폼과 GitOps 스택, 시크릿을 지웁니다.

```bash
aws cloudformation delete-stack --region ap-northeast-2 --stack-name IncidentToPrSampleAppStack
aws cloudformation wait stack-delete-complete --region ap-northeast-2 --stack-name IncidentToPrSampleAppStack
cd infra
AWS_REGION=ap-northeast-2 npx aws-cdk destroy IncidentToPrPlatformStack IncidentToPrGitOpsStack --force
cd ..
aws secretsmanager delete-secret --region ap-northeast-2 --secret-id incident-to-pr/web-password --force-delete-without-recovery
# API key 방식
aws secretsmanager delete-secret --region ap-northeast-2 --secret-id incident-to-pr/kiro-api-key --force-delete-without-recovery
# 웹 로그인 방식
aws secretsmanager delete-secret --region ap-northeast-2 --secret-id incident-to-pr/kiro-web-login --force-delete-without-recovery
aws secretsmanager delete-secret --region ap-northeast-2 --secret-id incident-to-pr/kiro-web-token --force-delete-without-recovery
```

`IncidentToPrGitOpsStack`을 지우면 워크로드 저장소와 커밋 기록도 삭제됩니다.

Lambda, CodeBuild, AgentCore Runtime의 로그 그룹은 스택을 지워도 남습니다. 필요 없으면 지웁니다.

```bash
for prefix in /aws/lambda/incident-to-pr- /aws/codebuild/incident-to-pr- /aws/bedrock-agentcore/runtimes/incident_to_pr_agent-; do
  for group in $(aws logs describe-log-groups --region ap-northeast-2 --log-group-name-prefix $prefix --query 'logGroups[].logGroupName' --output text); do
    aws logs delete-log-group --region ap-northeast-2 --log-group-name $group
  done
done
```

## 테스트

`tests/`는 웹 인증, 입력 검증, 수정 PR 도구와 리포트 상태, 지식 동기화, Slack 전송, 채팅 기억의 보안 통제를 AWS(moto)와 Kiro 없이 확인합니다.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r tests/requirements.txt ../kiro-agent-sdk   # ai-ml/kiro-agent-sdk
python -m pytest tests
```

## 라이선스

MIT-0 License
