# AgentCore 가이드

`kiro_agent_sdk.agentcore`는 Kiro 에이전트를 Amazon Bedrock AgentCore Runtime에서 실행하는 HTTP 어댑터입니다. AgentCore가 보내는 `/invocations` 요청을 받아 런타임 세션마다 Kiro 세션을 하나씩 운영하고, `/ping`으로 상태를 알립니다. 모델 호출과 도구 실행은 컨테이너 안의 Kiro CLI가 합니다.

## 구성 요소

| 구성 | 설명 |
| --- | --- |
| `Dockerfile.agentcore` | Kiro CLI 2.35.1(고정 SHA-256), SDK, AgentCore 의존성, 미리 준비한 V3 런타임 캐시를 담은 Linux 이미지. 사용자 10001로 실행하고 기본 명령은 `python -m kiro_agent_sdk.agentcore`(도구 없는 기본 앱. `session_authorizer`가 없으므로 `KIRO_AGENTCORE_ALLOW_UNAUTHORIZED_SESSIONS=true`를 줘야 시작합니다) |
| `create_agentcore_app()` | Starlette 앱을 만듭니다. 세션마다 Kiro 옵션을 만드는 `options_factory`, 호출자 인가 `session_authorizer` 등을 받습니다. |
| `AgentCoreConfig` | 어댑터 설정. `AgentCoreConfig.from_env()`는 환경 변수에서 읽습니다. |
| `scripts/build_agentcore_code_zip.py` | 컨테이너 대신 AgentCore 직접 코드 배포(CodeZip)용 ZIP을 만듭니다. Kiro CLI와 런타임 캐시를 포함합니다. |
| `examples/agentcore/` | 배포(`deploy.py`), 호출(`invoke.py`), 원격 상호작용(`interact.py`), 디바이스 로그인(`auth.py`), 백그라운드 작업(`background.py`), 비공개 호출 정책(`private_access.py`) 예제 |

Kiro 바이너리 사용에는 [Kiro 이용 조건](https://kiro.dev/downloads/)이 적용됩니다. 이미지에는 Kiro 로그인 정보나 API 키를 넣지 않습니다.

## 애플리케이션 만들기

```py
from kiro_agent_sdk import KiroAgentOptions
from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app


def options_for_session(workspace, resume):
    return KiroAgentOptions(agent="support", can_use_tool=policy)


app = create_agentcore_app(AgentCoreConfig.from_env(), options_factory=options_for_session)
```

- `options_factory(workspace, resume)`는 런타임 세션이 Kiro를 시작할 때 불립니다. `workspace`는 그 세션 전용 작업 디렉터리이고, `resume`은 재개할 Kiro 세션 ID(새 세션이면 `None`)입니다.
- 어댑터는 돌려받은 옵션의 `cwd`를 `workspace`로, `cli_path`, `agent_engine`, 시간 제한, 인증 환경을 설정 값으로 덮어씁니다. Kiro 프로젝트를 쓰려면 새 세션에서 프로젝트 파일을 `workspace`로 복사합니다. [examples/kiro_project/agentcore_app.py](../examples/kiro_project/agentcore_app.py)가 그렇게 합니다.
- Kiro 프로세스에는 컨테이너 환경 변수를 물려주지 않습니다(`child_env_mode="minimal"`). AWS 자격 증명 같은 값이 필요하면 옵션의 `env`로 명시합니다.
- 앱은 uvicorn 워커 하나로 실행합니다: `uvicorn app:app --host 0.0.0.0 --port 8080 --workers 1`

## 이미지와 CodeZip

```bash
# SDK 기본 이미지 (저장소 루트에서)
docker buildx build --platform linux/arm64 --load -f Dockerfile.agentcore -t kiro-agent-sdk:agentcore .

# 애플리케이션 이미지: 기본 이미지 위에 앱과 프로젝트를 더함
docker buildx build --platform linux/arm64 --load -t my-agent:latest examples/kiro_project
```

- AgentCore Runtime은 Linux ARM64 이미지를 실행합니다. 로컬 테스트는 현재 아키텍처로 빌드해도 됩니다.
- 빌드 인자 `KIRO_INSTALL_MCP=1`은 Python MCP SDK를 함께 설치합니다.
- 기본 이미지는 V3 런타임 캐시를 `/opt/kiro-prewarm`에 준비하고 `KIRO_AGENTCORE_RUNTIME_CACHE`로 알려 줍니다. 세션 시작 때 Kiro가 런타임을 내려받지 않습니다.

CodeZip은 AgentCore의 Python 런타임 위에서 실행되는 ZIP입니다. 빌더는 `Dockerfile.agentcore`로 만든 ARM64 이미지에서 Kiro CLI, 런타임 캐시, SDK를 꺼내 묶습니다. 직접 만든 앱은 `create_app(config)` 함수를 가진 파일로 `--app-source`에 줍니다.

```bash
python scripts/build_agentcore_code_zip.py --image kiro-agent-sdk:agentcore --app-source my_app.py --output dist/agent.zip
aws s3 cp dist/agent.zip s3://<bucket>/agent.zip
python examples/agentcore/deploy.py ... --code-s3-uri s3://<bucket>/agent.zip
```

## 설정

| 필드 | 환경 변수 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `auth_mode` | `KIRO_AGENTCORE_AUTH_MODE` | `api-key` | `api-key`, `cli`, `device` |
| `api_key` | `KIRO_API_KEY` | | Kiro API 키 |
| `secret_id`, `secret_json_key` | `KIRO_AGENTCORE_SECRET_ID`, `KIRO_AGENTCORE_SECRET_JSON_KEY` | | API 키를 담은 Secrets Manager 비밀. JSON 비밀이면 키 이름을 함께 지정 |
| `device_license`, `device_start_url`, `device_region`, `device_timeout` | `KIRO_AGENTCORE_DEVICE_*` | `free` | 디바이스 로그인 설정. `pro`는 IAM Identity Center 시작 URL과 리전 필요 |
| `state_dir` | `KIRO_AGENTCORE_STATE_DIR` | `/tmp/kiro-agentcore` | 세션별 HOME, 작업 디렉터리, 상태 파일 위치 |
| `cli_path` | `KIRO_AGENTCORE_CLI_PATH` | `kiro-cli` | Kiro CLI 경로 |
| `agent_engine` | `KIRO_AGENTCORE_ENGINE` | `v3` | Kiro 엔진 |
| `model` | `KIRO_AGENTCORE_MODEL` | | `options_factory`가 모델을 정하지 않았을 때 쓸 모델 |
| `runtime_cache_dir` | `KIRO_AGENTCORE_RUNTIME_CACHE` | | 미리 준비한 V3 런타임 |
| `startup_timeout`, `turn_timeout` | `KIRO_AGENTCORE_STARTUP_TIMEOUT`, `KIRO_AGENTCORE_TURN_TIMEOUT` | 120, 300 | 초 |
| `max_sessions` | `KIRO_AGENTCORE_MAX_SESSIONS` | 32 | 한 인스턴스가 유지할 세션 수 |
| `max_prompt_bytes`, `max_body_bytes` | `KIRO_AGENTCORE_MAX_PROMPT_BYTES`, `KIRO_AGENTCORE_MAX_BODY_BYTES` | 1 MiB, 2 MiB | 요청 크기 상한 |
| `child_env_mode` | `KIRO_AGENTCORE_CHILD_ENV` | `minimal` | `inherit`면 컨테이너 환경을 Kiro에 물려줌 |
| `allow_unauthorized_sessions` | `KIRO_AGENTCORE_ALLOW_UNAUTHORIZED_SESSIONS` | false | `session_authorizer` 없이 앱을 만들 수 있게 합니다. 런타임을 호출할 수 있는 모든 호출자가 모든 세션을 씁니다. |
| `session_authorization_timeout` | `KIRO_AGENTCORE_SESSION_AUTHORIZATION_TIMEOUT` | 10 | `session_authorizer` 제한 시간(초) |
| `allow_sdk_events` | `KIRO_AGENTCORE_ALLOW_SDK_EVENTS` | false | 스트리밍 응답에 SDK 메시지 이벤트 포함 허용 |
| `allow_interactions`, `interaction_timeout` | `KIRO_AGENTCORE_ALLOW_INTERACTIONS`, `KIRO_AGENTCORE_INTERACTION_TIMEOUT` | false, 120 | 권한 확인, 사용자 질문, MCP 정보 요청을 원격 호출자에게 넘김 |
| `allow_mcp_oauth_interactions` | `KIRO_AGENTCORE_ALLOW_MCP_OAUTH_INTERACTIONS` | false | MCP OAuth 인가를 원격 호출자에게 넘김 |
| `preauthorize_mcp_oauth`, `mcp_oauth_preauthorize_servers` | `KIRO_AGENTCORE_PREAUTHORIZE_MCP_OAUTH` | false | Kiro 시작 전에 MCP OAuth 인가 |
| `allow_command_introspection`, `allow_hook_introspection`, `allow_session_introspection`, `allow_active_introspection` | `KIRO_AGENTCORE_ALLOW_*_INTROSPECTION` | false | 조회 동작 허용 |
| `allow_session_history`, `allow_session_history_tools` | `KIRO_AGENTCORE_ALLOW_SESSION_HISTORY`, `KIRO_AGENTCORE_ALLOW_SESSION_HISTORY_TOOLS` | false | 세션 기록 조회 허용 |
| `allow_prompt_block_details` | `KIRO_AGENTCORE_ALLOW_PROMPT_BLOCK_DETAILS` | false | `promptSubmit` 훅이 막은 이유를 인가된 호출자에게 반환 |
| `reauthorize_output` | `KIRO_AGENTCORE_REAUTHORIZE_OUTPUT` | false | 응답 본문을 보내기 직전에 인가를 다시 확인 |
| `allow_background_task_*`, `background_*` | `KIRO_AGENTCORE_ALLOW_BACKGROUND_*` 등 | false | 백그라운드 작업 결과, 이벤트, 스트림, 재시도, 보관 |

`allow_` 설정은 모두 기본적으로 꺼져 있습니다. 대부분은 `session_authorizer`가 있어야 켤 수 있습니다.

## 인증

| `auth_mode` | 동작 |
| --- | --- |
| `api-key` | `KIRO_API_KEY` 또는 Secrets Manager 비밀에서 키를 읽어 세션마다 Kiro에 전달합니다. 무인 배포에 맞습니다. |
| `cli` | 컨테이너 사용자의 Kiro CLI 로그인을 씁니다. 로컬 테스트용입니다. |
| `device` | 세션마다 `auth_start` 동작으로 브라우저 디바이스 로그인을 시작합니다. 로그인 정보는 그 세션의 HOME에만 저장합니다. POSIX 전용 |

애플리케이션이 Kiro 로그인 토큰을 관리한다면 `create_agentcore_app(kiro_token_provider_factory=...)`에 세션별 토큰 공급자를 만드는 함수를 줍니다. 이 경우 `session_authorizer`가 필요합니다.

API 키는 실행 역할만 읽을 수 있는 비밀에 두고 주기적으로 교체하세요. 실행 역할은 AWS 리소스 접근용이며 Kiro 로그인을 대신하지 않습니다.

## HTTP API

AgentCore는 `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` 헤더에 런타임 세션 ID를 넣어 `/invocations`를 호출합니다. 같은 세션 ID의 요청은 같은 Kiro 세션으로 갑니다.

### 프롬프트

```json
{"prompt": "Summarize the incident", "stream": false}
```

| 필드 | 설명 |
| --- | --- |
| `prompt` | 문자열, 또는 텍스트, 이미지, `memory://` URI의 내장 리소스 블록 목록. 로컬 파일을 가리키는 리소스는 받지 않습니다. |
| `stream` | `true`면 `text/event-stream`으로 `text_delta`와 마지막 `result` 이벤트를 보냅니다. |
| `include_events` | 스트리밍에 SDK 메시지 이벤트 포함 (`allow_sdk_events` 필요). 이벤트는 `{"type": "sdk_event", "event_type": "AgentMessage", "message": {...}}` 형식이고 `event_type`은 메시지 타입 이름(`AgentMessage`, `ToolCall`, `KiroEvent` 등)입니다. |
| `include_prompt_block_details` | 프롬프트가 훅에 막혔을 때 `{"kind": "blocked", "reasons": [...], "warnings": [...]}` 반환 (`allow_prompt_block_details`와 인가 필요) |

응답:

```json
{"response": "...", "is_error": false, "stop_reason": "end_turn", "session_id": "<runtime session>", "kiro_session_id": "<kiro session>"}
```

### 동작

`{"action": "<이름>", ...}`으로 보냅니다.

| 동작 | 설명 | 조건 |
| --- | --- | --- |
| `prewarm` | 프롬프트 없이 Kiro 세션을 시작합니다. 이미 시작했으면 `already_warm: true` | |
| `interrupt`, `steer` | 진행 중인 턴을 멈추거나 지시를 끼워 넣습니다. | |
| `compact` | 유휴 V3 세션의 문맥 압축 | 인가 |
| `interactions`, `respond` | 대기 중인 권한 확인, 사용자 질문, MCP 정보 요청 조회와 응답 | `allow_interactions` |
| `server_info`, `context_usage`, `mcp_status`, `sdk_permission_mode` | 세션 상태 조회 | `allow_session_introspection` |
| `available_commands`, `list_kiro_hooks` | Kiro 명령과 훅 조회 | `allow_command_introspection`, `allow_hook_introspection` |
| `session_messages` | 세션 미러 기록 조회. `{"messages": [...], "next_offset": ...}`이고 각 메시지에 타입 이름(`UserMessage`, `AgentMessage`, `ToolCall`)이 `type`으로 들어 있습니다. | `allow_session_history` |
| `mcp_reconnect`, `mcp_revoke_oauth`, `mcp_cancel_tools` | 원격 MCP 서버 재연결, OAuth 토큰 폐기, 진행 중인 호출 취소 | 인가 |
| `code_status`, `code_init`, `code_overview` | Kiro 코드 인텔리전스 | 인가 |
| `auth_start`, `auth_status`, `auth_cancel`, `auth_logout` | 디바이스 로그인 | `auth_mode=device` |
| `background_task_*` | 백그라운드 작업 시작, 상태, 결과, 이벤트, 보관 | `background_tasks_factory`와 `allow_background_task_*` |

`/ping`은 처리 중인 요청이나 백그라운드 작업이 있으면 `HealthyBusy`, 없으면 `Healthy`를 돌려줍니다. AgentCore는 `HealthyBusy`인 세션을 정리하지 않습니다.

## 인가와 비공개 호출

- `session_authorizer(request, session_id)`는 요청마다 호출자를 확인하는 비동기 함수입니다. 신원 확인(예: JWT 검증)은 이 함수가 직접 합니다. 원격 상호작용, 조회, 백그라운드 작업 같은 기능은 이 함수가 있어야 켤 수 있습니다.
- `create_agentcore_app()`은 `session_authorizer`가 없으면 시작하지 않습니다. 인가 없이 실행하려면 `allow_unauthorized_sessions=True`(환경 변수 `KIRO_AGENTCORE_ALLOW_UNAUTHORIZED_SESSIONS=true`)를 명시합니다. 이때 어댑터는 런타임을 호출할 수 있는 모든 요청을 받고, 요청이 고른 세션 ID의 기존 세션을 그대로 쓰며, 시작할 때 경고 로그를 남깁니다. 런타임을 호출할 수 있는 사람이 한 명이거나 모두가 같은 대화를 봐도 되는 배포에만 씁니다.
- AgentCore의 IAM이나 JWT 인바운드 인증은 런타임을 호출할 수 있는지만 확인하고, 그 호출자가 이 세션의 주인인지는 확인하지 않습니다.
- `AgentCoreConfig`의 `auth_mode`는 Kiro에 접속하는 인증이고 HTTP 호출자의 인증이 아닙니다.

여러 사용자가 같은 런타임을 쓰면 세션 ID를 서버가 만들고 주인을 기록한 뒤, 요청마다 주인을 확인합니다. `SQLiteSessionOwnership`과 `DynamoDBSessionOwnership`은 등록하지 않은 세션을 거부합니다.

```py
import secrets

from kiro_agent_sdk import DynamoDBSessionOwnership

ownership = DynamoDBSessionOwnership("agent-session-owners")


async def start_session(user_id):
    # 세션을 만드는 신뢰할 수 있는 서버 경로(예: 로그인한 사용자의 API 요청)에서만 등록합니다.
    session_id = secrets.token_hex(24)
    if not await ownership.register(session_id, user_id):
        raise RuntimeError("Session ID collision")
    return session_id


async def authorize(request, session_id):
    user_id = await verified_user_id(request)  # JWT 서명, 발급자, 대상, 만료를 직접 검증
    return user_id is not None and await ownership.is_owner(session_id, user_id)


app = create_agentcore_app(AgentCoreConfig.from_env(), options_factory=options_for_session,
                           session_authorizer=authorize)
```

- `session_authorizer`가 처음 보는 세션을 요청한 사용자에게 등록하게 만들면, 남의 세션 ID를 먼저 쓴 사람이 주인이 됩니다. 등록은 세션을 만드는 경로에서만 합니다.
- 긴 응답 중에 권한을 회수해야 하면 `reauthorize_output=True`로 응답을 보내기 직전에 다시 확인합니다.
- `apply_private_access()`와 `AgentCorePrivateAccess`는 AgentCore 리소스 정책으로 특정 VPC 엔드포인트나 계정만 런타임을 호출하게 합니다. 적용 중 실패하면 `AgentCorePolicyApplyError`가 적용한 리소스와 확인하지 못한 리소스를 알려 줍니다.

## 세션 수명

- 런타임 세션마다 `state_dir` 아래에 HOME과 작업 디렉터리를 만들고 Kiro 세션 ID를 기록합니다. 같은 인스턴스로 돌아온 세션은 기록한 Kiro 세션을 재개합니다.
- AgentCore가 세션을 새 인스턴스로 옮기면 로컬 상태가 없습니다. 오래 보존해야 하면 `options_factory`에서 `session_store`와 `resume_from_store`를 쓰거나, AgentCore 세션 저장소를 `state_dir`로 마운트합니다(`deploy.py --session-storage`).
- 세션 수가 `max_sessions`에 닿으면 가장 오래 쓰지 않은 유휴 세션을 닫고 새 세션을 받습니다. 유휴 세션이 없으면 요청을 거부합니다.
- 첫 요청 지연을 줄이려면 세션을 만든 직후 `prewarm`을 보냅니다.

## 보안 경계

- 런타임 세션마다 HOME과 작업 디렉터리를 나누지만, 같은 컨테이너의 세션과 Kiro 프로세스는 같은 OS 계정으로 실행합니다. 셸이나 넓은 파일 읽기를 허용한 세션은 같은 컨테이너의 다른 세션 디렉터리와 프로세스 환경(`/proc/<pid>/environ`의 Kiro API 키 등)을 읽을 수 있습니다. 디렉터리 분리는 OS 격리가 아닙니다.
- AgentCore Runtime은 런타임 세션마다 별도의 microVM을 씁니다. 서로 믿을 수 없는 사용자는 AgentCore 세션을 나누어 받게 하고, 한 컨테이너에서 여러 사용자를 받는 자체 호스팅은 사용자마다 컨테이너나 VM을 나눕니다.
- `child_env_mode="minimal"`은 Kiro에 물려주는 환경 변수를 줄일 뿐 파일, 프로세스, 네트워크 접근을 막지 않습니다. `require_sandbox`는 Kiro 명령 샌드박스가 켜져 있는지 확인하는 설정이며 사용자 격리를 대신하지 않습니다. Python 도구와 콜백은 SDK 프로세스에서 실행합니다.
- 소유권 DB, 자격 증명, 비밀은 Kiro가 읽거나 쓸 수 있는 디렉터리 밖에 둡니다. `state_dir`는 서비스 계정만 쓸 수 있는 디렉터리(mode 0700)로 미리 만들어 둡니다.
- `max_sessions`는 메모리에 둔 세션 수만 제한합니다. 밀려난 세션의 디렉터리(로그인, 대화, 런타임 캐시 사본)는 재개를 위해 디스크에 남습니다. 오래 실행하는 자체 호스팅에서는 오래된 세션 디렉터리를 지우는 작업과 디스크 상한을 따로 두고, 호출자별 요청 수와 새 세션 수를 앞단에서 제한합니다. Kiro 사용량은 `KiroAgentOptions`의 `max_reported_credits`와 `reported_credit_ledger`(`SQLiteCreditLedger`, `DynamoDBCreditLedger`)로 제한할 수 있습니다.

## 운영 점검

- 로그에는 프롬프트와 응답 본문을 남기지 않습니다. 실패는 예외 종류만 기록합니다.
- 세션별 Kiro 프로세스는 연결을 끊거나 세션을 닫을 때 정리합니다. 컨테이너 종료 때 남은 세션도 정리합니다.
- 이미지의 Kiro CLI 버전을 바꾸면 V3 런타임 패치(`runtime-acp`, `runtime_compat_mode="v3"`)는 시작 전에 거부합니다. SDK가 테스트한 버전은 2.35.1입니다.
