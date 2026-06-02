# Claude Code OpenTelemetry 텔레메트리 스키마

이 문서는 Claude Code가 내보내는 OpenTelemetry 텔레메트리 데이터의 전체 스키마를 정의합니다.
메트릭은 OTel Metrics 프로토콜, 이벤트는 OTel Logs 프로토콜을 통해 전송됩니다.

---

## 목차

1. [리소스 속성 (Resource Attributes)](#리소스-속성-resource-attributes)
2. [공통 속성 (Common Attributes)](#공통-속성-common-attributes)
3. [메트릭 (Metrics)](#메트릭-metrics)
4. [이벤트 (Events)](#이벤트-events)

---

## 리소스 속성 (Resource Attributes)

모든 텔레메트리 데이터에 포함되는 리소스 수준 속성입니다.
OTel Resource 스펙에 따라 텔레메트리를 생성하는 엔티티를 식별합니다.

| 속성명 | 타입 | 설명 | 예시 |
|--------|------|------|------|
| `service.name` | string | 서비스 식별자 (고정값) | `claude-code` |
| `service.version` | string | Claude Code 버전 | `2.1.159` |
| `os.type` | string | 운영체제 종류 | `darwin`, `linux`, `windows` |
| `os.version` | string | 운영체제 버전 | `25.2.0` |
| `host.arch` | string | 호스트 CPU 아키텍처 | `arm64`, `x86_64` |

---

## 공통 속성 (Common Attributes)

모든 메트릭과 이벤트 데이터 포인트에 포함되는 속성입니다.

| 속성명 | 타입 | 설명 | 예시 |
|--------|------|------|------|
| `session.id` | string | 세션 고유 식별자 (UUID) | `a1b2c3d4-e5f6-7890-abcd-ef1234567890` |
| `app.version` | string | 애플리케이션 버전 | `1.0.32` |
| `organization.id` | string | 조직 식별자 | `org_abc123` |
| `user.account_uuid` | string | 사용자 계정 UUID | `user_def456` |
| `terminal.type` | string | 터미널 종류 | `vscode`, `iterm2`, `terminal`, `warp` |

---

## 메트릭 (Metrics)

Claude Code는 OTel Metrics 프로토콜(OTLP)을 통해 아래 메트릭을 내보냅니다.
모든 메트릭은 **monotonic counter** 타입이며, 누적(cumulative) 집계 방식을 사용합니다.

### claude_code.session.count

세션 시작 횟수를 추적하는 카운터입니다.

| 항목 | 값 |
|------|-----|
| **이름** | `claude_code.session.count` |
| **타입** | Counter (monotonic) |
| **단위** | `{session}` |
| **설명** | Claude Code 세션이 시작된 횟수 |

**메트릭 고유 속성 (2.x):**

| 속성명 | 타입 | 필수 | 설명 | 허용값 |
|--------|------|------|------|--------|
| `start_type` | string | N | 세션 시작 유형 (2.x 신규) | `fresh`, `resume` |

---

### claude_code.lines_of_code.count

코드 라인 변경 수를 추적하는 카운터입니다.

| 항목 | 값 |
|------|-----|
| **이름** | `claude_code.lines_of_code.count` |
| **타입** | Counter (monotonic) |
| **단위** | `{line}` |
| **설명** | Claude Code가 수정한 코드 라인 수 |

**메트릭 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 허용값 |
|--------|------|------|------|--------|
| `type` | string | Y | 변경 유형 | `added`, `removed` |

---

### claude_code.pull_request.count

Pull Request 생성 횟수를 추적하는 카운터입니다.

| 항목 | 값 |
|------|-----|
| **이름** | `claude_code.pull_request.count` |
| **타입** | Counter (monotonic) |
| **단위** | `{pull_request}` |
| **설명** | Claude Code로 생성된 Pull Request 수 |

**메트릭 고유 속성:** 없음 (공통 속성만 사용)

---

### claude_code.commit.count

Git 커밋 생성 횟수를 추적하는 카운터입니다.

| 항목 | 값 |
|------|-----|
| **이름** | `claude_code.commit.count` |
| **타입** | Counter (monotonic) |
| **단위** | `{commit}` |
| **설명** | Claude Code로 생성된 Git 커밋 수 |

**메트릭 고유 속성:** 없음 (공통 속성만 사용)

---

### claude_code.cost.usage

API 사용 비용을 추적하는 카운터입니다. 단위는 USD입니다.

| 항목 | 값 |
|------|-----|
| **이름** | `claude_code.cost.usage` |
| **타입** | Counter (monotonic) |
| **단위** | `USD` |
| **설명** | Claude Code API 호출에 사용된 비용 (USD) |

**메트릭 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 예시 |
|--------|------|------|------|------|
| `model` | string | Y | 사용된 AI 모델 이름 | `claude-opus-4-6`, `claude-sonnet-4-20250514` |

**2.x 신규 라벨 (귀속/Attribution):** 아래 라벨은 2.x부터 비용/토큰 메트릭에 추가되어 AMP에 수집됩니다. 해당 컨텍스트가 없으면 빈 문자열입니다.

| 라벨명 | 설명 | 예시 |
|--------|------|------|
| `agent_name` | 비용을 유발한 Subagent 이름 (빈 값 = 메인 스레드) | `Explore`, `Plan`, `general-purpose`, `custom` |
| `effort` | Effort 모드 | `high`, `xhigh` |
| `query_source` | 쿼리 소스 | `repl_main_thread` |
| `mcp_server_name` | 귀속된 MCP 서버 이름 | `playwright` |
| `mcp_tool_name` | 귀속된 MCP 도구 이름 | `browser_click` |
| `skill_name` | 활성 Skill 이름 (빈 값 = 메인 스레드) | `frontend-design:frontend-design` |
| `plugin_name` | 플러그인 이름 | `skill-creator` |
| `marketplace_name` | 마켓플레이스 이름 | `claude-plugins-official` |

---

### claude_code.token.usage

토큰 사용량을 추적하는 카운터입니다.

| 항목 | 값 |
|------|-----|
| **이름** | `claude_code.token.usage` |
| **타입** | Counter (monotonic) |
| **단위** | `{token}` |
| **설명** | Claude Code API 호출에 사용된 토큰 수 |

**메트릭 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 허용값 |
|--------|------|------|------|--------|
| `type` | string | Y | 토큰 유형 | `input`, `output`, `cacheRead`, `cacheCreation` |
| `model` | string | Y | 사용된 AI 모델 이름 | `claude-opus-4-6`, `claude-sonnet-4-20250514` |

**2.x 신규 라벨:** `cost.usage`와 동일하게 `agent_name`, `effort`, `query_source`, `mcp_server_name`, `mcp_tool_name`, `skill_name`, `plugin_name`, `marketplace_name` 라벨이 토큰 메트릭에도 추가되어 Subagent/Effort/Skill별 토큰 귀속 분석이 가능합니다.

**토큰 유형 설명:**

| 값 | 설명 |
|----|------|
| `input` | 모델에 전송된 입력 토큰 |
| `output` | 모델이 생성한 출력 토큰 |
| `cacheRead` | 프롬프트 캐시에서 읽은 토큰 |
| `cacheCreation` | 프롬프트 캐시에 기록된 토큰 |

---

### claude_code.code_edit_tool.decision

코드 편집 도구의 수락/거절 결정을 추적하는 카운터입니다.

| 항목 | 값 |
|------|-----|
| **이름** | `claude_code.code_edit_tool.decision` |
| **타입** | Counter (monotonic) |
| **단위** | `{decision}` |
| **설명** | 코드 편집 도구 사용 결정 (수락/거절) 횟수 |

**메트릭 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 허용값 |
|--------|------|------|------|--------|
| `tool` | string | Y | 편집 도구 이름 | `Edit`, `Write`, `NotebookEdit` |
| `decision` | string | Y | 결정 유형 | `accept`, `reject` |
| `language` | string | Y | 프로그래밍 언어 | `python`, `typescript`, `javascript`, `go` 등 |

---

### claude_code.active_time.total

활성 사용 시간을 추적하는 카운터입니다.

| 항목 | 값 |
|------|-----|
| **이름** | `claude_code.active_time.total` |
| **타입** | Counter (monotonic) |
| **단위** | `s` (초) |
| **설명** | Claude Code가 활성 상태인 총 시간 (초) |

**메트릭 고유 속성:** 없음 (공통 속성만 사용)

---

## 이벤트 (Events)

Claude Code는 OTel Logs 프로토콜(OTLP)을 통해 아래 이벤트를 내보냅니다.
각 이벤트는 LogRecord의 `Body`에 구조화된 데이터로 전달되며, `event.name` 속성으로 이벤트 유형을 식별합니다.
모든 이벤트에 공통 속성이 포함됩니다.

> 총 12종 이벤트: 1.x 기준 5종(`user_prompt`, `tool_result`, `api_request`, `api_error`, `tool_decision`) + 2.x 신규 7종(`hook_execution_start`, `hook_execution_complete`, `hook_registered`, `plugin_loaded`, `mcp_server_connection`, `skill_activated`, `subagent_completed`). 2.x 신규 이벤트와 기존 이벤트의 2.x 확장 속성은 아래 [이벤트 (2.x 신규)](#이벤트-2x-신규) 및 [기존 이벤트 2.x 확장 속성](#기존-이벤트-2x-확장-속성) 절을 참고하세요.

### claude_code.user_prompt

사용자가 입력한 프롬프트 이벤트입니다. 기본적으로 프롬프트 내용은 리다이렉트(redacted)됩니다.

| 항목 | 값 |
|------|-----|
| **이벤트명** | `claude_code.user_prompt` |
| **SeverityText** | `INFO` |
| **설명** | 사용자 프롬프트 입력 이벤트 |

**이벤트 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 비고 |
|--------|------|------|------|------|
| `prompt_length` | int | Y | 프롬프트 문자 수 | 항상 포함 |
| `prompt` | string | N | 프롬프트 원문 | 기본적으로 수집하지 않음 (redacted). 조직 설정에 따라 활성화 가능 |

---

### claude_code.tool_result

도구 실행 결과 이벤트입니다.

| 항목 | 값 |
|------|-----|
| **이벤트명** | `claude_code.tool_result` |
| **SeverityText** | `INFO` |
| **설명** | 도구 실행 완료 이벤트 |

**이벤트 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 예시 |
|--------|------|------|------|------|
| `tool_name` | string | Y | 실행된 도구 이름 | `Read`, `Edit`, `Bash`, `Grep`, `Glob`, `Write` |
| `success` | boolean | Y | 실행 성공 여부 | `true`, `false` |
| `duration_ms` | int | Y | 실행 소요 시간 (밀리초) | `1523` |
| `error` | string | N | 오류 메시지 (실패 시) | `File not found` |
| `decision` | string | Y | 도구 실행 결정 | `accept`, `reject` |
| `source` | string | Y | 결정 주체 | `user`, `auto`, `policy` |
| `tool_parameters` | string | N | 도구 호출 파라미터 (JSON) | `{"file_path": "/src/main.py"}` |

---

### claude_code.api_request

AI 모델 API 호출 이벤트입니다.

| 항목 | 값 |
|------|-----|
| **이벤트명** | `claude_code.api_request` |
| **SeverityText** | `INFO` |
| **설명** | API 요청 완료 이벤트 |

**이벤트 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 예시 |
|--------|------|------|------|------|
| `model` | string | Y | 사용된 AI 모델 이름 | `claude-opus-4-6` |
| `cost_usd` | double | Y | 요청 비용 (USD) | `0.0123` |
| `duration_ms` | int | Y | 요청 소요 시간 (밀리초) | `3542` |
| `input_tokens` | int | Y | 입력 토큰 수 | `1500` |
| `output_tokens` | int | Y | 출력 토큰 수 | `800` |
| `cache_read_tokens` | int | Y | 캐시에서 읽은 토큰 수 | `500` |
| `cache_creation_tokens` | int | Y | 캐시에 기록된 토큰 수 | `200` |

---

### claude_code.api_error

AI 모델 API 호출 오류 이벤트입니다.

| 항목 | 값 |
|------|-----|
| **이벤트명** | `claude_code.api_error` |
| **SeverityText** | `ERROR` |
| **설명** | API 요청 실패 이벤트 |

**이벤트 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 예시 |
|--------|------|------|------|------|
| `model` | string | Y | 사용된 AI 모델 이름 | `claude-opus-4-6` |
| `error` | string | Y | 오류 메시지 | `rate_limit_exceeded` |
| `status_code` | int | Y | HTTP 상태 코드 | `429`, `500`, `503` |
| `duration_ms` | int | Y | 요청 소요 시간 (밀리초) | `1200` |
| `attempt` | int | Y | 재시도 횟수 (1부터 시작) | `1`, `2`, `3` |

---

### claude_code.tool_decision

도구 사용 결정 이벤트입니다. 사용자 또는 정책에 의한 도구 허용/거절을 기록합니다.

| 항목 | 값 |
|------|-----|
| **이벤트명** | `claude_code.tool_decision` |
| **SeverityText** | `INFO` |
| **설명** | 도구 사용 허용/거절 결정 이벤트 |

**이벤트 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 예시 |
|--------|------|------|------|------|
| `tool_name` | string | Y | 도구 이름 | `Bash`, `Edit`, `Write` |
| `decision` | string | Y | 결정 유형 | `accept`, `reject` |
| `source` | string | Y | 결정 주체 | `user`, `auto`, `policy` |

---

## 이벤트 (2.x 신규)

Claude Code 2.x부터 Hooks, Plugins, MCP, Skills, Subagent(Agent) 관련 신규 이벤트가 추가되었습니다.
아래 이벤트와 속성은 live OTLP 데이터(us-east-1, 2026-06)로 검증되었습니다.
2.x에서는 일부 속성이 점(`.`) 표기로 전송되며(예: `event.sequence`, `plugin.name`, `skill.name`),
파이프라인 변환 단계에서 평탄화된(flat) 컬럼명으로 매핑됩니다.

> **공통 신규 속성**: 모든 신규 이벤트에는 `event.sequence` (int, 단조 증가 이벤트 시퀀스 번호) 속성이 포함됩니다.

### claude_code.hook_execution_start

Hook 실행이 시작된 이벤트입니다.

| 항목 | 값 |
|------|-----|
| **이벤트명** | `claude_code.hook_execution_start` |
| **SeverityText** | `INFO` |
| **설명** | Hook 실행 시작 이벤트 |

**이벤트 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 예시 |
|--------|------|------|------|------|
| `hook_name` | string | Y | Hook 이름 | `PreToolUse:Workflow` |
| `hook_event` | string | Y | Hook 라이프사이클 이벤트 | `PreToolUse`, `SessionStart` |
| `hook_source` | string | N | Hook 소스 | `merged`, `flagSettings` |
| `num_hooks` | int | N | 실행 대상 Hook 수 | `1` |

---

### claude_code.hook_execution_complete

Hook 실행이 완료된 이벤트입니다.

| 항목 | 값 |
|------|-----|
| **이벤트명** | `claude_code.hook_execution_complete` |
| **SeverityText** | `INFO` |
| **설명** | Hook 실행 완료 이벤트 |

**이벤트 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 예시 |
|--------|------|------|------|------|
| `hook_name` | string | Y | Hook 이름 | `PreToolUse:Workflow` |
| `hook_event` | string | Y | Hook 라이프사이클 이벤트 | `PreToolUse` |
| `hook_source` | string | N | Hook 소스 | `merged` |
| `total_duration_ms` | double | Y | 전체 Hook 실행 소요 시간 (ms) | `2` |
| `num_hooks` | int | Y | 실행 대상 Hook 수 | `1` |
| `num_success` | int | Y | 성공한 Hook 수 | `1` |
| `num_blocking` | int | Y | 차단(blocking) Hook 수 | `0` |
| `num_cancelled` | int | Y | 취소된 Hook 수 | `0` |
| `num_non_blocking_error` | int | Y | 비차단 오류 Hook 수 | `0` |

---

### claude_code.hook_registered

Hook이 등록된 이벤트입니다.

| 항목 | 값 |
|------|-----|
| **이벤트명** | `claude_code.hook_registered` |
| **SeverityText** | `INFO` |
| **설명** | Hook 등록 이벤트 |

**이벤트 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 예시 |
|--------|------|------|------|------|
| `hook_event` | string | Y | Hook 라이프사이클 이벤트 | `SessionStart` |
| `hook_source` | string | N | Hook 소스 | `flagSettings` |
| `hook_type` | string | Y | Hook 유형 | `command` |
| `plugin.name` | string | N | 등록 출처 플러그인 이름 | `workflow-plugin` |

---

### claude_code.plugin_loaded

플러그인이 로드된 이벤트입니다.

| 항목 | 값 |
|------|-----|
| **이벤트명** | `claude_code.plugin_loaded` |
| **SeverityText** | `INFO` |
| **설명** | 플러그인 로드 이벤트 |

**이벤트 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 예시 |
|--------|------|------|------|------|
| `plugin.name` | string | Y | 플러그인 이름 | `skill-creator` |
| `plugin.scope` | string | N | 플러그인 스코프 | `official` |
| `plugin.version` | string | N | 플러그인 버전 | `1.0.0` |
| `marketplace.name` | string | N | 마켓플레이스 이름 | `claude-plugins-official` |
| `enabled_via` | string | N | 활성화 경로 | `user-install` |
| `has_mcp` | boolean | N | MCP 제공 여부 | `false` |
| `has_hooks` | boolean | N | Hook 제공 여부 | `false` |

---

### claude_code.mcp_server_connection

MCP 서버 연결 이벤트입니다.

| 항목 | 값 |
|------|-----|
| **이벤트명** | `claude_code.mcp_server_connection` |
| **SeverityText** | `INFO` |
| **설명** | MCP 서버 연결 이벤트 |

**이벤트 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 예시 |
|--------|------|------|------|------|
| `status` | string | Y | 연결 상태 | `connected` |
| `transport_type` | string | Y | 전송 방식 | `stdio` |
| `server_scope` | string | N | 서버 스코프 | `dynamic` |
| `is_plugin` | boolean | N | 플러그인에서 제공된 서버 여부 | `true` |
| `plugin.name` | string | N | MCP 서버를 제공한 플러그인 이름 | `playwright` |
| `duration_ms` | double | N | 연결 소요 시간 (ms) | `6261` |

---

### claude_code.skill_activated

Skill이 활성화된 이벤트입니다.

| 항목 | 값 |
|------|-----|
| **이벤트명** | `claude_code.skill_activated` |
| **SeverityText** | `INFO` |
| **설명** | Skill 활성화 이벤트 |

**이벤트 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 예시 |
|--------|------|------|------|------|
| `skill.name` | string | Y | Skill 이름 | `frontend-design:frontend-design` |
| `skill.source` | string | N | Skill 소스 | `plugin` |
| `invocation_trigger` | string | N | Skill 호출 트리거 | `nested-skill` |
| `plugin.name` | string | N | Skill 제공 플러그인 이름 | `frontend-design` |
| `marketplace.name` | string | N | 마켓플레이스 이름 | `claude-plugins-official` |

---

### claude_code.subagent_completed

서브에이전트(Task 도구로 생성된 에이전트) 실행이 완료된 이벤트입니다. 서브에이전트별 토큰/도구 사용량 귀속 분석에 사용됩니다.

| 항목 | 값 |
|------|-----|
| **이벤트명** | `claude_code.subagent_completed` |
| **SeverityText** | `INFO` |
| **설명** | 서브에이전트 실행 완료 이벤트 |

**이벤트 고유 속성:**

| 속성명 | 타입 | 필수 | 설명 | 예시 |
|--------|------|------|------|------|
| `agent_type` | string | Y | 서브에이전트 유형 | `general-purpose`, `Explore` |
| `agent.source` | string | N | 서브에이전트 출처 | `built-in` |
| `is_built_in` | boolean | N | 빌트인 에이전트 여부 | `true` |
| `is_async` | boolean | N | 비동기 실행 여부 | `false` |
| `duration_ms` | int | Y | 실행 소요 시간 (ms) | `43335` |
| `total_tokens` | int | Y | 총 사용 토큰 수 | `26821` |
| `total_tool_uses` | int | Y | 총 도구 사용 횟수 | `6` |
| `model` | string | N | 사용된 모델 | `claude-opus-4-8` |

---

## 기존 이벤트 2.x 확장 속성

기존 이벤트(`api_request`, `api_error`, `tool_result`, `user_prompt`)에 2.x에서 신규 속성이 추가되었습니다.
또한 `tool_result`/`tool_decision`의 결정 관련 속성은 1.x의 `decision`/`source`에서 2.x의 `decision_type`/`decision_source`로 이름이 변경되었으며,
파이프라인은 양쪽 키를 모두 `decision`/`source` 컬럼으로 매핑합니다(하위 호환).

### api_request / api_error 신규 속성

| 속성명 | 타입 | 설명 | 예시 |
|--------|------|------|------|
| `agent.name` | string | Subagent 이름 (비어 있으면 메인 스레드) | `Explore`, `Plan`, `general-purpose` |
| `effort` | string | Effort 모드 | `high`, `xhigh` |
| `query_source` | string | 쿼리 소스 | `repl_main_thread` |
| `mcp_server.name` | string | 요청에 귀속된 MCP 서버 이름 (api_request) | `playwright` |
| `mcp_tool.name` | string | 요청에 귀속된 MCP 도구 이름 (api_request) | `browser_click` |
| `cost_usd_micros` | bigint | API 비용 (micro-USD, api_request) | `4102358` |
| `request_id` | string | API 요청 ID (api_error) | `req_abc123` |

### tool_result 신규 속성

| 속성명 | 타입 | 설명 | 예시 |
|--------|------|------|------|
| `decision_type` | string | 결정 유형 (1.x `decision` 대체, `decision` 컬럼으로 매핑) | `accept` |
| `decision_source` | string | 결정 주체 (1.x `source` 대체, `source` 컬럼으로 매핑) | `user` |
| `error_type` | string | 오류 카테고리 | `timeout` |
| `tool_input_size_bytes` | int | 도구 입력 크기 (bytes) | `240` |
| `tool_use_id` | string | 도구 호출 ID (tool_result, tool_decision) | `tu_1` |
| `mcp_server_scope` | string | MCP 서버 스코프 | `dynamic` |

### user_prompt 신규 속성

| 속성명 | 타입 | 설명 | 예시 |
|--------|------|------|------|
| `command_name` | string | 슬래시 커맨드 이름 | `effort` |
| `command_source` | string | 커맨드 소스 | `user` |

---

## 데이터 흐름 아키텍처

```
Developer PC (Claude Code)
    │
    │  OTLP (gRPC :4317 / HTTP :4318)
    ▼
NLB (Network Load Balancer)
    │
    ▼
ADOT Collector (ECS Fargate)
    ├── Metrics Pipeline ──→ prometheusremotewrite ──→ AMP (Amazon Managed Prometheus)
    │                                                       │
    │                                                       ▼
    │                                               Amazon Managed Grafana
    │
    └── Logs Pipeline ────→ awscloudwatchlogs ──→ CloudWatch Logs
                                                        │
                                                        ▼ (Subscription Filter)
                                                    Firehose + Lambda Transformer
                                                        │
                                                        ▼
                                                    S3 (Parquet)
                                                        │
                                                        ▼
                                                    Athena (SQL 분석)
                                                        │
                                                        ▼
                                                Amazon Managed Grafana
```

---

## 메트릭 스키마 요약 테이블

| 메트릭 이름 | 타입 | 단위 | 고유 속성 |
|-------------|------|------|-----------|
| `claude_code.session.count` | Counter | `{session}` | `start_type` (2.x) |
| `claude_code.lines_of_code.count` | Counter | `{line}` | `type` |
| `claude_code.pull_request.count` | Counter | `{pull_request}` | - |
| `claude_code.commit.count` | Counter | `{commit}` | - |
| `claude_code.cost.usage` | Counter | `USD` | `model` + 2.x 라벨 (`agent_name`, `effort`, `query_source`, `mcp_server_name`, `mcp_tool_name`, `skill_name`, `plugin_name`, `marketplace_name`) |
| `claude_code.token.usage` | Counter | `{token}` | `type`, `model` + 2.x 라벨 (`agent_name`, `effort`, `query_source`, `mcp_server_name`, `mcp_tool_name`, `skill_name`, `plugin_name`, `marketplace_name`) |
| `claude_code.code_edit_tool.decision` | Counter | `{decision}` | `tool`, `decision`, `language` |
| `claude_code.active_time.total` | Counter | `s` | - |

## 이벤트 스키마 요약 테이블

총 12종 이벤트 (1.x 5종 + 2.x 신규 7종).

| 이벤트명 | Severity | 주요 속성 |
|----------|----------|-----------|
| `claude_code.user_prompt` | INFO | `prompt_length`, `prompt`, `command_name` (2.x), `command_source` (2.x) |
| `claude_code.tool_result` | INFO | `tool_name`, `success`, `duration_ms`, `error`, `decision`, `source`, `tool_parameters`, `error_type` (2.x), `tool_input_size_bytes` (2.x), `tool_use_id` (2.x), `mcp_server_scope` (2.x) |
| `claude_code.api_request` | INFO | `model`, `cost_usd`, `duration_ms`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `agent.name` (2.x), `effort` (2.x), `query_source` (2.x), `mcp_server.name` (2.x), `mcp_tool.name` (2.x), `cost_usd_micros` (2.x) |
| `claude_code.api_error` | ERROR | `model`, `error`, `status_code`, `duration_ms`, `attempt`, `agent.name` (2.x), `effort` (2.x), `query_source` (2.x), `request_id` (2.x) |
| `claude_code.tool_decision` | INFO | `tool_name`, `decision`, `source`, `tool_use_id` (2.x) |
| `claude_code.hook_execution_start` (2.x) | INFO | `hook_name`, `hook_event`, `hook_source`, `num_hooks`, `event.sequence` |
| `claude_code.hook_execution_complete` (2.x) | INFO | `hook_name`, `hook_event`, `total_duration_ms`, `num_hooks`, `num_success`, `num_blocking`, `num_cancelled`, `num_non_blocking_error` |
| `claude_code.hook_registered` (2.x) | INFO | `hook_event`, `hook_source`, `hook_type`, `plugin.name` |
| `claude_code.plugin_loaded` (2.x) | INFO | `plugin.name`, `plugin.scope`, `plugin.version`, `marketplace.name`, `enabled_via`, `has_mcp`, `has_hooks` |
| `claude_code.mcp_server_connection` (2.x) | INFO | `status`, `transport_type`, `server_scope`, `is_plugin`, `plugin.name`, `duration_ms` |
| `claude_code.skill_activated` (2.x) | INFO | `skill.name`, `skill.source`, `invocation_trigger`, `plugin.name`, `marketplace.name` |
