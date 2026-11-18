# 사용 가이드

Kiro Agent SDK의 기능과 옵션을 설명합니다. 함수와 클래스의 정확한 시그니처는 [API 참조](api-reference.md), AgentCore 배포는 [AgentCore 가이드](agentcore.md)를 보세요.

## 동작 방식

`KiroSDKClient`는 연결할 때 Kiro CLI(`kiro-cli acp`)를 하위 프로세스로 실행하고 표준 입출력으로 Agent Client Protocol(ACP) 메시지를 주고받습니다.

- 프롬프트를 보내면 Kiro가 모델 호출, 도구 실행, 하위 에이전트 위임을 합니다.
- Kiro가 권한을 물으면 SDK가 권한 모드, 훅, `can_use_tool` 콜백으로 답합니다.
- Kiro가 보내는 ACP 진행 이벤트(`session/update`)는 SDK가 메시지 객체(`AgentMessage`, `ToolCall`, `ResultMessage` 등)로 묶어 돌려줍니다.
- SDK가 연결에 필요한 임시 agent 프로필을 쓰는 경우가 있습니다. `agents`, `system_prompt`, `tools`, `kiro_permissions`처럼 코드로 agent 설정을 줄 때입니다. 이 프로필은 연결을 끊을 때 지웁니다.

Kiro CLI는 V2와 V3 두 엔진을 가집니다. V3는 Kiro IDE와 같은 엔진이며 steering, 스킬, 훅, 권한 규칙, 체크포인트 같은 기능 대부분이 V3에서만 동작합니다. 새 애플리케이션은 `agent_engine="v3"`를 지정하세요.

## 기본 사용

### 한 번 묻기: `query()`

```py
async for message in query(prompt="Summarize README.md", options=KiroAgentOptions(cwd="project")):
    ...
```

`query()`는 연결, 프롬프트 전송, 응답 수신, 연결 종료를 한 번에 합니다. 마지막 메시지는 `ResultMessage`입니다.

### 대화: `KiroSDKClient`

```py
async with KiroSDKClient(options) as client:
    await client.query("First question")
    async for message in client.receive_response():
        ...
    await client.query("Follow-up question")
    async for message in client.receive_response():
        ...
```

| 메서드 | 설명 |
| --- | --- |
| `connect(prompt=None)` | Kiro를 시작하고 세션을 만들거나 재개합니다. `async with`가 대신 호출합니다. |
| `query(prompt)` | 프롬프트를 보냅니다. 응답은 `receive_response()`로 받습니다. |
| `receive_response()` | 현재 턴의 메시지를 `ResultMessage`까지 돌려줍니다. |
| `receive_messages()` | 입력 스트림의 모든 턴을 차례로 돌려줍니다. `receive_messages_mode="continuous"`면 다음 `query()`도 계속 기다립니다. |
| `interrupt()` | 진행 중인 턴을 멈춥니다. |
| `steer(message)` | 진행 중인 V3 턴에 지시를 끼워 넣습니다. |
| `set_model(model)`, `set_mode(mode)`, `set_permission_mode(mode)` | 연결된 세션의 모델, Kiro 모드, SDK 권한 모드를 바꿉니다. |
| `disconnect()` | Kiro 프로세스와 SDK가 만든 브리지를 정리합니다. |

### 스트리밍 입력

`query()`와 `connect()`에 비동기 반복자를 주면 항목마다 프롬프트 하나를 차례로 보냅니다. 각 항목은 문자열, ACP 콘텐츠 블록 목록, 또는 `VerbatimPrompt`입니다.

| `stream_input_mode` | 동작 |
| --- | --- |
| `"on-receive"` (기본) | 응답을 받을 때마다 다음 입력을 읽습니다. |
| `"buffered"` | 입력 전체를 먼저 복사한 뒤 첫 턴을 보냅니다. 유한한 입력에 씁니다. |
| `"prefetch"` | 제한된 버퍼로 입력을 백그라운드에서 미리 읽습니다. |
| `"prefetch-first"` | 첫 입력만 먼저 보내고 나머지는 백그라운드에서 읽습니다. |

버퍼 한도는 `stream_input_buffer_max_messages`, `stream_input_buffer_max_bytes`, `stream_input_buffer_timeout`으로 정합니다.

### 프롬프트 콘텐츠

프롬프트는 문자열이나 ACP 콘텐츠 블록 목록입니다.

| 블록 | 형식 | 조건 |
| --- | --- | --- |
| 텍스트 | `{"type": "text", "text": "..."}` | 항상 |
| 이미지 | `{"type": "image", "mimeType": "image/png", "data": "<base64>"}` | Kiro가 이미지 입력을 지원한다고 알렸을 때 |
| 내장 리소스 | `{"type": "resource", "resource": {"uri": "memory://notes.txt", "mimeType": "text/plain", "text": "..."}}`, 바이너리는 `"blob": "<base64>"` | Kiro가 embedded context를 지원한다고 알렸을 때 |

지원하지 않는 블록은 프롬프트를 보내기 전에 `ValueError` 또는 `ProtocolError`로 거부합니다.

`VerbatimPrompt(content)`로 감싼 프롬프트는 Kiro가 슬래시 명령이나 `@` 파일 참조로 해석하지 않고 쓴 그대로 받습니다. `runtime_compat_mode="v3"`가 필요하며, `verbatim_prompts=True`는 모든 프롬프트에 같은 처리를 합니다.

## 메시지

응답 메시지는 Kiro의 ACP 이벤트를 따릅니다. `receive_response()`는 다음 객체를 차례로 돌려주고, 세션 기록 함수(`get_session_messages()` 등)도 같은 `UserMessage`, `AgentMessage`, `ToolCall`을 돌려줍니다.

| 타입 | 내용 |
| --- | --- |
| `AgentMessage` | 에이전트 출력. `agent_message_chunk`와 `agent_thought_chunk`를 메시지 ID별로 이어 붙인 것입니다. `content`는 `TextBlock`, `ThoughtBlock`, 그 밖의 ACP 블록(사전)입니다. |
| `ToolCall` | Kiro 도구 호출. ACP `tool_call`과 이후 `tool_call_update`를 합친 상태로, 입력이 알려졌을 때와 끝났을 때 보냅니다. `tool_call_id`, `title`, `status`(`pending`, `in_progress`, `completed`, `failed`), `kind`, `raw_input`, `raw_output`, `content`, `locations`, `kiro_metadata` |
| `UserMessage` | 사용자 프롬프트. 세션 기록에서 읽을 때 나옵니다. |
| `StreamEvent` | `include_partial_messages=True`일 때 부분 출력 이벤트(원래 ACP 이벤트) |
| `KiroEvent` | 그 밖의 ACP 이벤트(`kind`는 `sessionUpdate` 이름, `data`는 원래 이벤트)와 SDK 알림 |
| `HookEventMessage` | 훅 경고(`KiroEvent`의 하위 타입, `kind="hook_warning"`) |
| `MirrorErrorMessage` | `session_store_error_policy="continue"`에서 저장 실패 알림 |
| `ResultMessage` | 턴의 끝. `stop_reason`(ACP 값: `end_turn`, `max_tokens`, `max_turn_requests`, `refusal`, `cancelled`), `is_error`, `result`(최종 텍스트), `structured_output`, `permission_denials`, `kiro_usage`, `kiro_model_usage`, `model_turns`, `duration_ms`, `user_message_id` |

- 생각 과정은 `include_thoughts=True`일 때 `ThoughtBlock`으로 받습니다.
- 하위 에이전트의 메시지와 도구 호출에는 `agent_id`(Kiro 하위 에이전트 ID)와 `parent_tool_call_id`(그 하위 에이전트를 시작한 도구 호출)가 있습니다. 하위 에이전트가 낸 텍스트는 기본적으로 받지 않고, `forward_subagent_text=True`면 별도 `AgentMessage`로 받습니다.

## Kiro 프로젝트와 에이전트

| 옵션 | 설명 |
| --- | --- |
| `cwd` | Kiro 작업 디렉터리. 이 디렉터리의 `.kiro/`를 Kiro가 읽습니다. |
| `add_dirs` | V3에 추가로 알릴 작업 디렉터리 |
| `agent` | 사용할 Kiro custom agent 이름 |
| `agents` | 코드로 정의한 `AgentDefinition` 사전. Kiro 홈의 `agents/`에 임시 프로필로 쓰고 연결을 끊을 때 지웁니다. |
| `system_prompt` | 임시 프로필의 지시 문자열 |
| `tools` | 임시 프로필의 Kiro 도구 선택자 목록. `[]`는 도구 없음 |
| `kiro_resources` | 프로필에 넣을 `file://`, `skill://` 리소스 |
| `model`, `effort`, `thinking` | Kiro 모델 ID, 추론 노력 수준, 생각 켜기/끄기(`True`, `False`, `None`) |
| `fallback_model`, `fallback_model_mode` | `"startup"`은 새 세션에서 주 모델을 쓸 수 없을 때 대체 모델 선택, `"runtime"`은 테스트된 V3 런타임의 모델 요청 대체 |
| `max_turns` | 주 에이전트의 모델 반복 상한. `runtime_compat_mode="v3"` 필요 |

### `AgentDefinition`

| 필드 | 설명 |
| --- | --- |
| `description`, `prompt` | agent 설명과 지시 |
| `tools`, `disallowed_tools` | 허용, 제외할 Kiro 도구 선택자 |
| `model`, `effort` | 이 agent의 모델과 추론 노력. `model="inherit"`는 주 세션 모델을 따릅니다. |
| `skills` | `.kiro/skills` 또는 Kiro 홈 `skills/`에서 미리 불러올 스킬 이름 |
| `mcp_servers` | 이 agent에 붙일 MCP 서버 이름이나 인라인 정의 |
| `initial_prompt` | 새 세션이 시작될 때 이 agent로 먼저 보낼 프롬프트 |
| `max_turns` | 이 agent의 모델 반복 상한. `runtime_compat_mode="v3"` 필요 |
| `kiro_resources`, `kiro_permissions` | 프로필 리소스와 Kiro 권한 규칙 |
| `tools_mode`, `tool_selector_map` | `"selectors"`면 도구 이름을 Kiro 선택자로 바꿔 씁니다. |
| `disallowed_tools_mode`, `disallowed_tool_permission_map` | `"v3-permissions"`면 제외 도구를 V3 거부 규칙으로 바꿔 씁니다. |

## 권한

Kiro는 권한이 필요한 도구 호출마다 SDK에 묻습니다. 묻는 대상은 Kiro 규칙이 결정합니다. Kiro 권한 규칙이 이미 허용한 호출은 SDK까지 오지 않습니다.

### 권한 모드

| `permission_mode` | 동작 |
| --- | --- |
| `"ask"` (기본) | 훅을 거친 뒤 `can_use_tool`에 묻습니다. 콜백이 없으면 거부합니다. |
| `"denyUnlisted"` | `can_use_tool`을 부르지 않고, 훅이 허용하지 않은 요청을 거부합니다. |
| `"trustAll"` | 훅이 막지 않은 모든 요청을 허용합니다. |
| `"plan"` | Kiro의 계획 모드로 바꿉니다. V3 전용 |

연결된 세션에서는 `set_permission_mode()`로 바꿉니다. `options.permission_mode`를 연결 뒤에 바꾸면 다음 요청에서 `ValueError`를 냅니다.

### `can_use_tool`

```py
ROOT = Path("/srv/project").resolve()


async def decide(tool_name, tool_input, context):
    consent = context.kiro_consent or {}
    resource = consent.get("resource")
    if consent.get("capability") == "fs_read" and isinstance(resource, str):
        path = (Path(consent.get("workspaceRoot") or ROOT) / resource).resolve()
        if path == ROOT or ROOT in path.parents:
            return PermissionResultAllow()
    return PermissionResultDeny(message="Not allowed")
```

- 허용 여부는 도구 제목이 아니라 Kiro가 보고한 동작과 대상으로 판단합니다. V3는 `context.kiro_consent`에 `capability`(`fs_read`, `fs_write`, `shell` 등), `resource`(대상 파일 경로 등), 작업 공간 안의 대상이면 `workspaceRoot`를 담아 보냅니다. 파일 읽기 요청은 `tool_input`이 비어 있고 `kiro_input_unavailable`이 `True`일 수 있습니다. 대상을 알 수 없는 요청은 거부하도록 작성합니다.
- `can_use_tool`은 Kiro가 묻는 호출만 봅니다. 콜백 하나로 모든 파일 접근을 막을 수는 없으므로 기밀 파일은 Kiro 프로세스가 읽을 수 없는 곳에 둡니다.
- `tool_name`은 Kiro가 보고한 도구 제목입니다. 서브에이전트는 `Sub-agent: <이름>`, MCP 도구는 `@서버/도구`, 스킬은 `Load skill: <이름>` 형식입니다.
- `context`(`ToolPermissionContext`)에는 `title`, `tool_call_id`, `tool_call`, `options`(Kiro가 제시한 선택지), `blocked_path`, `agent_id`, `kiro_agent_type`, `kiro_metadata`, `kiro_consent`, `kiro_input_unavailable`, `request_metadata`가 있습니다.
- `PermissionResultAllow(option_id=...)`로 Kiro가 제시한 특정 선택지(예: 항상 허용)를 고를 수 있고, `kiro_consent`로 Kiro 세션, 작업 공간, 사용자 범위의 동의를 기록할 수 있습니다.
- `updated_input`은 `kiro_shell_edit=True`인 V3 셸 명령에서만 명령 문자열을 바꿀 수 있습니다.
- `PermissionResultDeny(interrupt=True)`는 거부와 함께 턴을 멈춥니다.

### Kiro 권한 규칙과 기타

| 옵션 | 설명 |
| --- | --- |
| `kiro_permissions` | V3 agent 프로필의 권한 규칙. 예: `{"rules": [{"capability": "fs_write", "effect": "deny"}]}` |
| `kiro_policy_presets` | V3 세션에 요청할 Kiro 권한 프리셋 ID |
| `require_sandbox` | V3 명령 샌드박스가 켜져 있지 않으면 프롬프트를 보내지 않습니다. |
| `on_user_input` | Kiro가 사용자에게 질문할 때 답을 돌려주는 비동기 콜백. 답이 없으면 `None` |

연결된 세션에서 `list_kiro_permissions()`, `explain_kiro_permission(capability, resource)`로 Kiro가 적용 중인 규칙을 확인합니다.

## 훅

훅은 Kiro의 생명주기 지점에서 Python 콜백을 실행합니다. 옵션의 키는 Kiro 훅 트리거 이름이고, 콜백이 받는 `hook_event_name`은 Kiro가 보고하는 이벤트 이름입니다.

| 트리거 | `hook_event_name` | SDK 훅(`hooks`) | 네이티브 훅(`native_hooks`) |
| --- | --- | --- | --- |
| `promptSubmit` | `UserPromptSubmit` | 프롬프트를 막거나 문맥, 세션 제목을 더함 | 문맥 |
| `preToolUse` | `PreToolUse` | 도구 호출을 막거나, 사용자 확인을 요청하거나, 허용 | 막기, 확인 요청 |
| `postToolUse` | `PostToolUse` | 도구 결과 관찰 | 관찰 |
| `agentStop` | `Stop` | 턴 종료 관찰, `sdk_stop_hook_mode="native"`면 작업 계속 요청 | 작업 계속 요청 |
| `sessionStart` | `SessionStart` | - | 문맥 |
| `preTaskExecution`, `postTaskExecution` | `PreTaskExec`, `PostTaskExec` | - | spec 작업 전 문맥 추가나 막기, 작업 후 관찰 |
| `fileCreated`, `fileEdited`, `fileDeleted` | `PostFileCreate`, `PostFileSave`, `PostFileDelete` | - | 관찰 |

```py
async def guard(payload, tool_call_id, context):
    if "rm -rf" in str(payload.get("tool_input")):
        return {"block": "Destructive command"}
    return {}

options = KiroAgentOptions(hooks={"preToolUse": [HookMatcher(matcher="shell", hooks=[guard], timeout=10)]})
```

- `HookMatcher.matcher`는 도구 이름(spec 작업 훅은 작업 이름, 파일 훅은 경로)에 맞추는 정규식입니다. `timeout`은 콜백 하나의 제한 시간(초)입니다.
- `on_failure`를 정하지 않으면 콜백 예외와 시간 초과는 오류가 됩니다. `"continue"`는 실패한 콜백을 건너뛰고, `"block"`은 그 요청을 막습니다.
- `payload`에는 Kiro가 주는 `hook_event_name`, `session_id`, `cwd`와 이벤트별 필드(`tool_name`, `tool_input`, `tool_response`, `prompt`, `spec_name`, `task_name`, `task_success`, `file_path`)가 들어 있습니다. SDK는 도구 훅에 `tool_call_id`를, `agentStop` 훅에 `stop_reason`, `last_agent_message`, `continued`(같은 턴에서 이미 작업 계속을 요청했는지)를 더합니다.

### 콜백 반환값

반환값은 Kiro 명령형 훅이 하는 일(종료 코드 0이면 stdout을 문맥에 추가, 종료 코드 2면 stderr를 이유로 막기)을 그대로 키로 표현한 사전입니다. 빈 사전은 아무 것도 하지 않습니다.

| 키 | 값 | 의미 | 트리거 |
| --- | --- | --- | --- |
| `context` | 문자열 | 에이전트 문맥에 더할 텍스트 | `promptSubmit`, `sessionStart`, `preTaskExecution`, `preToolUse`(네이티브, 테스트된 V3 런타임) |
| `block` | 이유 문자열 또는 `True` | 프롬프트, 도구 호출, spec 작업을 막습니다. | `promptSubmit`, `preToolUse`, `preTaskExecution` |
| `ask` | 이유 문자열 또는 `True` | 도구를 실행하기 전에 사용자 확인을 요청합니다. SDK 훅에서는 `can_use_tool`이 답합니다. | `preToolUse` |
| `allow` | `True` | 도구 호출을 허용합니다. | `preToolUse` |
| `tool_input` | 사전 | 허용과 함께 셸 명령을 바꿉니다. SDK 훅에서는 `allow: True`와 함께 주고 `kiro_shell_edit=True`가 필요합니다. | `preToolUse` |
| `title` | 문자열 | 세션 제목을 바꿉니다. V3 전용 | `promptSubmit` |
| `continue` | 지시 문자열 또는 `True` | 턴을 끝내지 않고 이 지시로 작업을 이어 갑니다. | `agentStop` |
| `warning` | 문자열 | 애플리케이션에 경고로 전달합니다(`HookEventMessage` 또는 `on_native_hook_system_message`). | 모든 트리거 |

- `block`, `ask`, `allow`는 하나만 줄 수 있습니다. 트리거가 받지 않는 키는 오류입니다. `on_failure`가 있으면 받지 않는 키를 버리되, `block`과 `ask`는 버리지 않고 오류로 처리해 요청을 막습니다.
- `promptSubmit` 훅이 프롬프트를 막으면 `query()`가 `PromptBlockedError`를 냅니다. `reasons`와 `warnings`에 훅이 준 값이 있습니다.

### SDK 훅의 실행 위치

- `sdk_pretool_hook_mode="permission"`(기본): Kiro의 권한 요청 시점에 실행합니다. Kiro 규칙이 이미 허용한 호출에는 실행되지 않습니다.
- `sdk_pretool_hook_mode="native"`: Kiro V3 네이티브 `preToolUse` 훅으로 등록해 모든 도구 호출 전에 실행합니다. POSIX 전용
- `sdk_stop_hook_mode="native"`: `agentStop` 훅을 Kiro 네이티브 훅으로 실행합니다. 훅이 `continue`를 주면 Kiro가 그 지시를 받아 작업을 이어 갑니다. 한 턴의 계속 횟수는 `max_native_stop_blocks`(기본 1, 최대 16)로 제한합니다.

### 네이티브 훅

`native_hooks`는 Kiro agent 프로필에 명령형 훅을 등록하고, Kiro가 그 명령을 실행하면 SDK 프로세스의 콜백이 답합니다. 콜백 코드가 Kiro와 같은 사용자 권한에서 신호를 받으므로 연결 방식을 명시해야 합니다.

| `native_hook_bridge_mode` | 의미 |
| --- | --- |
| `"reject"` (기본) | 네이티브 훅을 쓰지 않습니다. |
| `"same-user"` | 로컬 소켓 브리지를 씁니다. 같은 사용자로 실행되는 모델 코드와 콜백을 격리하지 않으므로 그 코드를 신뢰할 때만 씁니다. |
| `"runtime-acp"` | 테스트된 Kiro CLI 2.35.1 V3 런타임을 SDK가 패치해 ACP 연결로 훅을 전달합니다. 소켓이나 인증 파일이 없습니다. |

`runtime-acp`와 `runtime_compat_mode="v3"`는 Kiro CLI 2.35.1의 `acp-server.js` 해시를 확인한 뒤에만 동작합니다. 다른 경로의 번들은 `native_hook_runtime_path`로 지정합니다.

## MCP

### 서버 설정

`mcp_servers`는 이름을 키로 하는 사전, JSON 파일 경로, 또는 `{"mcpServers": {...}}` 형식의 JSON 문자열입니다.

| `type` | 필드 |
| --- | --- |
| `"stdio"` | `command`, `args`, `env` |
| `"http"`, `"sse"` | `url`, `headers`. SDK가 중계하는 서버에 `headers`를 주면 `url`은 HTTPS여야 합니다(루프백 HTTP 제외). |
| `"sdk"` | `create_mcp_server()`가 만든 Python 도구 서버, 또는 `instance`(Python MCP SDK 서버 객체, `mcp` extra 필요) |

- `mcp_env_mode="expand"`면 코드로 준 외부 서버 설정의 `${VAR}`를 환경 변수로 바꿉니다.
- `strict_mcp_config=True`면 테스트된 V3 런타임에서 SDK가 넘긴 서버만 연결합니다.

### Python 도구

```py
@tool("add", "Add two numbers", {"a": int, "b": int})
async def add(args):
    return {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}

options = KiroAgentOptions(mcp_servers={"calc": create_mcp_server("calc", tools=[add])})
```

- 입력 스키마는 이름과 타입의 사전, `TypedDict`, 또는 JSON Schema입니다. 인자는 스키마로 검증한 뒤 함수에 넘깁니다.
- `ToolAnnotations(read_only_hint=True, ...)`는 MCP 도구 힌트이며 권한 정책이 아닙니다.
- Kiro에서 도구 이름은 `@calc/add`입니다. `can_use_tool`과 `kiro_permissions`도 이 이름을 씁니다.

### 중계와 제어

| 옵션, 메서드 | 설명 |
| --- | --- |
| `sdk_mcp_input_transforms`, `sdk_mcp_output_transforms` | 서버 이름별 비동기 콜백으로 도구 입력을 실행 전에 검토, 변경하거나 결과를 모델에 보내기 전에 바꿉니다. 콜백이 `McpInputDeniedError`를 내면 호출을 거부합니다. 출력 콜백은 오류 결과(`isError`, Python 도구의 예외 메시지, 원격 서버의 도구 오류)도 받으며, 오류는 바꾼 뒤에도 오류로 남습니다. 서버가 JSON-RPC 오류로 답하면 콜백은 `message`를 담은 오류 결과를 받고, 모델에는 콜백이 돌려준 결과만 갑니다. `data` 같은 나머지 필드는 버립니다. 입력, 출력 콜백이 모두 없으면 Python 도구의 예외 메시지가 그대로 모델에 전달되므로, 비밀이 담길 수 있는 예외는 도구 안에서 처리하거나 출력 콜백으로 지웁니다. |
| `sdk_mcp_toggle_mode` | `toggle_mcp_server()`로 끌 수 있는 서버 범위. `"sdk-tools"`, `"sdk-and-stdio"`, `"sdk-all"` |
| `sdk_tool_interrupt_mode` | `interrupt()` 때 SDK 도구 호출 취소 범위. `"cancel-all"`, `"cancel-observed"` |
| `get_mcp_status()` | Kiro가 보고한 서버 연결 상태 |
| `reconnect_mcp_server()`, `request_mcp_tool_cancellation()` | 원격 서버 재연결, 진행 중인 호출 취소 요청 |
| `get_mcp_resource()`, `get_mcp_prompt()` | MCP 리소스와 프롬프트 조회 |
| `on_mcp_elicitation` | MCP 서버가 사용자에게 추가 정보를 요청할 때 답하는 비동기 콜백 |

### MCP OAuth

`mcp_oauth`에 서버별 `McpOAuthConfig(redirect_uri, authorize, ...)`를 주면 SDK가 HTTP, SSE 서버의 브라우저 OAuth를 중계합니다. `authorize` 콜백이 인가 URL을 사용자에게 보여 주고 돌아온 리디렉션 URL을 돌려줍니다. 토큰은 `InMemoryMcpOAuthStorage` 또는 직접 만든 저장소에 보관합니다. `preauthorize_mcp_oauth()`는 Kiro를 시작하기 전에 인가를 마칩니다.

## 구조화 출력

```py
options = KiroAgentOptions(output_format={"type": "json_schema", "schema": {"type": "object", "properties": {...}}})
```

SDK가 스키마를 프롬프트에 붙이고, 최종 텍스트를 JSON으로 읽어 스키마로 검증합니다. 통과하면 `ResultMessage.structured_output`에 값이 들어가고, 실패하면 `ResultError`를 냅니다. `query_with_structured_output_retry()`는 실패했을 때 애플리케이션이 허락하면 같은 세션에서 한 번 더 요청합니다.

## 세션

| 옵션 | 설명 |
| --- | --- |
| `resume` | 이어 갈 Kiro 세션 ID |
| `continue_conversation` | 작업 디렉터리의 최근 세션 이어 가기 |
| `fork_session`, `resume_session_at` | V3에서 기존 세션을 복사해 새 세션으로 시작. `resume_session_at`은 분기 지점 메시지 ID |
| `session_id` | V3 새 세션에 쓸 UUID |
| `load_timeout_ms` | 세션 불러오기 제한 시간 |

세션 관리 함수는 Kiro가 저장한 세션을 다룹니다: `list_sessions()`, `get_session_info()`, `get_session_messages()`, `get_session_history()`, `rename_session()`, `delete_session()`, `fork_session()`, `list_models()`. 하위 에이전트 기록은 `list_subagents()`, `get_subagent_messages()`로 봅니다. 이벤트 루프 밖에서는 `kiro_agent_sdk.sync`의 같은 이름 함수를 씁니다.

`list_sessions()`가 돌려주는 `SessionInfo`는 Kiro가 보고한 세션 레코드를 그대로 담은 사전이고, 속성으로 주요 값을 읽습니다.

| 속성 | Kiro 값 |
| --- | --- |
| `session_id` | `sessionId` |
| `title` | Kiro가 대화에서 만든 제목, 또는 이름을 바꾼 제목 |
| `updated_at`, `created_at` | `updatedAt`, `createdAt`(Unix 밀리초) |
| `cwd`, `agent`, `model` | 작업 디렉터리, 세션의 Kiro agent, 모델 ID |
| `status`, `source` | 세션 상태, 세션을 가진 Kiro 엔진(`v3` 등) |

연결된 클라이언트에서는 `fork_session()`, `rename_session()`, `compact()`(문맥 압축), `export_session()`, `get_history_page()`를 씁니다. `enable_file_checkpointing=True`면 `restore_checkpoint()`, `rewind_files()`로 파일 변경을 되돌립니다.

`KiroSessionClient`는 애플리케이션 토큰 인증(`kiro_auth_method="client"`)을 쓰는 환경에서 같은 토큰 공급자로 V3 세션을 조회, 이름 변경, 분기, 삭제합니다. 채팅 세션을 새로 만들지 않으며, 네트워크 서비스에서는 `SessionAccess` 인가 콜백으로 세션 소유자를 확인합니다.

## 세션 저장소

`session_store`를 주면 SDK가 대화를 저장소에 미러링합니다. Kiro의 로컬 세션 파일과 별개이므로 다른 호스트에서 기록을 읽거나 세션을 복구할 수 있습니다.

| 옵션 | 설명 |
| --- | --- |
| `session_store` | `InMemorySessionStore`, `SQLiteSessionStore`, 또는 직접 구현한 저장소 |
| `session_store_flush` | `"batched"`(기본) 또는 `"eager"` |
| `session_store_flush_timeout` | 턴 끝과 연결 종료 때 플러시 제한 시간 |
| `session_store_error_policy` | `"fail"`(기본)은 저장 실패를 오류로, `"continue"`는 `MirrorErrorMessage`로 알리고 계속 |
| `session_store_archive` | 완료된 턴마다 Kiro 세션 아카이브를 미러에 추가 |
| `resume_from_store` | `resume` 세션의 아카이브를 미러에서 가져와 새 로컬 세션으로 복구 |
| `session_store_source_directory` | 다른 작업 공간으로 복구할 때 원래 경로 |

미러는 Kiro가 보낸 ACP 이벤트를 차례로 담습니다. 항목은 `{"type": ..., "data": ...}` 형식이며 `type`은 `prompt`, `update`, `notification`, `result`, `session_archive`, `session_info`입니다.

- `session_info`는 Kiro가 보고한 세션 메타데이터입니다. 턴이 끝날 때마다 SDK가 Kiro의 `session/list`에서 그 세션의 레코드(제목, 수정 시각, 작업 디렉터리, agent)를 받아 기록하고, `rename_session()`으로 바꾼 제목도 기록합니다. 아카이브를 가져올 때는 아카이브의 `session.json`에서 제목, 생성 시각, agent, 모델을 읽습니다.
- 저장소 목록(`list_sessions_from_store()`)은 이 기록을 보여 주므로, Kiro가 없는 호스트에서도 Kiro가 붙인 제목으로 세션을 고를 수 있습니다. 여러 `session_info`가 있으면 필드마다 나중 값을 씁니다(`merge_session_info()`).
- Kiro가 세션 목록을 주지 못하면 메타데이터 없이 대화만 저장합니다.

직접 만드는 저장소는 비동기 `append(key, entries)`와 `load(key)`를 구현합니다. `list_sessions`, `delete`, `list_subkeys`를 더하면 저장소 함수를 모두 쓸 수 있습니다. 세션마다 최신 메타데이터를 따로 보관하는 `list_session_info(project_key)`를 더하면 목록을 만들 때 미러 전체를 읽지 않습니다. `append()` 안에서 `merge_session_info()`로 갱신하고 `{"session_id", "mtime", "info"}` 목록을 돌려주면 됩니다. `key`는 `{"project_key": ..., "session_id": ..., "subpath": ...}`이고, 하위 에이전트 기록은 `subpath`로 구분합니다. `project_key_for_directory(path)`가 작업 디렉터리에서 `project_key`를 만듭니다.

저장소 함수: `list_sessions_from_store()`, `get_session_info_from_store()`, `get_session_messages_from_store()`, `rename_session_via_store()`, `delete_session_via_store()`, `fork_session_via_store()`. 아카이브는 `export_session_archive()`, `import_session_archive()`, `import_session_to_store()`, `restore_session_from_store()`로 다룹니다.

## 사용량 상한

| 옵션 | 설명 |
| --- | --- |
| `max_reported_credits` | 이 클라이언트에서 Kiro가 보고한 크레딧 합계가 상한에 닿으면 다음 턴을 보내지 않고 `ReportedCreditLimitError`를 냅니다. V3 전용 |
| `reported_credit_ledger`, `reported_credit_scope` | 여러 클라이언트가 같은 상한을 나누는 장부. `SQLiteCreditLedger`, `DynamoDBCreditLedger` |
| `runtime_usage_mode="reported"` | 테스트된 V3 런타임에서 토큰 사용량을 `ResultMessage.kiro_model_usage`로 받습니다. |

`get_kiro_account_usage()`는 Kiro 계정의 사용량을 조회합니다. 크레딧은 Kiro가 보고한 값이며 청구 금액이 아닙니다.

## 백그라운드 작업

`BackgroundTaskManager`는 긴 작업을 별도 Kiro 세션으로 실행합니다.

```py
manager = BackgroundTaskManager(max_tasks=4)
task_id = await manager.start("Review the repository", options=KiroAgentOptions(cwd="project", agent_engine="v3"))
status = await manager.wait(task_id)
result = manager.retained_result(task_id)
await manager.aclose()
```

- `status()`, `statuses()`로 상태를, `events()`, `event_history()`로 진행 이벤트를 봅니다. `stop()`은 작업을 멈춥니다.
- `client.attach_background_tasks(manager)`로 클라이언트에 붙이면 `stop_task()`로 같은 작업을 다룹니다.
- 요청과 결과를 다른 프로세스에서 보려면 `SQLiteBackgroundRequestStore`, `DynamoDBBackgroundRequestStore`, `SQLiteBackgroundResultStore`, `DynamoDBBackgroundResultStore`를 씁니다.

## Kiro 기능

연결된 V3 클라이언트에서 Kiro의 기능을 직접 호출합니다.

| 분류 | 메서드 |
| --- | --- |
| 메모리 | `list_kiro_memories()`, `get_kiro_memory()`, `create_kiro_memory()`, `update_kiro_memory()`, `delete_kiro_memory()`, `get_kiro_memory_sync_status()` |
| 워크플로 | `list_kiro_workflows()`, `create_kiro_workflow()`, `inspect_kiro_workflow()`, `run_kiro_workflow()`, `pause_kiro_workflow()`, `resume_kiro_workflow()`, `cancel_kiro_workflow()`, `delete_kiro_workflow()` |
| 문맥 | `list_kiro_context()`, `add_kiro_context()`, `remove_kiro_context()`, `clear_kiro_context()`, `get_context_usage()` |
| 코드 인텔리전스 | `enable_kiro_code_intelligence=True`와 `get_kiro_code_intelligence_status()`, `initialize_kiro_code_intelligence()`, `get_kiro_code_overview()` |
| 조회 | `get_server_info()`, `get_kiro_available_commands()`, `list_kiro_hooks()`, `list_kiro_powers()`, `get_sandbox_status()`, `get_kiro_spec_task_statuses()` |
| 기타 | `extension_request(method, params)`로 SDK가 감싸지 않은 Kiro 확장 메서드 호출 |

Kiro 메모리 동기화는 API 키 인증에서는 꺼져 있을 수 있습니다. `get_kiro_memory_sync_status()`로 확인하세요.

## 인증

| `kiro_auth_method` | 설명 |
| --- | --- |
| `"cli"` (기본) | Kiro CLI의 로그인 상태나 `KIRO_API_KEY` 환경 변수를 씁니다. |
| `"client"` | Kiro V3가 토큰을 요청할 때마다 `kiro_access_token_provider()`를 호출합니다. 반환값은 `{"accessToken": ..., "expiresAt": ..., "profileArn": ...}`입니다. |

- Kiro CLI 2.35.1의 `kiro-cli` 실행 파일은 저장된 로그인이 없으면 시작 전에 로그인을 요구합니다. 로그인 저장소가 없는 서버에서는 같은 설치의 `kiro-cli-chat`을 `cli_path`로 지정합니다.
- `kiro_access_token_timeout`은 공급자 호출 제한 시간입니다.
- 진행 중인 턴이 없을 때 오는 토큰 요청은 기본적으로 거부합니다. `kiro_client_auth_idle_mode="authorized"`와 `kiro_idle_token_authorizer`로 허용 조건을 줄 수 있습니다.
- 로그인 토큰은 수명이 짧습니다. 갱신 토큰으로 미리 새 토큰을 받아 두는 일은 애플리케이션이 맡습니다.

## 프로세스와 환경

| 옵션 | 설명 |
| --- | --- |
| `cli_path` | Kiro CLI 경로 (기본 `kiro-cli`) |
| `env` | Kiro 프로세스에 더할 환경 변수 |
| `env_inheritance_mode` | `"inherit"`(기본) 또는 `"minimal"`. `"minimal"`은 PATH, HOME, 로캘, TMPDIR, CA, 프록시, XDG, KIRO_HOME 외의 상속 변수를 비웁니다. `KIRO_API_KEY`도 상속하지 않으므로 필요하면 `env`에 넣습니다. |
| `user` | Kiro를 실행할 다른 로컬 계정. 기본 그룹과 보조 그룹도 그 계정의 것으로 바꿉니다. 다른 계정으로 바꾸려면 root 권한이 필요합니다. POSIX 전용 |
| `stderr` | Kiro CLI 진단 출력을 받는 동기 콜백 |
| `startup_timeout`, `turn_timeout` | 시작 요청과 턴의 제한 시간(초) |
| `max_buffer_size` | 한 JSON-RPC 메시지의 최대 크기 |
| `enable_kiro_hooks` | V3에서 Kiro 훅 기능 사용 여부 (기본 `True`) |
| `runtime_compat_mode` | `"v3"`면 테스트된 V3 런타임 패치로 반복 상한, 원문 입력(`verbatim_prompts`), 대체 모델 같은 기능을 켭니다. |

## 오류 처리

| 오류 | 의미 |
| --- | --- |
| `CLINotFoundError` | Kiro CLI를 찾을 수 없음. `cli_path`와 PATH를 확인 |
| `CLIConnectionError` | 연결 상태가 요청에 맞지 않음. 예: 연결 전 `query()`, 진행 중인 응답을 다 받지 않은 채 다음 질의 |
| `ProcessError` | Kiro 프로세스가 비정상 종료. `exit_code`, `stderr` 확인 |
| `ProtocolError`, `JSONRPCError`, `CLIJSONDecodeError` | ACP 메시지 오류나 Kiro가 거부한 요청. `JSONRPCError.code`, `data` 확인 |
| `PromptBlockedError` | `promptSubmit` 훅이 프롬프트를 막음. `reasons`, `warnings` 확인 |
| `ResultError` | 구조화 출력 검증 실패 등 결과를 받을 수 없음. `kind`(예: `invalid_structured_output`), `errors`, `result` 확인 |
| `McpInputDeniedError` | 입력 변환 콜백이 MCP 호출을 거부 |
| `ReportedCreditLimitError` | 크레딧 상한 도달 |
| `SessionArchiveError`, `StoreForkError`, `CheckpointRecoveryError` | 아카이브, 저장소 분기, 체크포인트 복구 실패 |

모든 SDK 오류는 `KiroSDKError`의 하위 클래스입니다. 옵션 값이 잘못되면 연결 전에 `ValueError` 또는 `TypeError`를 냅니다.
