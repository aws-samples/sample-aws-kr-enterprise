# Agent Runtime Base Image 분석 — `agent_runner.py`

> 대상 파일: `code/agent-runtime/agent_runner.py` (402 lines)
> 작성일: 2026-06-04

## 1. 개요

`agent_runner.py`는 **모든 Domain/Supervisor Agent가 공유하는 단일 범용 Base Image의 진입점**이다. 도메인별로 코드가 따로 생성되지 않으며, 컨테이너는 환경변수 `AGENT_ID`로만 구분된다. 실제 에이전트의 성격(시스템 프롬프트, 도구, 위임 대상, 하니스 정책)은 모두 **DynamoDB에 저장된 Agent Config**에서 런타임에 로드되어 동적으로 조립된다.

```
하나의 Base Image  ×  AGENT_ID별 Config(DynamoDB)  =  서로 다른 Agent 인스턴스
```

- **프레임워크**: FastAPI (HTTP 서버) + Strands Agents SDK (LLM 에이전트)
- **모델**: Amazon Bedrock (`BedrockModel`), config의 `model` 필드로 지정
- **배포 대상**: AWS Bedrock AgentCore Runtime
- **관찰성**: OpenTelemetry auto-instrumentation + 커스텀 SpanProcessor + DynamoDB Side-Channel

---

## 2. 구성요소 맵

`agent_runner.py`는 아래 모듈들을 오케스트레이션하는 **조립자(assembler) + HTTP 핸들러** 역할을 한다.

| 영역 | 코드 위치 | 의존 모듈 |
|------|-----------|-----------|
| 전역 상태/환경변수 | L26–33 | — |
| Side-Channel 디버그 기록 | `_write_debug_event` (L36–57) | `side_channel`, `ulid` |
| **Lazy 초기화 (핵심 조립부)** | `_initialize_agent` (L60–229) | `config_loader`, `internal_tools`, `mcp_connector`, `harness`, `observability_hook`, `span_filter`, `strands` |
| 스트리밍 핸들러 | `_stream_agent` (L232–275) | `strands` stream, `side_channel` |
| HTTP 엔드포인트 | `/invocations`, `/health`, `/ping` (L281–397) | FastAPI |

---

## 3. 환경변수 (L26–28)

| 변수 | 기본값 | 용도 |
|------|--------|------|
| `AGENT_ID` | `""` | 이 컨테이너가 어떤 에이전트인지 식별. Config 키 + supervisor 판별에 사용 |
| `DYNAMODB_TABLE` | `aiops-platform` | Config / Side-Channel / Runtime 정보가 담긴 단일 테이블 |
| `AWS_REGION` | `ap-northeast-2` | Bedrock / DynamoDB 리전 |

> Dockerfile에서 추가로 `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=urllib3,requests,httpx`를 설정해 noise HTTP span을 제거하고, `opentelemetry-instrument`로 실행한다.

---

## 4. Lazy Initialization — `_initialize_agent()` (핵심)

> **설계 의도**: FastAPI는 5초 내에 즉시 기동되어야 AgentCore health check를 통과한다. 따라서 무거운 LLM/MCP 초기화는 첫 `/invocations` 요청 시 1회만 수행한다. `threading.Lock` + double-checked locking으로 동시 요청에서도 1회만 실행되도록 보장한다 (L62–68).

초기화는 다음 순서로 진행되며, 각 단계는 Side-Channel `__debug_{AGENT_ID}` 파티션에 디버그 이벤트로 기록된다.

### 4.1 Config 로드 (L77–89)
- `load_config(AGENT_ID, table)` → DynamoDB `PK=AGENT#{id}, SK=CONFIG` 항목 조회 (`config_loader.py`)
- **지수 백오프 재시도** (최대 3회, `2**attempt`초): 배포 직후 Config 쓰기와 컨테이너 기동 사이의 경합을 흡수
- Config은 immutable — 시작 시 1회만 로드

### 4.2 Delegations → A2A Tool 자동 변환 (L91–104)
- config의 `delegations[]` 각 항목을 `scoped_agent_invoke` 타입 internal tool로 변환
- 생성 도구명: `delegate_{target}` (하이픈→언더스코어)
- `internalTools[]`와 합쳐 `all_tool_configs` 구성

### 4.3 Internal Tools 등록 (L106–130)
- `create_internal_tool(tool_config, dynamodb)` (`internal_tools/__init__.py`)로 각 도구를 Strands `@tool`로 래핑
- 지원 타입(`HANDLER_MAP` + 분기):
  - `dynamodb_query` / `dynamodb_get` / `dynamodb_put`
  - `agent_invoke` (동적 대상) / `scoped_agent_invoke` (고정 대상, delegation용)
  - `python_function` (모듈 동적 import)
- **개별 도구 실패는 격리**: 예외 발생 시 해당 도구만 skip하고 `tool_errors`에 기록 (에이전트 전체는 계속 기동)

### 4.4 MCP Gateway 연결 (L132–143)
- `gateways[]`가 있을 때만 `connect_gateways(config)` 호출 (`mcp_connector.py`)
- `MCPClient` + **SigV4 서명 transport** (`bedrock-agentcore` 서비스) + `streamablehttp_client`
- `toolFilter`가 `"all"`이면 전체 도구, 배열이면 `allowed_tool_names`로 선택 바인딩
- 연결 실패는 `BaseException`까지 잡아 skip (Gateway 장애가 에이전트 기동을 막지 않음)

### 4.5 Strands Agent 조립 (L144–191)
- **모델**: `BedrockModel(model_id, region, max_tokens=32768)`. 기본 `apac.anthropic.claude-sonnet-4-...`
- **Supervisor 판별**: `AGENT_ID`에 `"supervisor"` 포함 여부 → `ObservabilityHook(is_supervisor=...)`
- **도구 결합**: `all_tools = internal_tools + mcp_clients`
- **Harness**: `Tier2Harness(config)` 생성 (pre/post hooks, delegation/depth 검증)
- **System Prompt 조립**:
  - 기본값: config의 `systemPrompt` (없으면 "You are a helpful AI assistant.")
  - **persona-injection pre-hook**: `harness.pre_hooks`에 `persona-injection`이 있으면 `contextBoundary` 기반 **SCOPE ENFORCEMENT** 규칙을 프롬프트 끝에 자동 주입 (범위 밖 요청 거부 강제)
- 최종: `Agent(model, tools, system_prompt, hooks=[obs_hook])`

### 4.6 상태 저장 & 완료 (L193–229)
- `_state` 딕셔너리에 `agent`, `harness`, `config`, `obs_hook`, `table`, `dynamodb` 저장
- `_initialized = True`
- `init_complete` 디버그 이벤트 기록 (모델, 도구 수, 도구명, MCP 수, supervisor 여부)
- **OTEL SpanProcessor 등록**: `AgentSpanProcessor()`를 TracerProvider에 추가 → 모든 span에 `agent.id`/`session.id`/`agent.phase` 자동 주입 (`span_filter.py`)

---

## 5. 요청 처리 — `/invocations` (L281–385)

```
POST /invocations
 ├─ _initialize_agent()              # 첫 요청 시 1회 조립
 ├─ type == "wake-up"?  → {status: ready}  즉시 리턴 (LLM 미호출, 워밍업)
 ├─ Accept: text/event-stream?  → _stream_agent() (SSE)
 └─ 일반(JSON) 처리
```

### 5.1 요청 컨텍스트 (L301–320)
- 입력: `prompt`, `context.{sessionId, caller, delegationDepth}`
- `_request_context["session_id"]` 갱신 → A2A 도구가 빈 session_id로 호출돼도 fallback
- 현재 OTEL span에 `session.id`, `agent.id`, `caller`, `delegation.depth` attribute 설정

### 5.2 Tier 2 Pre-Hook: Depth 검증 (L322–326)
- `harness.check_depth(depth)` → `delegationDepth`가 `max_depth(=2)` 초과 시 에러 반환 (A2A 무한 위임 방지)

### 5.3 실행 & Side-Channel (L328–385)
- `SideChannelWriter`로 `agent_start` → `message`(final) / `error` 이벤트 발행
- `obs_hook.writer`를 요청별로 주입했다가 `finally`에서 해제
- PRE/POST_INVOKE 로깅: messages 개수, 누적 문자 수, stop_reason 등
- 예외 시 traceback 마지막 500자를 응답·Side-Channel에 기록

### 5.4 스트리밍 경로 — `_stream_agent()` (L232–275)
- `agent.stream_async(prompt)` 이벤트를 SSE로 변환:
  - `data` → `event: text`
  - `current_tool_use` → `event: tool_call` (phase: start)
  - `result` → `event: done` (+ Side-Channel `message` final)

### 5.5 헬스 체크
- `/health` (L388): `{status: healthy}`
- `/ping` (L393): AgentCore 표준 — `{status: Healthy, time_of_last_update}`

---

## 6. 관찰성 (Observability) 3중 구조

| 계층 | 메커니즘 | 코드 |
|------|----------|------|
| **분산 추적** | OTEL auto-instrumentation + `AgentSpanProcessor` (span에 agent/session/phase 주입) | `span_filter.py` |
| **실시간 이벤트** | `ObservabilityHook` — Strands `Before/AfterToolCallEvent` 구독 → Side-Channel `tool_call` / Supervisor의 `routing` 이벤트 | `observability_hook.py` |
| **세션 타임라인** | `SideChannelWriter` — DynamoDB `PK=SESSION#{id}` 에 ULID 정렬 이벤트 기록 (TTL 7일, fire-and-forget) | `side_channel.py` |

이벤트 타입: `agent_start`, `tool_call`(start/end), `routing`, `a2a_delegation`(start/end/error), `message`, `error`, 그리고 초기화 디버그(`init_start`/`init_tools`/`init_complete`/`init_error`).

---

## 7. Agent-to-Agent (A2A) 위임

> `docs/a2a-implementation.md` 참고. **Google A2A Protocol이 아니라** AWS Bedrock AgentCore의 native `invoke_agent_runtime` API 기반.

- `delegations[]` → `scoped_agent_invoke`(대상 고정) / `agent_invoke`(대상 동적) 도구로 노출 (`internal_tools/agent_invoke_handler.py`)
- 대상 에이전트의 `PK=AGENT#{id}, SK=RUNTIME` 항목에서 `runtimeArn` 조회, `status ∈ {active, READY}`인 경우만 호출
- 호출 시 `context.delegationDepth + 1` 전파 → 수신 측 depth 검증으로 무한 루프 차단
- 각 위임은 `a2a_delegation` 이벤트(start/end/error)로 Side-Channel에 기록

---

## 8. 핵심 설계 패턴 요약

1. **Config-Driven / 단일 Base Image** — 코드 생성 없이 DynamoDB Config로 에이전트를 차별화
2. **Lazy Init + Double-Checked Locking** — 빠른 기동(health check 통과) + 1회 조립 보장
3. **Graceful Degradation** — 도구/MCP/Side-Channel 실패가 에이전트 전체 기동·실행을 막지 않음 (격리·skip·fire-and-forget)
4. **Hook 기반 횡단 관심사** — persona-injection(프롬프트), scope/depth 검증(하니스), 관찰성(Strands hook + OTEL)을 코드 본문과 분리
5. **재시도 내성** — Config 로드 지수 백오프로 배포 직후 경합 흡수

---

## 9. 모듈 의존 관계도

```
agent_runner.py  (FastAPI 진입점 + 조립자)
├── config_loader.py            Config 로드 (DynamoDB)
├── internal_tools/             도구 팩토리
│   ├── __init__.py             create_internal_tool / 타입 디스패치
│   ├── dynamodb_handler.py     dynamodb_query/get/put
│   └── agent_invoke_handler.py A2A invoke (scoped/dynamic)
├── mcp_connector.py            MCP Gateway + SigV4 transport
├── harness.py                  Tier2Harness (pre/post hook, depth/delegation 검증)
├── observability_hook.py       Strands HookProvider → Side-Channel
├── side_channel.py             SideChannelWriter (DynamoDB 이벤트)
└── span_filter.py              AgentSpanProcessor (OTEL enrichment)
```
