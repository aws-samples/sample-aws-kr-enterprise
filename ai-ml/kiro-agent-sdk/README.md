# Kiro Agent SDK for Python

[Kiro CLI](https://kiro.dev/docs/cli/)를 에이전트 실행 엔진으로 쓰는 비동기 Python SDK입니다. Kiro 데스크톱(IDE 또는 CLI)에서 만든 custom agent, steering, 스킬, MCP 설정이 들어 있는 `.kiro/` 프로젝트를 그대로 Python 애플리케이션에서 실행하고, 일반 서버나 Amazon Bedrock AgentCore Runtime에 배포할 수 있습니다.

SDK는 Kiro CLI를 하위 프로세스로 띄우고 Agent Client Protocol(ACP)로 대화합니다. 에이전트 루프, 모델 호출, 도구 실행은 Kiro가 하고, 애플리케이션은 SDK로 프롬프트를 보내고 응답을 받으면서 도구 권한, 훅, 세션, 인증을 코드로 정합니다.

## 주요 기능

- **Kiro 프로젝트 실행**: 작업 디렉터리의 `.kiro/agents`, `.kiro/steering`, `.kiro/skills`, MCP 설정을 Kiro가 데스크톱에서와 같은 방식으로 불러옵니다. `agent="이름"`으로 custom agent를 고릅니다.
- **질의와 대화**: `query()`로 한 번 묻고 끝내거나, `KiroSDKClient`로 여러 턴을 이어 갑니다. 세션 ID로 재개, 분기, 삭제합니다.
- **스트리밍 메시지**: Kiro의 ACP 이벤트를 그대로 따르는 메시지 객체로 에이전트 텍스트, 생각 과정, 도구 호출(입력, 상태, 출력), 하위 에이전트 출력을 받습니다.
- **도구 권한**: Kiro가 권한을 물을 때마다 `can_use_tool` 콜백이 허용 또는 거부를 정합니다. Kiro 권한 규칙(`kiro_permissions`)과 권한 모드(`ask`, `denyUnlisted`, `trustAll`, `plan`)를 함께 씁니다.
- **Python 도구**: `@tool`과 `create_mcp_server()`로 Python 함수를 MCP 도구로 제공합니다. stdio, HTTP MCP 서버와 MCP OAuth도 지원합니다.
- **Kiro 훅**: `promptSubmit`, `preToolUse`, `postToolUse`, `agentStop` 트리거를 Python 함수로 처리합니다. Kiro에 직접 등록하는 네이티브 훅으로는 `sessionStart`, spec 작업 트리거(`preTaskExecution`, `postTaskExecution`), 파일 트리거(`fileCreated`, `fileEdited`, `fileDeleted`)도 받습니다. 콜백은 Kiro 훅처럼 문맥 추가(`context`), 막기(`block`), 확인 요청(`ask`), 작업 계속(`continue`)을 돌려줍니다.
- **에이전트 정의**: 코드에서 `AgentDefinition`으로 Kiro agent 프로필을 만들어 하위 에이전트로 씁니다.
- **구조화 출력**: JSON Schema로 응답 형식을 정하고 검증합니다.
- **세션 저장**: 대화 기록을 SQLite, DynamoDB 또는 직접 만든 저장소에 미러링하고 다른 호스트에서 복구합니다.
- **사용량 상한**: Kiro가 보고한 크레딧으로 세션이나 팀 단위 상한을 겁니다.
- **백그라운드 작업**: 긴 작업을 별도 Kiro 세션으로 실행하고 상태, 이벤트, 결과를 조회합니다.
- **AgentCore 배포**: Kiro CLI를 포함한 ARM64 컨테이너 이미지와 CodeZip, AgentCore `/invocations` HTTP 서버, 세션 사전 기동(`prewarm`), 세션 소유권 검사를 제공합니다.
- **Kiro 인증**: CLI 로그인, Kiro API 키, 애플리케이션이 관리하는 로그인 토큰 중에서 고릅니다.

## 요구 사항

- Python 3.9 이상 (MCP 서버 인스턴스 기능은 3.10 이상)
- [Kiro CLI](https://kiro.dev/docs/cli/installation/) 2.35.1. 이 SDK는 이 버전으로 테스트했습니다. `native_hook_bridge_mode="runtime-acp"`와 `runtime_compat_mode="v3"`는 Kiro CLI 2.35.1의 V3 런타임 번들에만 적용되고, 다른 번들에서는 시작 전에 거부합니다.
- Kiro 인증: `kiro-cli login`, 또는 API 키를 지원하는 Kiro 구독의 `KIRO_API_KEY` ([Kiro 인증](https://kiro.dev/docs/getting-started/authentication/))

모델 요청은 Kiro 구독의 크레딧을 사용합니다.

### 지원 운영체제

| 환경 | 상태 |
| --- | --- |
| Linux x86_64, ARM64 | 테스트함. AgentCore 이미지도 Linux ARM64입니다. |
| macOS | Kiro CLI는 지원합니다. SDK는 순수 Python이라 동작할 것으로 보지만 이 저장소에서 테스트하지 않았습니다. |
| Windows 11 | Kiro CLI는 PowerShell 설치를 지원합니다. SDK는 테스트하지 않았고, 아래 POSIX 전용 기능은 쓸 수 없습니다. |

POSIX(Linux, macOS)에서만 쓰는 기능: 네이티브 Kiro 훅 브리지(`native_hooks`, `native_hook_bridge_mode`), 네이티브 SDK 훅 모드(`sdk_pretool_hook_mode="native"`, `sdk_stop_hook_mode="native"`), 다른 계정으로 Kiro 실행(`user`), AgentCore 디바이스 로그인. 나머지 기능은 운영체제에 종속되지 않습니다.

## 설치

PyPI 패키지가 아니므로 소스에서 설치합니다. SDK는 [sample-aws-kr-enterprise](https://github.com/aws-samples/sample-aws-kr-enterprise) 저장소의 `ai-ml/kiro-agent-sdk`에 있습니다.

```bash
git clone https://github.com/aws-samples/sample-aws-kr-enterprise.git
cd sample-aws-kr-enterprise/ai-ml/kiro-agent-sdk
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
kiro-cli login
```

Windows PowerShell에서는 `.venv\Scripts\Activate.ps1`로 가상 환경을 켭니다.

선택 기능은 extra로 설치합니다.

| extra | 용도 |
| --- | --- |
| `mcp` | Python MCP SDK 서버 인스턴스를 SDK MCP 서버로 연결 (Python 3.10 이상) |
| `agentcore` | AgentCore HTTP 서버(`starlette`, `uvicorn`)와 AWS 연동(`boto3`) |

```bash
python -m pip install '.[agentcore]'
```

## 빠른 시작

한 번 묻고 결과를 받습니다.

```python
import asyncio

from kiro_agent_sdk import KiroAgentOptions, ResultMessage, query


async def main():
    async for message in query(prompt="Say hello in one sentence.", options=KiroAgentOptions(tools=[])):
        if isinstance(message, ResultMessage):
            print(message.result)


asyncio.run(main())
```

같은 대화를 여러 턴 이어 갑니다.

```python
import asyncio

from kiro_agent_sdk import AgentMessage, KiroAgentOptions, KiroSDKClient, TextBlock


async def main():
    async with KiroSDKClient(KiroAgentOptions(tools=[])) as client:
        for prompt in ("Pick a number between 1 and 10.", "Double it."):
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AgentMessage):
                    print("".join(block.text for block in message.content if isinstance(block, TextBlock)))


asyncio.run(main())
```

`tools=[]`는 도구 없이 대화만 하는 설정입니다. 도구를 쓰려면 아래처럼 Kiro 프로젝트의 agent를 고르거나 `tools`에 Kiro 도구 이름을 넣고, 권한 콜백을 붙입니다.

## 개발에서 배포까지

이 SDK의 기본 흐름은 "Kiro에서 만들고 테스트한 프로젝트 폴더를 애플리케이션이 그대로 실행한다"입니다. 아래는 [examples/kiro_project](examples/kiro_project)의 릴리스 노트 작성 에이전트로 네 단계를 따라갑니다.

```
examples/kiro_project/
  project/                      Kiro로 여는 프로젝트 폴더
    .kiro/agents/release-writer.md
    .kiro/steering/writing-style.md
    .kiro/skills/release-notes/SKILL.md
    changes/2026-11.md
  run.py                        일반 호스트에서 실행하는 애플리케이션
  agentcore_app.py              AgentCore에서 실행하는 애플리케이션
  Dockerfile                    AgentCore 이미지
```

### 1. 개발: Kiro에서 에이전트 만들기

`project/` 폴더를 Kiro IDE로 열거나 터미널에서 Kiro CLI를 실행해 에이전트를 만듭니다. 이 단계에는 SDK가 필요 없습니다.

- `.kiro/agents/*.md`: custom agent. frontmatter에 `name`, `description`, `tools`, `resources`, `mcpServers`, `model` 등을 적고 본문에 지시를 씁니다.
- `.kiro/steering/*.md`: 프로젝트 전체에 적용할 규칙. `inclusion: always`면 모든 대화에 들어갑니다.
- `.kiro/skills/<이름>/SKILL.md`: 필요할 때 불러 쓰는 작업 절차.
- MCP 서버와 훅도 Kiro 설정 파일로 정의합니다.

채팅으로 바로 테스트합니다.

```bash
cd examples/kiro_project/project
kiro-cli chat --agent release-writer
```

### 2. 테스트: 같은 프로젝트를 SDK로 실행하기

애플리케이션은 `cwd`로 프로젝트 폴더를, `agent`로 custom agent를 고릅니다. Kiro가 프로젝트의 `.kiro/`를 직접 읽으므로 1단계에서 고친 내용이 그대로 반영됩니다. 애플리케이션이 덧붙이는 것은 권한 정책 같은 실행 규칙입니다.

```py
import asyncio
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, PermissionResultAllow, PermissionResultDeny, ResultMessage

PROJECT = Path("examples/kiro_project/project")


async def read_only(name, data, context):
    # Kiro reports the file in the consent request; the tool input itself can be unavailable.
    consent = context.kiro_consent or {}
    resource = consent.get("resource")
    if consent.get("capability") == "fs_read" and isinstance(resource, str):
        root = PROJECT.resolve()
        path = (Path(consent.get("workspaceRoot") or root) / resource).resolve()
        if path == root or root in path.parents:
            return PermissionResultAllow()
    return PermissionResultDeny(message="Only reads of project files are allowed")


async def main():
    options = KiroAgentOptions(cwd=PROJECT, agent_engine="v3", agent="release-writer", can_use_tool=read_only)
    async with KiroSDKClient(options) as client:
        await client.query("Write the release notes for 2026-11 from changes/2026-11.md.")
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                print(message.result)


asyncio.run(main())
```

```bash
python examples/kiro_project/run.py
```

자동 테스트는 두 층으로 나눕니다.

- **단위 테스트**: 저장소의 `tests/fake_kiro.py`는 ACP를 흉내 내는 가짜 Kiro CLI입니다. `cli_path`를 이 파일로 두면 크레딧 없이 권한 콜백, 훅, 세션 처리를 테스트할 수 있습니다. SDK 자체 테스트는 `scripts/check.sh`로 실행합니다.
- **통합 테스트**: 실제 Kiro CLI와 로그인으로 에이전트 출력까지 확인합니다. 이 저장소의 실제 Kiro 테스트는 `KIRO_SDK_INTEGRATION=1`을 설정했을 때만 실행됩니다.

### 3. 패키징

배포 묶음은 프로젝트 폴더, 애플리케이션 코드, SDK 세 가지입니다. Kiro CLI와 Kiro 인증은 실행 환경이 준비합니다.

#### 일반 OS 호스트 (Linux, macOS, Windows)

애플리케이션을 평범한 Python 프로그램으로 묶습니다.

1. 프로젝트 폴더(`project/`)와 애플리케이션(`run.py`)을 함께 둡니다. 애플리케이션은 프로젝트를 실행 파일 기준 상대 경로로 찾게 합니다. 예제의 `run.py`가 그렇게 합니다.
2. SDK를 wheel로 만들어 애플리케이션 의존성에 넣습니다.

   ```bash
   python -m pip wheel . --no-deps -w dist
   ```

3. 대상 호스트에 Kiro CLI 2.35.1을 설치하고, 애플리케이션을 실행할 계정으로 인증을 준비합니다. 사람이 쓰는 호스트는 `kiro-cli login`, 무인 서버는 `KIRO_API_KEY` 환경 변수가 맞습니다.

#### AgentCore

AgentCore Runtime은 컨테이너 이미지(Linux ARM64)를 실행합니다. 이미지를 두 겹으로 만듭니다.

1. SDK 기본 이미지: 저장소 루트의 `Dockerfile.agentcore`가 Kiro CLI 2.35.1을 고정 SHA-256으로 내려받아 설치하고, SDK와 AgentCore 의존성을 넣고, Kiro V3 런타임을 미리 준비합니다. 로그인 정보는 이미지에 넣지 않습니다.

   ```bash
   docker buildx build --platform linux/arm64 --load -f Dockerfile.agentcore -t kiro-agent-sdk:agentcore .
   ```

2. 애플리케이션 이미지: 예제의 `Dockerfile`이 기본 이미지 위에 프로젝트 폴더와 `agentcore_app.py`를 더합니다.

   ```bash
   cd examples/kiro_project
   docker buildx build --platform linux/arm64 --load -t release-writer:latest .
   ```

`agentcore_app.py`는 AgentCore HTTP 서버를 만들고, 세션마다 Kiro 옵션을 돌려주는 `options_factory`를 등록합니다. AgentCore 어댑터는 세션마다 별도의 작업 디렉터리를 주므로, 새 세션이 시작될 때 프로젝트 파일을 그 디렉터리로 복사합니다.

```py
import shutil
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions, PermissionResultAllow, PermissionResultDeny
from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app

PROJECT = Path(__file__).resolve().parent / "project"


def read_only_in(workspace):
    root = Path(workspace).resolve()

    async def read_only(name, data, context):
        # Kiro reports the file in the consent request; the tool input itself can be unavailable.
        consent = context.kiro_consent or {}
        resource = consent.get("resource")
        if consent.get("capability") == "fs_read" and isinstance(resource, str):
            path = (Path(consent.get("workspaceRoot") or root) / resource).resolve()
            if path == root or root in path.parents:
                return PermissionResultAllow()
        return PermissionResultDeny(message="This deployment only allows reading files in the project")

    return read_only


def options_for_session(workspace, resume):
    if resume is None:
        shutil.copytree(PROJECT, workspace, dirs_exist_ok=True)
    return KiroAgentOptions(agent="release-writer", can_use_tool=read_only_in(workspace))


app = create_agentcore_app(AgentCoreConfig.from_env(), options_factory=options_for_session)
```

컨테이너 없이 소스 ZIP으로 배포하는 CodeZip 빌더(`scripts/build_agentcore_code_zip.py`)도 있습니다. 자세한 내용은 [AgentCore 가이드](docs/agentcore.md)에 있습니다.

### 4. 배포

#### 일반 OS 호스트

애플리케이션을 그 OS의 서비스 관리자로 실행합니다. Linux systemd 예시입니다.

```ini
# /etc/systemd/system/release-writer.service
[Unit]
Description=Release notes agent
After=network-online.target

[Service]
User=release-writer
WorkingDirectory=/opt/release-writer
EnvironmentFile=/etc/release-writer/env
ExecStart=/opt/release-writer/.venv/bin/python run.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

- `EnvironmentFile`에 `KIRO_API_KEY`를 두고 파일 권한을 서비스 계정만 읽게 제한합니다.
- `kiro-cli`가 서비스 계정의 `PATH`에 없으면 `KiroAgentOptions(cli_path=...)`로 경로를 지정합니다.
- macOS는 launchd, Windows는 작업 스케줄러나 서비스 래퍼로 같은 구성을 만듭니다.

#### AgentCore

1. 이미지를 Amazon ECR에 올립니다.

   ```bash
   aws ecr create-repository --repository-name release-writer
   docker tag release-writer:latest <account>.dkr.ecr.<region>.amazonaws.com/release-writer:latest
   docker push <account>.dkr.ecr.<region>.amazonaws.com/release-writer:latest
   ```

2. Kiro API 키를 AWS Secrets Manager에 저장하고, AgentCore 실행 역할에 그 비밀을 읽는 권한을 줍니다.

   ```bash
   aws secretsmanager create-secret --name release-writer/kiro-api-key --secret-string file://kiro-api-key.txt
   ```

3. AgentCore Runtime을 만듭니다. 환경 변수 `KIRO_AGENTCORE_SECRET_ID`에 비밀 이름을 넣으면 어댑터가 시작할 때 키를 읽습니다. `examples/agentcore/deploy.py`가 요청을 만들어 줍니다.

   ```bash
   python examples/agentcore/deploy.py --region <region> --name release_writer \
     --image-uri <account>.dkr.ecr.<region>.amazonaws.com/release-writer:latest \
     --role-arn arn:aws:iam::<account>:role/<agentcore-execution-role> \
     --secret-id release-writer/kiro-api-key --allow-unauthorized-sessions
   ```

   예제 앱은 호출자 인가(`session_authorizer`)가 없습니다. SDK는 인가 없는 앱을 기본으로 거부하므로, 런타임을 호출할 수 있는 사람이 혼자인 테스트 배포에서만 `--allow-unauthorized-sessions`로 명시합니다. 여러 사용자가 쓰면 [AgentCore 가이드](docs/agentcore.md#인가와-비공개-호출)처럼 세션 주인을 확인하는 `session_authorizer`를 붙이고 이 옵션을 뺍니다.

4. 호출합니다. 요청 본문은 `{"prompt": "..."}`이고 응답은 `{"response": "...", "is_error": false, ...}`입니다.

   ```bash
   python examples/agentcore/invoke.py --region <region> --runtime-arn <runtime-arn> \
     --session-id <33자 이상 세션 ID> "Write the release notes for 2026-11."
   ```

첫 요청의 Kiro 기동 시간을 줄이려면 세션을 만든 직후 `{"action": "prewarm"}`을 보냅니다(`invoke.py --prewarm`).

## Kiro 인증

| 방식 | 설정 | 맞는 곳 |
| --- | --- | --- |
| CLI 로그인 | `kiro-cli login`, 기본값 | 개발자 PC, 사람이 관리하는 서버 |
| API 키 | `KIRO_API_KEY` 환경 변수, AgentCore는 `KIRO_AGENTCORE_SECRET_ID` | 무인 서버, AgentCore. 관리형 구독은 관리자가 API 키를 허용해야 합니다. |
| 애플리케이션 토큰 | `kiro_auth_method="client"`, `kiro_access_token_provider` | 애플리케이션이 Kiro 로그인 토큰을 직접 관리할 때 |
| AgentCore 디바이스 로그인 | `KIRO_AGENTCORE_AUTH_MODE=device` | 세션마다 사람이 브라우저로 로그인할 때 |

- API 키는 오래 유효한 비밀입니다. Secrets Manager 같은 비밀 저장소에 두고, 실행 역할만 읽게 하고, 주기적으로 교체합니다.
- 애플리케이션 토큰 방식에서 SDK는 Kiro가 토큰을 요청할 때마다 `kiro_access_token_provider()`를 호출합니다. 반환값은 `{"accessToken": ..., "expiresAt": ..., "profileArn": ...}`입니다. 로그인 토큰은 수명이 짧으므로 갱신 토큰으로 미리 새 토큰을 받아 두는 작업을 애플리케이션이 따로 돌려야 합니다.

## 사용 조건과 제약

이 SDK는 Kiro CLI를 하위 프로세스로 실행하고 ACP로 대화합니다. 모델 호출과 도구 실행 요청은 모두 Kiro CLI가 보냅니다. Kiro의 이용 조건과 조직의 정책은 SDK를 통해 쓸 때도 그대로 적용됩니다.

### Kiro 이용 조건

- Kiro CLI는 AWS Customer Agreement에 따른 AWS Content로 제공됩니다([Kiro 라이선스](https://kiro.dev/license/)).
- Kiro를 이용한 자동화는 소프트웨어 개발 분야로 한정됩니다(예: CI/CD 중 코드 리뷰). Kiro 사용에 대한 자세한 내용은 [Kiro 요금 FAQ](https://kiro.dev/pricing/)를 참고하세요.

### 권장 패턴: 변경은 저장소로, 반영은 배포 파이프라인으로

운영 중인 소프트웨어의 문제를 다루는 에이전트는 운영 환경을 직접 바꾸지 않고, 수정안을 코드 변경으로 제안하는 구조를 권장합니다.

1. 에이전트는 로그, 메트릭, 변경 기록을 읽기 전용으로 조회해 원인을 분석합니다.
2. 에이전트가 애플리케이션 코드나 IaC(인프라 코드)를 고쳐 풀 리퀘스트를 엽니다.
3. CI 검사와 사람의 리뷰를 거쳐 병합되면, 기존 배포 파이프라인이 운영 환경에 반영합니다.

이 구조에서는 에이전트의 실행 역할에 운영 환경 쓰기 권한이 필요 없고, 모든 변경이 리뷰와 감사 기록을 거칩니다. SDK의 `can_use_tool`과 `kiro_permissions`로 에이전트가 쓸 수 있는 도구를 저장소 작업으로 제한하세요.

### 인증

- 무인 자동화에는 Kiro API 키가 권장 방식입니다. 관리자가 관리하는 구독에서는 API 키 생성이 기본으로 꺼져 있으며, 관리자가 허용해야 합니다([API 키 거버넌스](https://kiro.dev/docs/enterprise/governance/api-keys/)).
- CLI 로그인과 애플리케이션 토큰(`kiro_auth_method="client"`)은 사람의 로그인 세션을 씁니다. 세션이 끝나면 다시 로그인해야 하고, 요청은 로그인한 사람의 계정으로 기록됩니다. 사람의 로그인 세션을 무인 서비스에 쓰는 것은 조직의 인증 정책에 어긋날 수 있으므로, 관리자의 승인 없이 API 키 정책을 대신하는 용도로 쓰지 마세요.
- 모든 요청은 인증한 Kiro 계정의 권한, 거버넌스 규칙, 크레딧으로 처리됩니다. 서비스용으로는 개인 계정이 아닌 운영 전용 계정을 쓰세요.

### 버전과 내부 인터페이스

- 이 SDK는 Kiro CLI 2.35.1로 테스트했습니다. 다른 버전에서는 동작이 달라질 수 있습니다.
- `native_hook_bridge_mode="runtime-acp"`와 `runtime_compat_mode="v3"`는 Kiro CLI 2.35.1의 V3 런타임 번들을 SDK가 패치해서 동작합니다. 번들 해시가 다르면 시작 전에 거부합니다.
- 일부 기능은 공개 ACP 메서드가 아닌 Kiro 내부 확장 메서드(`_kiro/...`)를 씁니다. 애플리케이션 토큰 전달, 세션 이름 변경, 메모리, 워크플로 등이 그렇습니다. 이런 메서드는 Kiro 업데이트로 예고 없이 바뀔 수 있으므로, Kiro CLI 버전을 고정하고 업그레이드 전에 다시 테스트하세요.

### 지원 범위

이 SDK는 AWS 샘플 코드이며 Kiro 팀이 제공하는 공식 SDK가 아닙니다. Kiro CLI의 동작, 요금, 이용 조건에 대한 공식 안내는 [Kiro 문서](https://kiro.dev/docs/)를 따릅니다.

## 문서

- [사용 가이드](docs/guide.md): 옵션, 메시지, 권한, 훅, MCP, 세션, 저장소, 오류 처리
- [AgentCore 가이드](docs/agentcore.md): 이미지와 CodeZip, 설정, HTTP API, 인증, 운영
- [API 참조](docs/api-reference.md): 공개 함수와 클래스의 시그니처

## 테스트

```bash
python -m pip install '.[agentcore,mcp]'   # Python 3.9에서는 '.[agentcore]'
scripts/check.sh            # 가짜 Kiro CLI로 전체 단위 테스트
scripts/check-package.sh    # wheel을 설치한 환경에서 같은 테스트와 예제 실행
KIRO_SDK_INTEGRATION=1 python -m unittest discover -s tests -p test_live_kiro.py   # 실제 Kiro CLI, 크레딧 사용
```

## 라이선스

MIT-0. [LICENSE](LICENSE)를 확인하세요. Kiro CLI 사용에는 [Kiro 라이선스](https://kiro.dev/license/)와 [Kiro 이용 조건](#kiro-이용-조건)이 적용됩니다.
