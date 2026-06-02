# Claude Code 텔레메트리 데이터 스키마

## 목차

1. [개요](#개요)
2. [S3 버킷 구조](#s3-버킷-구조)
3. [Parquet 스키마](#parquet-스키마)
4. [Amazon Data Firehose 구성](#amazon-data-firehose-구성)
5. [AWS Glue Data Catalog](#aws-glue-data-catalog)
6. [Amazon Athena 쿼리 예시](#amazon-athena-쿼리-예시)
7. [데이터 보존 및 수명주기](#데이터-보존-및-수명주기)

---

## 개요

Claude Code는 OpenTelemetry 프로토콜을 통해 사용자 활동, 도구 실행, API 호출 등의 이벤트를 수집한다. 수집된 이벤트는 다음 파이프라인을 거쳐 분석 가능한 형태로 저장된다.

```
Claude Code (OTel SDK)
    ↓
ADOT Collector (수집/처리)
    ↓
Amazon Data Firehose (변환/전달)
    ↓
Amazon S3 (Parquet, 파티셔닝)
    ↓
Amazon Athena (SQL 쿼리 분석)
```

### 이벤트 유형

| 이벤트 이름 | 설명 |
|---|---|
| `claude_code.user_prompt` | 사용자가 프롬프트를 입력한 시점 |
| `claude_code.tool_result` | 도구 실행 결과 |
| `claude_code.api_request` | Claude API 호출 완료 |
| `claude_code.api_error` | Claude API 호출 오류 |
| `claude_code.tool_decision` | 도구 실행 승인/거부 결정 |
| `claude_code.hook_execution_start` | Hook 실행 시작 (2.x 신규) |
| `claude_code.hook_execution_complete` | Hook 실행 완료 (2.x 신규) |
| `claude_code.hook_registered` | Hook 등록 (2.x 신규) |
| `claude_code.plugin_loaded` | 플러그인 로드 (2.x 신규) |
| `claude_code.mcp_server_connection` | MCP 서버 연결 (2.x 신규) |
| `claude_code.skill_activated` | Skill 활성화 (2.x 신규) |
| `claude_code.subagent_completed` | 서브에이전트 실행 완료 (2.x 신규) |

> **2.x 스키마 업그레이드 주의**: 2.x 신규 이벤트와 신규 컬럼은 스키마 업그레이드 배포 이후에 기록된 데이터에만 채워집니다. 업그레이드 이전에 기록된 Parquet 파일을 조회하면 신규 컬럼은 모두 `NULL`로 반환됩니다(append-only, 하위 호환).

---

## S3 버킷 구조

### 버킷 이름

```
s3://claude-code-telemetry-events/
```

### 파티셔닝 전략

Hive 스타일 파티셔닝을 사용하여 시간 기반으로 데이터를 구성한다. 시간 단위(hour) 파티셔닝은 Firehose의 동적 파티셔닝을 통해 자동 생성된다.

```
s3://claude-code-telemetry-events/
  └── year=2026/
      └── month=02/
          └── day=19/
              └── hour=14/
                  ├── events-1-2026-02-19-14-00-00-a1b2c3d4.parquet
                  ├── events-1-2026-02-19-14-05-00-e5f6g7h8.parquet
                  └── ...
```

### 파티션 키 설명

| 파티션 키 | 형식 | 설명 |
|---|---|---|
| `year` | `YYYY` (예: `2026`) | 연도 |
| `month` | `MM` (예: `02`) | 월 (zero-padded) |
| `day` | `DD` (예: `19`) | 일 (zero-padded) |
| `hour` | `HH` (예: `14`) | 시간 (UTC, 24시간 형식, zero-padded) |

### 오류 레코드 경로

Firehose에서 Parquet 변환 실패 등으로 처리할 수 없는 레코드는 별도 경로에 저장된다.

```
s3://claude-code-telemetry-events/
  └── errors/
      └── year=2026/
          └── month=02/
              └── day=19/
                  └── hour=14/
                      └── error-1-2026-02-19-14-00-00-x9y8z7.json
```

### S3 접두사(Prefix) 구성

| 용도 | S3 접두사 |
|---|---|
| 정상 데이터 | `year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/` |
| 오류 데이터 | `errors/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/` |

---

## Parquet 스키마

모든 이벤트 유형을 하나의 통합(unified) 스키마로 관리한다. `event_name` 필드가 이벤트 유형 식별자(discriminator) 역할을 하며, 이벤트 유형에 따라 해당하지 않는 필드는 `null` 값을 갖는다.

### 전체 필드 정의

#### 공통 필드 (모든 이벤트)

| 필드 이름 | 데이터 타입 | Nullable | 설명 |
|---|---|---|---|
| `event_name` | `STRING` | No | 이벤트 유형 식별자 (예: `claude_code.user_prompt`) |
| `session_id` | `STRING` | No | 세션 고유 식별자 |
| `timestamp` | `TIMESTAMP` | No | 이벤트 발생 시각 (UTC, 밀리초 정밀도) |
| `organization_id` | `STRING` | Yes | 조직 식별자 |
| `user_id` | `STRING` | Yes | 사용자 식별자 (해시) |
| `user_name` | `STRING` | Yes | 사용자 이름 (리소스 속성에서 추출) |
| `terminal_type` | `STRING` | Yes | 터미널 유형 |

#### 리소스 속성 필드

| 필드 이름 | 데이터 타입 | Nullable | 설명 |
|---|---|---|---|
| `service_name` | `STRING` | Yes | 서비스 이름 (예: `claude-code`) |
| `service_version` | `STRING` | Yes | 서비스 버전 |
| `os_type` | `STRING` | Yes | 운영체제 유형 (예: `darwin`, `linux`) |
| `os_version` | `STRING` | Yes | 운영체제 버전 |
| `host_arch` | `STRING` | Yes | 호스트 아키텍처 (예: `arm64`, `x86_64`) |

#### 커스텀 리소스 속성 필드

| 필드 이름 | 데이터 타입 | Nullable | 설명 |
|---|---|---|---|
| `department` | `STRING` | Yes | 부서명 (`OTEL_RESOURCE_ATTRIBUTES`에서 주입) |
| `team_id` | `STRING` | Yes | 팀 식별자 |
| `cost_center` | `STRING` | Yes | 비용 센터 코드 |

#### 이벤트별 고유 필드

| 필드 이름 | 데이터 타입 | Nullable | 관련 이벤트 | 설명 |
|---|---|---|---|---|
| `prompt_length` | `INT` | Yes | `user_prompt` | 프롬프트 문자 길이 |
| `prompt_id` | `STRING` | Yes | `user_prompt` | 프롬프트 고유 식별자 |
| `tool_name` | `STRING` | Yes | `tool_result`, `tool_decision` | 도구 이름 |
| `success` | `BOOLEAN` | Yes | `tool_result` | 도구 실행 성공 여부 |
| `duration_ms` | `DOUBLE` | Yes | `tool_result`, `api_request`, `api_error` | 실행 소요 시간 (밀리초) |
| `error` | `STRING` | Yes | `tool_result`, `api_error` | 오류 메시지 |
| `decision` | `STRING` | Yes | `tool_result`, `tool_decision` | 도구 실행 결정 (`accept` / `reject`) |
| `source` | `STRING` | Yes | `tool_result`, `tool_decision` | 결정 출처 |
| `tool_parameters` | `STRING` | Yes | `tool_result` | 도구 파라미터 (JSON 문자열) |
| `tool_result_size_bytes` | `INT` | Yes | `tool_result` | 도구 실행 결과 크기 (바이트) |
| `model` | `STRING` | Yes | `api_request`, `api_error` | 사용된 모델 이름 |
| `speed` | `STRING` | Yes | `api_request` | API 응답 속도 모드 |
| `cost_usd` | `DOUBLE` | Yes | `api_request` | API 호출 비용 (USD) |
| `input_tokens` | `BIGINT` | Yes | `api_request` | 입력 토큰 수 |
| `output_tokens` | `BIGINT` | Yes | `api_request` | 출력 토큰 수 |
| `cache_read_tokens` | `BIGINT` | Yes | `api_request` | 캐시 읽기 토큰 수 |
| `cache_creation_tokens` | `BIGINT` | Yes | `api_request` | 캐시 생성 토큰 수 |
| `status_code` | `INT` | Yes | `api_error` | HTTP 상태 코드 |
| `attempt` | `INT` | Yes | `api_error` | 재시도 횟수 |

#### Claude Code 2.x 확장 필드 (append-only)

아래 38개 컬럼은 2.x 스키마 업그레이드 이후 수집된 데이터에만 채워지며, 이전 데이터에서는 `NULL`입니다.

| 필드 이름 | 데이터 타입 | Nullable | 관련 이벤트 | 설명 |
|---|---|---|---|---|
| `event_sequence` | `BIGINT` | Yes | (전체) | 단조 증가 이벤트 시퀀스 번호 |
| `agent_name` | `STRING` | Yes | `api_request`, `api_error` | Subagent 이름 (빈 값/NULL = 메인 스레드) |
| `effort` | `STRING` | Yes | `api_request`, `api_error` | Effort 모드 (`high`, `xhigh`) |
| `query_source` | `STRING` | Yes | `api_request`, `api_error` | 쿼리 소스 (`repl_main_thread`) |
| `mcp_server_name` | `STRING` | Yes | `api_request` | 요청에 귀속된 MCP 서버 이름 |
| `mcp_tool_name` | `STRING` | Yes | `api_request` | 요청에 귀속된 MCP 도구 이름 |
| `cost_usd_micros` | `BIGINT` | Yes | `api_request` | API 비용 (micro-USD) |
| `request_id` | `STRING` | Yes | `api_error` | API 요청 id |
| `error_type` | `STRING` | Yes | `tool_result` | 오류 카테고리 |
| `tool_input_size_bytes` | `INT` | Yes | `tool_result` | 도구 입력 크기 (bytes) |
| `tool_use_id` | `STRING` | Yes | `tool_result`, `tool_decision` | 도구 호출 id |
| `mcp_server_scope` | `STRING` | Yes | `tool_result` | MCP 서버 스코프 |
| `command_name` | `STRING` | Yes | `user_prompt` | 슬래시 커맨드 이름 |
| `command_source` | `STRING` | Yes | `user_prompt` | 커맨드 소스 |
| `hook_name` | `STRING` | Yes | `hook_execution_*` | Hook 이름 |
| `hook_event` | `STRING` | Yes | `hook_*` | Hook 라이프사이클 이벤트 (`PreToolUse`) |
| `hook_source` | `STRING` | Yes | `hook_*` | Hook 소스 (`merged`, `flagSettings`) |
| `hook_type` | `STRING` | Yes | `hook_registered` | Hook 유형 (`command`) |
| `total_duration_ms` | `DOUBLE` | Yes | `hook_execution_complete` | 전체 Hook 실행 소요 시간 (ms) |
| `num_hooks` | `INT` | Yes | `hook_execution_*` | Hook 수 |
| `num_success` | `INT` | Yes | `hook_execution_complete` | 성공한 Hook 수 |
| `num_blocking` | `INT` | Yes | `hook_execution_complete` | 차단 Hook 수 |
| `num_cancelled` | `INT` | Yes | `hook_execution_complete` | 취소된 Hook 수 |
| `num_non_blocking_error` | `INT` | Yes | `hook_execution_complete` | 비차단 Hook 오류 수 |
| `plugin_name` | `STRING` | Yes | `plugin_loaded`, `skill_activated`, `mcp_server_connection` | 플러그인 이름 |
| `plugin_scope` | `STRING` | Yes | `plugin_loaded` | 플러그인 스코프 (`official`) |
| `plugin_version` | `STRING` | Yes | `plugin_loaded` | 플러그인 버전 |
| `marketplace_name` | `STRING` | Yes | `plugin_loaded`, `skill_activated` | 마켓플레이스 이름 |
| `enabled_via` | `STRING` | Yes | `plugin_loaded` | 플러그인 활성화 경로 |
| `has_mcp` | `BOOLEAN` | Yes | `plugin_loaded` | 플러그인 MCP 제공 여부 |
| `has_hooks` | `BOOLEAN` | Yes | `plugin_loaded` | 플러그인 Hook 제공 여부 |
| `skill_name` | `STRING` | Yes | `skill_activated` | Skill 이름 |
| `skill_source` | `STRING` | Yes | `skill_activated` | Skill 소스 (`plugin`) |
| `invocation_trigger` | `STRING` | Yes | `skill_activated` | Skill 호출 트리거 |
| `transport_type` | `STRING` | Yes | `mcp_server_connection` | MCP 전송 방식 (`stdio`) |
| `mcp_status` | `STRING` | Yes | `mcp_server_connection` | MCP 연결 상태 (`connected`) |
| `server_scope` | `STRING` | Yes | `mcp_server_connection` | MCP 서버 스코프 |
| `is_plugin` | `BOOLEAN` | Yes | `mcp_server_connection` | MCP 서버가 플러그인 제공 여부 |
| `agent_type` | `STRING` | Yes | `subagent_completed` | 서브에이전트 유형 (예: `general-purpose`) |
| `agent_source` | `STRING` | Yes | `subagent_completed` | 서브에이전트 출처 (예: `built-in`) |
| `is_built_in` | `BOOLEAN` | Yes | `subagent_completed` | 빌트인 서브에이전트 여부 |
| `is_async` | `BOOLEAN` | Yes | `subagent_completed` | 비동기 실행 여부 |
| `total_tokens` | `BIGINT` | Yes | `subagent_completed` | 서브에이전트가 사용한 총 토큰 수 |
| `total_tool_uses` | `INT` | Yes | `subagent_completed` | 서브에이전트의 총 도구 사용 횟수 |

### 이벤트별 필드 매핑

아래 표는 각 이벤트 유형에서 값이 존재하는 고유 필드를 나타낸다 (공통 필드 제외).

| 필드 | `user_prompt` | `tool_result` | `api_request` | `api_error` | `tool_decision` |
|---|:---:|:---:|:---:|:---:|:---:|
| `prompt_length` | O | - | - | - | - |
| `prompt_id` | O | - | - | - | - |
| `tool_name` | - | O | - | - | O |
| `success` | - | O | - | - | - |
| `duration_ms` | - | O | O | O | - |
| `error` | - | O | - | O | - |
| `decision` | - | O | - | - | O |
| `source` | - | O | - | - | O |
| `tool_parameters` | - | O | - | - | - |
| `tool_result_size_bytes` | - | O | - | - | - |
| `model` | - | - | O | O | - |
| `speed` | - | - | O | - | - |
| `cost_usd` | - | - | O | - | - |
| `input_tokens` | - | - | O | - | - |
| `output_tokens` | - | - | O | - | - |
| `cache_read_tokens` | - | - | O | - | - |
| `cache_creation_tokens` | - | - | O | - | - |
| `status_code` | - | - | - | O | - |
| `attempt` | - | - | - | O | - |

#### 2.x 신규 이벤트별 필드 매핑

아래 표는 2.x 신규 이벤트 유형에서 값이 존재하는 고유 필드를 나타낸다 (공통 필드 제외). 모든 신규 이벤트에 `event_sequence`가 존재한다.

| 필드 | `hook_execution_start` | `hook_execution_complete` | `hook_registered` | `plugin_loaded` | `mcp_server_connection` | `skill_activated` |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| `event_sequence` | O | O | O | O | O | O |
| `hook_name` | O | O | - | - | - | - |
| `hook_event` | O | O | O | - | - | - |
| `hook_source` | O | O | O | - | - | - |
| `hook_type` | - | - | O | - | - | - |
| `total_duration_ms` | - | O | - | - | - | - |
| `num_hooks` | O | O | - | - | - | - |
| `num_success` | - | O | - | - | - | - |
| `num_blocking` | - | O | - | - | - | - |
| `num_cancelled` | - | O | - | - | - | - |
| `num_non_blocking_error` | - | O | - | - | - | - |
| `plugin_name` | - | - | O | O | O | O |
| `plugin_scope` | - | - | - | O | - | - |
| `plugin_version` | - | - | - | O | - | - |
| `marketplace_name` | - | - | - | O | - | O |
| `enabled_via` | - | - | - | O | - | - |
| `has_mcp` | - | - | - | O | - | - |
| `has_hooks` | - | - | - | O | - | - |
| `transport_type` | - | - | - | - | O | - |
| `mcp_status` | - | - | - | - | O | - |
| `server_scope` | - | - | - | - | O | - |
| `is_plugin` | - | - | - | - | O | - |
| `duration_ms` | - | - | - | - | O | - |
| `skill_name` | - | - | - | - | - | O |
| `skill_source` | - | - | - | - | - | O |
| `invocation_trigger` | - | - | - | - | - | O |

> 또한 기존 이벤트의 2.x 확장 필드: `api_request`/`api_error`에 `agent_name`, `effort`, `query_source`(및 `api_request`의 `mcp_server_name`, `mcp_tool_name`, `cost_usd_micros`, `api_error`의 `request_id`), `tool_result`에 `error_type`, `tool_input_size_bytes`, `tool_use_id`, `mcp_server_scope`, `tool_decision`에 `tool_use_id`, `user_prompt`에 `command_name`, `command_source`가 추가된다.

### Parquet 파일 속성

| 속성 | 값 |
|---|---|
| 압축 | Snappy |
| 행 그룹 크기 | 128 MB |
| 인코딩 | PLAIN / DICTIONARY (카디널리티 기반 자동 선택) |
| 파일 크기 (예상) | 64 ~ 128 MB |

---

## Amazon Data Firehose 구성

### 배달 스트림 개요

| 설정 항목 | 값 |
|---|---|
| 배달 스트림 이름 | `claude-code-telemetry-events-stream-prod` |
| 소스 | CloudWatch Logs Subscription Filter (ADOT → CW Logs → Firehose) |
| 대상 | Amazon S3 |
| S3 버킷 | `s3://claude-code-telemetry-events/` |

### 버퍼링 설정

| 설정 항목 | 값 | 설명 |
|---|---|---|
| 버퍼 크기 | 128 MB | 버퍼가 이 크기에 도달하면 S3로 전달 |
| 버퍼 간격 | 300초 (5분) | 버퍼 크기 미달 시 이 시간 경과 후 전달 |

버퍼 크기 또는 버퍼 간격 중 먼저 충족되는 조건에 따라 S3로 데이터가 전달된다.

### 형식 변환 설정

| 설정 항목 | 값 |
|---|---|
| 레코드 형식 변환 | 활성화 |
| 입력 형식 | JSON (OpenTelemetry JSON) |
| 출력 형식 | Apache Parquet |
| 압축 | Snappy |
| 스키마 소스 | AWS Glue Data Catalog |
| Glue 데이터베이스 | `claude_code_telemetry` |
| Glue 테이블 | `events` |

### 타임스탬프 기반 파티셔닝 설정

Firehose의 타임스탬프 기반 S3 접두사를 사용하여 시간 단위로 파티셔닝한다. 동적 파티셔닝(JQ 표현식 기반)은 사용하지 않으며, Firehose 내장 `!{timestamp:...}` 표현식으로 S3 경로를 구성한다.

```json
{
  "DynamicPartitioningConfiguration": {
    "Enabled": false
  },
  "Prefix": "year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/",
  "ErrorOutputPrefix": "errors/!{firehose:error-output-type}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/"
}
```

### IAM 역할 권한

Firehose 배달 스트림에 연결되는 IAM 역할에 필요한 주요 권한:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:AbortMultipartUpload",
        "s3:GetBucketLocation",
        "s3:GetObject",
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads",
        "s3:PutObject"
      ],
      "Resource": [
        "arn:aws:s3:::claude-code-telemetry-events",
        "arn:aws:s3:::claude-code-telemetry-events/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "glue:GetTable",
        "glue:GetTableVersion",
        "glue:GetTableVersions"
      ],
      "Resource": [
        "arn:aws:glue:*:*:catalog",
        "arn:aws:glue:*:*:database/claude_code_telemetry",
        "arn:aws:glue:*:*:table/claude_code_telemetry/events"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:PutLogEvents",
        "logs:CreateLogGroup",
        "logs:CreateLogStream"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    }
  ]
}
```

---

## AWS Glue Data Catalog

### 데이터베이스

| 설정 항목 | 값 |
|---|---|
| 데이터베이스 이름 | `claude_code_telemetry` |
| 설명 | Claude Code 텔레메트리 이벤트 데이터 |

### 테이블 정의

| 설정 항목 | 값 |
|---|---|
| 테이블 이름 | `events` |
| 분류 | `parquet` |
| 위치 | `s3://claude-code-telemetry-events/` |
| SerDe 라이브러리 | `org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe` |
| InputFormat | `org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat` |
| OutputFormat | `org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat` |

### 컬럼 정의 (DDL)

```sql
CREATE EXTERNAL TABLE claude_code_telemetry.events (
    -- 공통 필드
    event_name          STRING      COMMENT '이벤트 유형 식별자',
    session_id          STRING      COMMENT '세션 고유 식별자',
    `timestamp`         TIMESTAMP   COMMENT '이벤트 발생 시각 (UTC)',
    organization_id     STRING      COMMENT '조직 식별자',
    user_id             STRING      COMMENT '사용자 식별자 (해시)',
    user_name           STRING      COMMENT '사용자 이름',
    terminal_type       STRING      COMMENT '터미널 유형',

    -- 리소스 속성
    service_name        STRING      COMMENT '서비스 이름',
    service_version     STRING      COMMENT '서비스 버전',
    os_type             STRING      COMMENT '운영체제 유형',
    os_version          STRING      COMMENT '운영체제 버전',
    host_arch           STRING      COMMENT '호스트 아키텍처',

    -- 커스텀 리소스 속성
    department          STRING      COMMENT '부서명',
    team_id             STRING      COMMENT '팀 식별자',
    cost_center         STRING      COMMENT '비용 센터 코드',

    -- 이벤트별 고유 필드
    prompt_length       INT         COMMENT '프롬프트 문자 길이 (user_prompt)',
    prompt_id           STRING      COMMENT '프롬프트 고유 식별자',
    tool_name           STRING      COMMENT '도구 이름 (tool_result, tool_decision)',
    success             BOOLEAN     COMMENT '도구 실행 성공 여부 (tool_result)',
    duration_ms         DOUBLE      COMMENT '실행 소요 시간 ms (tool_result, api_request, api_error)',
    error               STRING      COMMENT '오류 메시지 (tool_result, api_error)',
    decision            STRING      COMMENT '도구 실행 결정 accept/reject (tool_result, tool_decision)',
    source              STRING      COMMENT '결정 출처 (tool_result, tool_decision)',
    tool_parameters     STRING      COMMENT '도구 파라미터 JSON 문자열 (tool_result)',
    tool_result_size_bytes INT      COMMENT '도구 실행 결과 크기 바이트 (tool_result)',
    model               STRING      COMMENT '모델 이름 (api_request, api_error)',
    speed               STRING      COMMENT 'API 응답 속도 모드 (api_request)',
    cost_usd            DOUBLE      COMMENT 'API 호출 비용 USD (api_request)',
    input_tokens        BIGINT      COMMENT '입력 토큰 수 (api_request)',
    output_tokens       BIGINT      COMMENT '출력 토큰 수 (api_request)',
    cache_read_tokens   BIGINT      COMMENT '캐시 읽기 토큰 수 (api_request)',
    cache_creation_tokens BIGINT    COMMENT '캐시 생성 토큰 수 (api_request)',
    status_code         INT         COMMENT 'HTTP 상태 코드 (api_error)',
    attempt             INT         COMMENT '재시도 횟수 (api_error)',

    -- Claude Code 2.x 확장 스키마 (live OTLP 2026-06 검증, append-only)
    -- 공통
    event_sequence      BIGINT      COMMENT '단조 증가 이벤트 시퀀스 번호',
    -- api_request / api_error: subagent, effort, MCP attribution
    agent_name          STRING      COMMENT 'Subagent 이름 (api_request, api_error)',
    effort              STRING      COMMENT 'Effort 모드 예: high/xhigh (api_request, api_error)',
    query_source        STRING      COMMENT '쿼리 소스 예: repl_main_thread (api_request, api_error)',
    mcp_server_name     STRING      COMMENT '요청에 귀속된 MCP 서버 이름 (api_request)',
    mcp_tool_name       STRING      COMMENT '요청에 귀속된 MCP 도구 이름 (api_request)',
    cost_usd_micros     BIGINT      COMMENT 'API 비용 micro-USD (api_request)',
    request_id          STRING      COMMENT 'API 요청 id (api_error)',
    -- tool_result 확장
    error_type          STRING      COMMENT '오류 카테고리 (tool_result)',
    tool_input_size_bytes INT       COMMENT '도구 입력 크기 bytes (tool_result)',
    tool_use_id         STRING      COMMENT '도구 호출 id (tool_result, tool_decision)',
    mcp_server_scope    STRING      COMMENT 'MCP 서버 스코프 (tool_result)',
    -- user_prompt 확장
    command_name        STRING      COMMENT '슬래시 커맨드 이름 (user_prompt)',
    command_source      STRING      COMMENT '커맨드 소스 (user_prompt)',
    -- hook_* 이벤트
    hook_name           STRING      COMMENT 'Hook 이름 (hook_execution_*)',
    hook_event          STRING      COMMENT 'Hook 라이프사이클 이벤트 예: PreToolUse (hook_*)',
    hook_source         STRING      COMMENT 'Hook 소스 예: merged/flagSettings (hook_*)',
    hook_type           STRING      COMMENT 'Hook 유형 예: command (hook_registered)',
    total_duration_ms   DOUBLE      COMMENT '전체 Hook 실행 소요 시간 ms (hook_execution_complete)',
    num_hooks           INT         COMMENT 'Hook 수 (hook_execution_*)',
    num_success         INT         COMMENT '성공한 Hook 수 (hook_execution_complete)',
    num_blocking        INT         COMMENT '차단 Hook 수 (hook_execution_complete)',
    num_cancelled       INT         COMMENT '취소된 Hook 수 (hook_execution_complete)',
    num_non_blocking_error INT      COMMENT '비차단 Hook 오류 수 (hook_execution_complete)',
    -- plugin_loaded / skill_activated / mcp_server_connection
    plugin_name         STRING      COMMENT '플러그인 이름 (plugin_loaded, skill_activated, mcp_server_connection)',
    plugin_scope        STRING      COMMENT '플러그인 스코프 예: official (plugin_loaded)',
    plugin_version      STRING      COMMENT '플러그인 버전 (plugin_loaded)',
    marketplace_name    STRING      COMMENT '마켓플레이스 이름 (plugin_loaded, skill_activated)',
    enabled_via         STRING      COMMENT '플러그인 활성화 경로 (plugin_loaded)',
    has_mcp             BOOLEAN     COMMENT '플러그인 MCP 제공 여부 (plugin_loaded)',
    has_hooks           BOOLEAN     COMMENT '플러그인 Hook 제공 여부 (plugin_loaded)',
    skill_name          STRING      COMMENT 'Skill 이름 (skill_activated)',
    skill_source        STRING      COMMENT 'Skill 소스 예: plugin (skill_activated)',
    invocation_trigger  STRING      COMMENT 'Skill 호출 트리거 (skill_activated)',
    transport_type      STRING      COMMENT 'MCP 전송 방식 예: stdio (mcp_server_connection)',
    mcp_status          STRING      COMMENT 'MCP 연결 상태 예: connected (mcp_server_connection)',
    server_scope        STRING      COMMENT 'MCP 서버 스코프 (mcp_server_connection)',
    is_plugin           BOOLEAN     COMMENT 'MCP 서버가 플러그인 제공 여부 (mcp_server_connection)',
    agent_type          STRING      COMMENT '서브에이전트 유형 (subagent_completed)',
    agent_source        STRING      COMMENT '서브에이전트 출처 (subagent_completed)',
    is_built_in         BOOLEAN     COMMENT '빌트인 서브에이전트 여부 (subagent_completed)',
    is_async            BOOLEAN     COMMENT '비동기 실행 여부 (subagent_completed)',
    total_tokens        BIGINT      COMMENT '서브에이전트 총 토큰 수 (subagent_completed)',
    total_tool_uses     INT         COMMENT '서브에이전트 총 도구 사용 횟수 (subagent_completed)'
)
PARTITIONED BY (
    year                STRING      COMMENT '연도 (YYYY)',
    month               STRING      COMMENT '월 (MM)',
    day                 STRING      COMMENT '일 (DD)',
    hour                STRING      COMMENT '시간 (HH, UTC)'
)
STORED AS PARQUET
LOCATION 's3://claude-code-telemetry-events/'
TBLPROPERTIES (
    'parquet.compression' = 'SNAPPY',
    'classification' = 'parquet',
    'has_encrypted_data' = 'false'
);
```

### 파티션 관리

새로운 파티션이 생성되면 Athena에서 쿼리하기 위해 Glue Data Catalog에 파티션을 등록해야 한다.

**방법 1: MSCK REPAIR TABLE (수동, 전체 스캔)**

```sql
MSCK REPAIR TABLE claude_code_telemetry.events;
```

**방법 2: ALTER TABLE ADD PARTITION (수동, 개별 파티션)**

```sql
ALTER TABLE claude_code_telemetry.events ADD IF NOT EXISTS
    PARTITION (year='2026', month='02', day='19', hour='14')
    LOCATION 's3://claude-code-telemetry-events/year=2026/month=02/day=19/hour=14/';
```

**방법 3: Glue Crawler (자동, 권장)**

주기적으로 S3 버킷을 스캔하여 새로운 파티션을 자동으로 등록하는 Glue Crawler를 구성한다.

| 설정 항목 | 값 |
|---|---|
| Crawler 이름 | `claude-code-telemetry-crawler` |
| 실행 주기 | 매시간 (cron: `0 * * * ? *`) |
| 데이터 소스 | `s3://claude-code-telemetry-events/` |
| 대상 데이터베이스 | `claude_code_telemetry` |
| 테이블 접두사 | (없음, 기존 `events` 테이블 업데이트) |

**방법 4: 파티션 프로젝션 (자동, 비용 효율적, 권장)**

Glue Crawler 없이 Athena가 파티션을 자동으로 추론하도록 파티션 프로젝션을 활성화할 수 있다.

```sql
ALTER TABLE claude_code_telemetry.events SET TBLPROPERTIES (
    'projection.enabled' = 'true',

    'projection.year.type' = 'integer',
    'projection.year.range' = '2026,2030',

    'projection.month.type' = 'integer',
    'projection.month.range' = '1,12',
    'projection.month.digits' = '2',

    'projection.day.type' = 'integer',
    'projection.day.range' = '1,31',
    'projection.day.digits' = '2',

    'projection.hour.type' = 'integer',
    'projection.hour.range' = '0,23',
    'projection.hour.digits' = '2',

    'storage.location.template' = 's3://claude-code-telemetry-events/year=${year}/month=${month}/day=${day}/hour=${hour}/'
);
```

파티션 프로젝션을 사용하면 `MSCK REPAIR TABLE`이나 Glue Crawler 없이도 즉시 모든 파티션에 쿼리할 수 있다. 대규모 파티션 수에서 Glue API 호출 비용 절감 효과가 크다.

---

## Amazon Athena 쿼리 예시

> **참고**: 아래 모든 쿼리에서 `year`, `month`, `day`, `hour` 파티션 필터를 사용하면 스캔 범위를 줄여 비용과 성능을 최적화할 수 있다.

### 1. 일별 사용자별 비용 합산

```sql
-- 일별 사용자별 API 호출 비용 합계
SELECT
    year,
    month,
    day,
    user_id,
    COUNT(*)                    AS api_call_count,
    ROUND(SUM(cost_usd), 4)    AS total_cost_usd,
    SUM(input_tokens)           AS total_input_tokens,
    SUM(output_tokens)          AS total_output_tokens
FROM claude_code_telemetry.events
WHERE event_name = 'claude_code.api_request'
  AND year = '2026'
  AND month = '02'
GROUP BY year, month, day, user_id
ORDER BY total_cost_usd DESC;
```

### 2. 도구별 사용 빈도 상위 10개

```sql
-- 가장 많이 사용된 도구 상위 10개
SELECT
    tool_name,
    COUNT(*)                                            AS usage_count,
    SUM(CASE WHEN success = true THEN 1 ELSE 0 END)    AS success_count,
    SUM(CASE WHEN success = false THEN 1 ELSE 0 END)   AS failure_count,
    ROUND(
        SUM(CASE WHEN success = true THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2
    )                                                   AS success_rate_pct
FROM claude_code_telemetry.events
WHERE event_name = 'claude_code.tool_result'
  AND year = '2026'
  AND month = '02'
GROUP BY tool_name
ORDER BY usage_count DESC
LIMIT 10;
```

### 3. 모델별 API 오류율

```sql
-- 모델별 API 요청 대비 오류 비율
WITH requests AS (
    SELECT
        model,
        COUNT(*) AS total_requests
    FROM claude_code_telemetry.events
    WHERE event_name = 'claude_code.api_request'
      AND year = '2026' AND month = '02'
    GROUP BY model
),
errors AS (
    SELECT
        model,
        COUNT(*)    AS total_errors,
        status_code,
        error
    FROM claude_code_telemetry.events
    WHERE event_name = 'claude_code.api_error'
      AND year = '2026' AND month = '02'
    GROUP BY model, status_code, error
),
error_summary AS (
    SELECT
        model,
        SUM(total_errors) AS total_errors
    FROM errors
    GROUP BY model
)
SELECT
    r.model,
    r.total_requests,
    COALESCE(e.total_errors, 0)                                         AS total_errors,
    ROUND(COALESCE(e.total_errors, 0) * 100.0 / r.total_requests, 2)   AS error_rate_pct
FROM requests r
LEFT JOIN error_summary e ON r.model = e.model
ORDER BY error_rate_pct DESC;
```

### 4. 도구 평균 실행 시간

```sql
-- 도구별 평균 실행 시간 (밀리초)
SELECT
    tool_name,
    COUNT(*)                        AS execution_count,
    ROUND(AVG(duration_ms), 2)      AS avg_duration_ms,
    ROUND(MIN(duration_ms), 2)      AS min_duration_ms,
    ROUND(MAX(duration_ms), 2)      AS max_duration_ms,
    ROUND(STDDEV(duration_ms), 2)   AS stddev_duration_ms
FROM claude_code_telemetry.events
WHERE event_name = 'claude_code.tool_result'
  AND year = '2026' AND month = '02'
  AND duration_ms IS NOT NULL
GROUP BY tool_name
ORDER BY avg_duration_ms DESC;
```

### 5. 팀별 토큰 사용량 추이

```sql
-- 일별 팀별 토큰 사용량 추이
SELECT
    year,
    month,
    day,
    team_id,
    SUM(input_tokens)           AS total_input_tokens,
    SUM(output_tokens)          AS total_output_tokens,
    SUM(cache_read_tokens)      AS total_cache_read_tokens,
    SUM(cache_creation_tokens)  AS total_cache_creation_tokens,
    SUM(input_tokens) + SUM(output_tokens)
        + SUM(cache_read_tokens) + SUM(cache_creation_tokens)
                                AS total_all_tokens
FROM claude_code_telemetry.events
WHERE event_name = 'claude_code.api_request'
  AND year = '2026' AND month = '02'
  AND team_id IS NOT NULL
GROUP BY year, month, day, team_id
ORDER BY year, month, day, team_id;
```

### 6. 캐시 적중률 분석

```sql
-- 일별 캐시 적중률 분석
-- cache_read_tokens > 0 이면 캐시가 활용된 것
SELECT
    year,
    month,
    day,
    COUNT(*)                                    AS total_api_calls,
    SUM(CASE
        WHEN cache_read_tokens > 0 THEN 1
        ELSE 0
    END)                                        AS cache_hit_calls,
    ROUND(
        SUM(CASE WHEN cache_read_tokens > 0 THEN 1 ELSE 0 END) * 100.0
        / COUNT(*), 2
    )                                           AS cache_hit_rate_pct,
    SUM(cache_read_tokens)                      AS total_cache_read_tokens,
    SUM(cache_creation_tokens)                  AS total_cache_creation_tokens,
    ROUND(
        SUM(cache_read_tokens) * 100.0
        / NULLIF(SUM(cache_read_tokens) + SUM(cache_creation_tokens), 0), 2
    )                                           AS cache_token_reuse_rate_pct
FROM claude_code_telemetry.events
WHERE event_name = 'claude_code.api_request'
  AND year = '2026' AND month = '02'
GROUP BY year, month, day
ORDER BY year, month, day;
```

### 7. 세션 분석 (세션별 지속 시간 및 활동량)

```sql
-- 세션별 지속 시간 및 활동 요약
SELECT
    session_id,
    user_id,
    MIN("timestamp")                                        AS session_start,
    MAX("timestamp")                                        AS session_end,
    DATE_DIFF('minute', MIN("timestamp"), MAX("timestamp")) AS session_duration_minutes,
    COUNT(*)                                                AS total_events,
    SUM(CASE WHEN event_name = 'claude_code.user_prompt'  THEN 1 ELSE 0 END) AS prompt_count,
    SUM(CASE WHEN event_name = 'claude_code.api_request'  THEN 1 ELSE 0 END) AS api_call_count,
    SUM(CASE WHEN event_name = 'claude_code.tool_result'  THEN 1 ELSE 0 END) AS tool_use_count,
    ROUND(SUM(CASE WHEN event_name = 'claude_code.api_request' THEN cost_usd ELSE 0 END), 4)
                                                            AS session_total_cost_usd
FROM claude_code_telemetry.events
WHERE year = '2026' AND month = '02' AND day = '19'
GROUP BY session_id, user_id
ORDER BY session_duration_minutes DESC;
```

### 8. 비용 이상 탐지 (2 표준편차 초과)

```sql
-- 일 평균 비용에서 2 표준편차를 초과하는 사용자 탐지
WITH daily_user_cost AS (
    SELECT
        user_id,
        year || '-' || month || '-' || day  AS date_str,
        SUM(cost_usd)                       AS daily_cost_usd
    FROM claude_code_telemetry.events
    WHERE event_name = 'claude_code.api_request'
      AND year = '2026' AND month = '02'
    GROUP BY user_id, year, month, day
),
user_stats AS (
    SELECT
        user_id,
        AVG(daily_cost_usd)     AS avg_daily_cost,
        STDDEV(daily_cost_usd)  AS stddev_daily_cost
    FROM daily_user_cost
    GROUP BY user_id
    HAVING COUNT(*) >= 3  -- 최소 3일 이상 데이터가 있는 사용자만
)
SELECT
    c.user_id,
    c.date_str,
    ROUND(c.daily_cost_usd, 4)     AS daily_cost_usd,
    ROUND(s.avg_daily_cost, 4)     AS avg_daily_cost,
    ROUND(s.stddev_daily_cost, 4)  AS stddev_daily_cost,
    ROUND(
        (c.daily_cost_usd - s.avg_daily_cost) / NULLIF(s.stddev_daily_cost, 0), 2
    )                              AS z_score
FROM daily_user_cost c
JOIN user_stats s ON c.user_id = s.user_id
WHERE c.daily_cost_usd > s.avg_daily_cost + 2 * s.stddev_daily_cost
ORDER BY z_score DESC;
```

### 9. 도구 승인/거부 비율

```sql
-- 도구별 승인(accept) vs 거부(reject) 비율
SELECT
    tool_name,
    COUNT(*)                                                AS total_decisions,
    SUM(CASE WHEN decision = 'accept' THEN 1 ELSE 0 END)   AS accept_count,
    SUM(CASE WHEN decision = 'reject' THEN 1 ELSE 0 END)   AS reject_count,
    ROUND(
        SUM(CASE WHEN decision = 'accept' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2
    )                                                       AS accept_rate_pct,
    ROUND(
        SUM(CASE WHEN decision = 'reject' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2
    )                                                       AS reject_rate_pct
FROM claude_code_telemetry.events
WHERE event_name = 'claude_code.tool_decision'
  AND year = '2026' AND month = '02'
GROUP BY tool_name
ORDER BY reject_rate_pct DESC;
```

### 10. API 응답 지연 시간 백분위수 (p50, p90, p99)

```sql
-- 모델별 API 응답 지연 시간 백분위수
SELECT
    model,
    COUNT(*)                                        AS request_count,
    ROUND(APPROX_PERCENTILE(duration_ms, 0.50), 2) AS p50_ms,
    ROUND(APPROX_PERCENTILE(duration_ms, 0.90), 2) AS p90_ms,
    ROUND(APPROX_PERCENTILE(duration_ms, 0.95), 2) AS p95_ms,
    ROUND(APPROX_PERCENTILE(duration_ms, 0.99), 2) AS p99_ms,
    ROUND(AVG(duration_ms), 2)                      AS avg_ms,
    ROUND(MAX(duration_ms), 2)                      AS max_ms
FROM claude_code_telemetry.events
WHERE event_name = 'claude_code.api_request'
  AND year = '2026' AND month = '02'
  AND duration_ms IS NOT NULL
GROUP BY model
ORDER BY p99_ms DESC;
```

### 11. 시간대별 사용 패턴 분석

```sql
-- 시간대별(UTC) 이벤트 발생 패턴 분석
SELECT
    hour                        AS hour_utc,
    COUNT(*)                    AS total_events,
    COUNT(DISTINCT session_id)  AS unique_sessions,
    COUNT(DISTINCT user_id) AS unique_users,
    SUM(CASE WHEN event_name = 'claude_code.user_prompt' THEN 1 ELSE 0 END)
                                AS prompt_count,
    SUM(CASE WHEN event_name = 'claude_code.api_request' THEN 1 ELSE 0 END)
                                AS api_request_count,
    ROUND(SUM(CASE WHEN event_name = 'claude_code.api_request' THEN cost_usd ELSE 0 END), 4)
                                AS total_cost_usd
FROM claude_code_telemetry.events
WHERE year = '2026' AND month = '02' AND day = '19'
GROUP BY hour
ORDER BY hour;
```

### 12. 부서별/비용 센터별 비용 요약 (관리 대시보드용)

```sql
-- 부서 및 비용 센터별 월간 비용 요약
SELECT
    department,
    cost_center,
    team_id,
    COUNT(DISTINCT user_id)   AS unique_users,
    COUNT(DISTINCT session_id)          AS unique_sessions,
    SUM(input_tokens)                   AS total_input_tokens,
    SUM(output_tokens)                  AS total_output_tokens,
    ROUND(SUM(cost_usd), 2)            AS total_cost_usd
FROM claude_code_telemetry.events
WHERE event_name = 'claude_code.api_request'
  AND year = '2026' AND month = '02'
GROUP BY department, cost_center, team_id
ORDER BY total_cost_usd DESC;
```

> **참고 (2.x 신규 컬럼)**: 아래 13~15번 쿼리는 2.x 확장 컬럼(`agent_name`, `effort`, `mcp_status`, `plugin_name` 등)을 사용한다. 이 컬럼들은 스키마 업그레이드 배포 이후 기록된 데이터에만 채워지므로, 그 이전 파티션을 조회하면 `NULL`로 반환된다.

### 13. Subagent별 비용 귀속 (2.x)

```sql
-- Subagent별 API 호출 비용/지연 시간 귀속 (agent_name NULL = 메인 스레드)
SELECT
    COALESCE(agent_name, '(main thread)') AS agent,
    COALESCE(effort, '-')                 AS effort,
    COUNT(*)                              AS api_calls,
    ROUND(SUM(cost_usd), 4)               AS total_cost_usd,
    ROUND(AVG(duration_ms), 0)            AS avg_latency_ms
FROM claude_code_telemetry.events
WHERE event_name = 'claude_code.api_request'
  AND year = '2026' AND month = '06'
GROUP BY agent_name, effort
ORDER BY total_cost_usd DESC;
```

### 14. MCP 서버 연결 성공률 (2.x)

```sql
-- MCP 서버별 연결 시도/성공/평균 연결 시간 및 전송 방식
SELECT
    COALESCE(plugin_name, '(standalone)') AS server,
    COUNT(*)                              AS attempts,
    SUM(CASE WHEN mcp_status = 'connected' THEN 1 ELSE 0 END) AS connected,
    ROUND(
        SUM(CASE WHEN mcp_status = 'connected' THEN 1 ELSE 0 END) * 100.0
        / NULLIF(COUNT(*), 0), 1
    )                                     AS success_pct,
    ROUND(AVG(duration_ms), 0)            AS avg_connect_ms,
    MAX(transport_type)                   AS transport,
    MAX(server_scope)                     AS scope
FROM claude_code_telemetry.events
WHERE event_name = 'claude_code.mcp_server_connection'
  AND year = '2026' AND month = '06'
GROUP BY plugin_name
ORDER BY attempts DESC;
```

### 15. 플러그인 채택 현황 (2.x)

```sql
-- 마켓플레이스별 로드된 플러그인 및 제공 기능(MCP/Hook) 매트릭스
SELECT
    plugin_name,
    MAX(plugin_scope)               AS scope,
    MAX(plugin_version)             AS version,
    COALESCE(MAX(marketplace_name), '(local)') AS marketplace,
    BOOL_OR(has_mcp)                AS has_mcp,
    BOOL_OR(has_hooks)              AS has_hooks,
    COUNT(DISTINCT session_id)      AS sessions
FROM claude_code_telemetry.events
WHERE event_name = 'claude_code.plugin_loaded'
  AND year = '2026' AND month = '06'
GROUP BY plugin_name
ORDER BY sessions DESC;
```

---

## 데이터 보존 및 수명주기

### S3 수명주기 정책

장기 비용 최적화를 위해 S3 수명주기 정책을 적용한다.

| 스토리지 클래스 전환 | 기간 | 설명 |
|---|---|---|
| S3 Standard | 0 ~ 90일 | 최근 데이터 (빈번한 쿼리 대상) |
| S3 Standard-IA | 90 ~ 365일 | 비교적 오래된 데이터 (간헐적 접근) |
| S3 Glacier Instant Retrieval | 365일 이후 | 아카이브 (드문 접근, 즉시 검색 가능) |
| 삭제 | 730일 (2년) 이후 | 보존 기간 만료 후 삭제 |

### 오류 레코드 수명주기

| 동작 | 기간 |
|---|---|
| 삭제 | 30일 이후 |

오류 레코드는 디버깅 용도이므로 30일 이후 자동 삭제한다.

### Athena 쿼리 결과 보존

Athena 쿼리 결과가 저장되는 S3 경로에도 수명주기 정책을 적용한다.

| 동작 | 기간 |
|---|---|
| 삭제 | 7일 이후 |
