# Claude Code 대시보드 가이드

이 문서는 Claude Code Observability Platform의 Grafana 대시보드 7종에 대한 상세 사용 가이드입니다.

---

## 목차

1. [대시보드 개요](#1-대시보드-개요)
2. [Overview (Prometheus + Athena)](#2-overview-prometheus--athena)
3. [Real-Time Metrics (Prometheus)](#3-real-time-metrics-prometheus)
4. [Cost Deep Analysis (Prometheus + Athena)](#4-cost-deep-analysis-prometheus--athena)
5. [Usage & Session Insights (Athena)](#5-usage--session-insights-athena)
6. [Tool Analytics (Athena)](#6-tool-analytics-athena)
7. [API Performance (Athena)](#7-api-performance-athena)
8. [Extensions & Agents (Prometheus + Athena)](#8-extensions--agents-prometheus--athena)
9. [템플릿 변수 사용법](#9-템플릿-변수-사용법)
10. [핵심 인사이트 해석 가이드](#10-핵심-인사이트-해석-가이드)
11. [FAQ](#11-faq)
12. [트러블슈팅](#12-트러블슈팅)

---

## 1. 대시보드 개요

### 1.1 설계 철학

이 플랫폼은 두 가지 데이터 소스의 **강점에 집중**하여 역할을 분리합니다:

- **Prometheus (AMP)**: 실시간 집계 메트릭 모니터링 + 정확한 비용 합산. 카운터와 비율(rate)을 30초 갱신으로 보여줌. 비용 관련 패널은 Prometheus 기반으로 새로고침 시 값 변동 없음
- **Athena (S3 + SQL)**: 이벤트 레벨 심층 분석. 개별 API 요청의 레이턴시, 에러 상세, 도구 결과 크기 등을 SQL로 쿼리. "왜 그런 일이 일어났는가?"에 답함

비용 관련 패널(Overview Top Users, Cost Deep Analysis)은 Prometheus+Athena 하이브리드: Prometheus가 정확한 합산값을, Athena가 유저명 매핑과 요청 단위 상세를 제공합니다.

### 1.2 대시보드 구성

Claude Code Observability Platform은 **7개 프로덕션 수준 대시보드, 총 91개 패널**(행 헤더 제외, Grafana JSON 전수 검증 기준)로 구성되어 있습니다. 각 대시보드의 패널 수는 행 헤더(row)를 제외하고 접힌 행 내부의 패널까지 모두 포함하여 집계했습니다. 게이지 패널, 스파크라인, 그라디언트 채움, 임계값 기반 색상, 테이블 셀 컬러링, 드릴다운 데이터 링크 등 운영 환경에 적합한 시각화를 제공합니다.

> **2.x 업데이트**: Claude Code 2.x 텔레메트리(Hooks, Plugins, MCP, Skills, Subagent, Effort)를 다루는 **Extensions & Agents** 대시보드(UID `claude-code-extensions`, 12 패널)가 신규 추가되었습니다. 또한 Overview의 **Error Rate** 패널은 존재하지 않는 이벤트 카운트 메트릭을 조회하던 버그를 Athena 쿼리(`api_error` vs `api_request` 비율)로 교체하여 수정했고, **Subagent Cost Share** KPI가 추가되어 Overview는 18 패널이 되었습니다.

**통합 대시보드 (Prometheus + Athena)**:

| 대시보드 | UID | 패널 수 | 데이터 소스 | 목적 |
|----------|-----|---------|-------------|------|
| **Overview** | `claude-code-overview` | 18 | Prometheus (AMP) + Athena | 핵심 KPI 통합 요약(스파크라인, 임계값 색상), 모든 대시보드의 진입점. Error Rate(Athena) + Subagent Cost Share(AMP) 포함 |
| **Extensions & Agents** | `claude-code-extensions` | 12 | Prometheus (AMP) + Athena | Subagent/Effort 비용 귀속(AMP), MCP 서버/도구 분석 및 Plugin/Skill 채택 현황(Athena). 2.x 텔레메트리 전용 |

**Prometheus(AMP) 기반 대시보드 (실시간 메트릭)**:

| 대시보드 | UID | 패널 수 | 데이터 소스 | 목적 |
|----------|-----|---------|-------------|------|
| **Real-Time Metrics** | `claude-code-realtime` | 18 | Prometheus (AMP) | 게이지(캐시 히트율/수락률), 스파크라인, 그라디언트 채움, 드릴다운 링크. 30초 자동 새로고침 |

**하이브리드 대시보드 (Prometheus + Athena)**:

| 대시보드 | UID | 패널 수 | 데이터 소스 | 목적 |
|----------|-----|---------|-------------|------|
| **Cost Deep Analysis** | `claude-code-cost` | 10 | Prometheus + Athena | 모델별 비용 트렌드(Prometheus), 비용 분포 파이차트, 유저별 비용 귀속(하이브리드), KPI/캐시 효율(Athena) |

**Athena 기반 대시보드 (이벤트 심층 분석)**:

| 대시보드 | UID | 패널 수 | 데이터 소스 | 목적 |
|----------|-----|---------|-------------|------|
| **Usage & Session Insights** | `claude-code-usage` | 10 | Athena | 테이블 셀 컬러링, 세션 흐름, 프롬프트 복잡도, 사용 패턴 심층 분석 |
| **Tool Analytics** | `claude-code-tools` | 12 | Athena | 게이지(성공률/수락률), 그라디언트 바 차트, 테이블 셀 컬러링, 에러 패턴 |
| **API Performance** | `claude-code-api` | 13 | Athena | 게이지(에러율), 그라디언트 채움, 임계값 라인, 테이블 셀 컬러링, 에러 분석 |

### 1.3 대시보드 관계

```
Overview (Prometheus + Athena) <--- 통합 진입점, 핵심 KPI 요약
    |                                 "전체 상황을 한눈에 파악"
    |
    | 네비게이션 링크 (상세 분석으로 드릴다운)
    |
    +----> Real-Time Metrics (Prometheus/AMP) <--- 실시간 운영 모니터링 (30초 갱신)
    |          "지금 무슨 일이 일어나고 있는가?"
    |
    +----> Athena 기반 심층 분석 대시보드 <--- "왜 그런 일이 일어났는가?"
              |
              +-- 비용 심층 ----> Cost Deep Analysis
              |                      (요청 단위 비용, 캐시 절감, 모델 비교)
              |
              +-- 사용 패턴 ----> Usage & Session Insights
              |                      (세션 흐름, 프롬프트 복잡도, 버전 분포)
              |
              +-- 도구 분석 ----> Tool Analytics
              |                      (도구 성능, 승인/거부, 에러)
              |
              +-- API 성능 ----> API Performance
                                    (레이턴시, 처리량, 캐시, 에러)
```

모든 대시보드 상단에 다른 대시보드로의 양방향 네비게이션 링크가 있어, 빠르게 이동할 수 있습니다. Overview 대시보드가 전체 대시보드의 진입점 역할을 하며, 핵심 KPI를 확인한 후 상세 분석이 필요한 대시보드로 드릴다운하는 것이 일반적인 워크플로우입니다.

### 1.4 데이터 파이프라인

두 개의 독립적인 파이프라인이 동작합니다:

```
[메트릭 파이프라인 - 실시간]
Claude Code (OTel SDK, cumulative) --> ADOT Collector
    --> Prometheus Remote Write (SigV4) --> AMP
    --> PromQL --> Grafana (Real-Time Metrics 대시보드, 30초 갱신)

[이벤트 파이프라인 - 배치]
Claude Code (OTel SDK) --> ADOT Collector --> CloudWatch Logs
    --> Kinesis Data Firehose --> Lambda (변환) --> S3 (Parquet)
    --> Glue Catalog --> Athena --> Grafana (Athena 대시보드 4종)
```

이벤트 데이터는 S3에 `year=YYYY/month=MM/day=DD/hour=HH/` 구조로 파티셔닝되어 저장되며, S3 ObjectCreated 이벤트 -> EventBridge -> Lambda -> Glue `BatchCreatePartition`으로 수 초 내 파티션이 자동 등록됩니다.

> **주의**: 메트릭 파이프라인은 클라이언트에서 **cumulative temporality**를 사용해야 합니다. `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta` 설정 시 메트릭이 AMP에 도달하지 않습니다.

---

## 2. Overview (Prometheus + Athena)

**목적**: Prometheus 실시간 메트릭과 Athena 이벤트 심층 분석을 통합한 Executive Summary 대시보드. 핵심 KPI를 한눈에 파악하고, 모든 대시보드로의 네비게이션 진입점 역할을 합니다.

**UID**: `claude-code-overview` | **패널 수**: 18 | **데이터 소스**: Prometheus (AMP) + Athena

### 2.1 패널 구성

**Prometheus 기반 패널 (12개)** - 실시간 집계 메트릭:

| 패널 | 시각화 | 데이터 소스 | 설명 |
|------|--------|-------------|------|
| **Total Cost** | Stat (임계값 색상) | Prometheus | 총 비용. 임계값 기반 초록/노란/빨간 색상 |
| **Lines Changed** | Stat | Prometheus | 코드 변경 라인 수(추가+삭제) |
| **Subagent Cost Share** | Stat | Prometheus | Subagent(`agent_name` 라벨이 있는) 비용이 전체 비용에서 차지하는 비중. 2.x 신규 KPI |
| **Total Tokens** | Stat (임계값 색상) | Prometheus | 총 토큰 사용량. 임계값 기반 색상 |
| **Input Tokens** | Stat | Prometheus | 총 입력 토큰 수 |
| **Output Tokens** | Stat | Prometheus | 총 출력 토큰 수 |
| **Cache Tokens** | Stat | Prometheus | 캐시 토큰 수(읽기+생성) |
| **Cost per Minute by Model** | Time Series | Prometheus | 모델별 분당 비용 증가율 추이 |
| **Tokens per Minute by Model** | Time Series | Prometheus | 모델별 분당 토큰 사용률 추이 |
| **Token I/O (Input & Output)** | Time Series | Prometheus | 입력/출력 토큰 추이 |
| **Cache Token Usage (Read & Write)** | Time Series | Prometheus | 캐시 읽기/생성 토큰 추이 |
| **Lines of Code (Added vs Removed)** | Time Series | Prometheus | 코드 변경량(추가/삭제) 추이 |

**Athena 기반 패널 (6개)** - 이벤트 심층 분석:

| 패널 | 시각화 | 데이터 소스 | 설명 |
|------|--------|-------------|------|
| **Error Rate** | Stat (임계값 색상) | Athena | API 에러율(`api_error` vs `api_request` 비율). 기존 존재하지 않던 이벤트 카운트 메트릭 버그를 Athena 쿼리로 교체 |
| **Hourly Event Flow** | Time Series | Athena | 시간대별 이벤트 발생 추이 |
| **Top Users by Cost** | Table | Athena | 비용 상위 사용자 목록 |
| **Top 10 Tools** | Bar Chart | Athena | 가장 많이 사용된 도구 순위 |
| **API Latency p50/p90** | Time Series | Athena | API 레이턴시 백분위수 추이 (Details 행, 기본 접힘) |
| **Recent Sessions** | Table | Athena | 최근 세션 목록 (세션ID, 사용자, 비용, 도구 사용 수) (Details 행, 기본 접힘) |

### 2.2 템플릿 변수

Overview 대시보드는 두 데이터 소스의 변수를 모두 사용합니다:

| 변수 | 데이터 소스 | 설명 |
|------|-------------|------|
| `$prometheus_datasource` | Prometheus | Prometheus(AMP) 데이터 소스 선택 |
| `$athena_datasource` | Athena | Athena 데이터 소스 선택 |
| `$organization_id` | Prometheus | 조직 ID 필터 |
| `$user_id` | Prometheus | 사용자 ID 필터 |

### 2.3 주요 사용 시나리오

- **일일 현황 파악**: 핵심 KPI(세션, 비용, 토큰, 활성시간)를 한눈에 확인
- **비용/성능 트렌드**: 비용 추이와 API 레이턴시를 동시에 모니터링
- **드릴다운 진입점**: 이상 징후 발견 시 네비게이션 링크를 통해 상세 대시보드로 이동
- **팀 리더/관리자 뷰**: 전체 사용 현황을 요약 수준에서 빠르게 파악

---

## 3. Real-Time Metrics (Prometheus)

**목적**: AMP(Amazon Managed Prometheus)에 저장된 8종 메트릭을 PromQL로 실시간 조회하는 운영 대시보드. 실시간 카운터와 비율(rate) 모니터링에 특화.

**UID**: `claude-code-realtime` | **패널 수**: 18 | **데이터 소스**: Prometheus (AMP) | **새로고침**: 30초

### 3.1 Athena 대시보드와의 차이

| 항목 | Real-Time Metrics | Athena 대시보드 (4종) |
|------|-------------------|----------------------|
| 데이터 소스 | AMP (Prometheus) | Amazon Athena |
| 쿼리 언어 | PromQL | SQL |
| 갱신 주기 | 30초 (실시간) | 5~10분 (Firehose 버퍼링) |
| 데이터 유형 | 집계 메트릭 (카운터 8종) | 개별 이벤트 (5종 이벤트 레코드) |
| 용도 | 실시간 운영 모니터링, 이상 감지 | 이벤트 레벨 심층 분석 |
| 예시 | "현재 세션 수", "비용 증가율" | "특정 API 요청의 p99 레이턴시", "도구 에러 상세" |

### 3.2 필터 변수

| 변수 | 라벨 | 설명 |
|------|------|------|
| `$organization_id` | Organization | 조직 ID 필터 |
| `$user_id` | User | 사용자 ID 필터 |
| `$model` | Model | 모델 필터 (비용/토큰 메트릭에 적용) |

### 3.3 패널 구성

#### Row 1: 핵심 카운터 (Stat 패널 6개)

| 패널 | 시각화 | PromQL 예시 | 설명 |
|------|--------|-------------|------|
| **Total Sessions** | Stat (스파크라인, 임계값) | `sum(claude_code_session_count{...})` | 총 세션 수. 스파크라인 추이, 임계값 색상(50/200) |
| **Total Cost (USD)** | Stat (스파크라인, 임계값) | `sum(claude_code_cost_usage{...})` | 총 비용. $100 이하 초록, $500 이상 빨간 |
| **Total Tokens** | Stat (스파크라인, 임계값) | `sum(claude_code_token_usage{...})` | 총 토큰 수. 임계값(1M/5M) 기반 색상 |
| **Total Commits** | Stat (스파크라인) | `sum(claude_code_commit_count{...})` | 총 커밋 수 |
| **Pull Requests** | Stat (스파크라인) | `sum(claude_code_pull_request_count{...})` | 총 PR 수 |
| **Active Time** | Stat (스파크라인, 임계값) | `sum(claude_code_active_time{...})` | 총 활성 시간. 임계값(1h/8h) 기반 색상 |

#### Row 1.5: 게이지 패널 (2개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Cache Hit Ratio** | Gauge (임계값) | 캐시 히트율. 빨간(<30%) / 노란(30-60%) / 초록(>60%). 프롬프트 캐싱 효율성 모니터링 |
| **Edit Acceptance Rate** | Gauge (임계값) | 코드 편집 수락률. 빨간(<60%) / 노란(60-80%) / 초록(>80%). AI 코드 품질 지표 |

#### Row 2: Rate 시계열 (2개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Session Rate** | Time Series (그라디언트) | 시간당 세션 생성 속도 (`rate()` 함수 사용). 그라디언트 채움, 축 라벨 |
| **Cost Accumulation Rate** | Time Series (그라디언트) | 시간당 비용 증가 속도. 모델별 분리 표시. 비용 대시보드 드릴다운 링크 |

#### Row 3: 토큰 & 코드 (2개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Token Usage by Type** | Time Series (Stacked, 그라디언트) | 토큰 유형별(input/output/cacheRead/cacheCreation) 사용 추이. 그라디언트 채움, 축 라벨 |
| **Lines of Code Changed** | Time Series (그라디언트) | 코드 변경량 (added/removed) 추이. 추가=초록, 삭제=빨간. 그라디언트 채움 |

#### Row 4: 생산성 (2개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Commits & PRs** | Time Series (그라디언트) | 커밋/PR 생성 추이. 그라디언트 채움 적용. 세션 대시보드 드릴다운 링크 |
| **Active Time** | Time Series (그라디언트) | 활성 시간 (user/cli) 추이 |

#### Row 5: 도구 결정 (3개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Tool Decisions by Type** | Time Series (Stacked, 그라디언트) | 도구 결정(accept/reject) 추이. 그라디언트 채움 |
| **Tool Decision Source** | Pie Chart | 결정 소스(config/user/auto) 비율 |
| **Edit Decision Trend** | Time Series (그라디언트) | 편집 결정 추이. accept=초록/reject=빨간. 그라디언트 채움 |

#### Row 6: 비용 상세 및 언어 (3개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Cost by Model** | Pie Chart | 모델별 비용 비중 |
| **Top Users by Cost** | Bar Gauge | 비용 상위 사용자 순위 |
| **Top Languages** | Bar Gauge (그라디언트) | 프로그래밍 언어별 코드 편집 결정 순위 |

### 3.4 주요 사용 시나리오

- **실시간 활동 모니터링**: 현재 세션 수, 비용 증가율을 30초 단위로 확인
- **이상 감지**: 비용 급증, 세션 수 급감 등 이상 패턴을 실시간으로 감지
- **모델 비용 추적**: Opus vs Sonnet 비용 비중을 실시간으로 모니터링
- **팀 생산성 현황**: 커밋, PR, 코드 변경량을 실시간으로 확인
- **Athena 대시보드로 드릴다운**: 이상 감지 시 네비게이션 링크를 통해 상세 이벤트 분석으로 전환

### 3.5 메트릭이 표시되지 않는 경우

Real-Time Metrics 대시보드에 데이터가 표시되지 않는 경우:

1. **클라이언트 temporality 확인**: `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE`가 `delta`로 설정되어 있으면 메트릭이 AMP에 도달하지 않습니다. `cumulative`로 변경하거나 이 환경변수를 제거하세요.
2. **Grafana 데이터 소스 확인**: Prometheus 데이터 소스의 UID가 `prometheus`인지 확인
3. **AMP 연결 확인**: Grafana > Configuration > Data Sources에서 Prometheus 데이터 소스가 정상 연결되어 있는지 확인
4. **시간 범위 확인**: 메트릭 수집 시작 이후의 시간 범위가 선택되어 있는지 확인

---

## 4. Cost Deep Analysis (Athena)

**목적**: 개별 API 요청 레벨에서의 비용 심층 분석. 실시간 비용 현황은 Real-Time Metrics에서, 비용의 근본 원인 분석은 이 대시보드에서 수행.

**UID**: `claude-code-cost` | **패널 수**: 10 | **필터**: team, user, model

### 4.1 패널 구성

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Avg Cost / Prompt** | Stat (임계값 색상) | 프롬프트당 평균 비용. 초록(<$0.05), 노란($0.05-$0.15), 빨간(>$0.15) |
| **Avg Cost / API Call** | Stat (임계값 색상) | API 호출당 평균 비용. 임계값 기반 색상 |
| **Cost per 1K Output Tokens** | Stat (임계값 색상) | 1K 출력 토큰당 비용. 모델 비용 효율 비교 지표 |
| **Model Cost Efficiency Comparison** | Table (셀 컬러링, 게이지) | 모델별 호출 수, 총 비용, 호출당 비용, 비용 비중 비교. `cost_share_pct` 컬럼에 게이지 셀, `total_cost`에 배경 그라디언트 |
| **Speed Mode Cost Comparison** | Bar Chart | 속도 모드(normal/fast)별 비용 비교. 필드별 단위 적용 |
| **Cost per Prompt Trend** | Time Series (임계값 라인) | 프롬프트당 비용 추이. $0.15 예산 임계값 빨간 라인, 시리즈별 고유 색상/스타일 |
| **User Cost Detail** | Table (셀 컬러링) | 사용자별 세션 수, 총 비용, 세션당 비용 상세. `total_cost_usd`에 배경 그라디언트, 토큰 포맷팅 |
| **Cache Efficiency & Cost Savings** | Stat (임계값 색상) | 캐시 효율성 및 절감 효과. 캐시 히트율 임계값(빨간<30%/노란/초록>60%) |
| **Error Cost Waste** | Stat (임계값 색상) | 에러로 인한 비용 낭비. 실패한 API 요청의 총 비용 표시 |

### 4.2 주요 사용 시나리오

- **비용 이상치 식별**: Cost per Request Distribution에서 비정상적으로 비싼 요청 식별
- **캐시 절감 효과 모니터링**: Cache Reuse Rate 게이지로 캐시 활용도 확인
- **예산 소진 추적**: Cumulative Cost Trend로 시간 경과에 따른 누적 비용 확인
- **사용자별 비용 관리**: User Cost Detail 테이블에서 비용 배분 데이터 확인
- **프롬프트당 비용 분석**: Cross-event 조인으로 프롬프트 하나가 유발하는 실제 비용 파악

---

## 5. Usage & Session Insights (Athena)

**목적**: 세션 흐름과 사용 패턴의 심층 분석. 실시간 사용량 현황은 Real-Time Metrics에서, 사용 패턴의 근본 분석은 이 대시보드에서 수행.

**UID**: `claude-code-usage` | **패널 수**: 10 | **필터**: team, user, model

### 5.1 패널 구성

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Event Distribution** | Pie Chart (퍼센트 라벨) | 이벤트 유형별 분포. 슬라이스에 퍼센트 라벨 표시, 멀티 툴팁 |
| **Hourly Activity Pattern** | Bar Chart (색상 분리) | 시간대(UTC 0-23시)별 이벤트/세션/사용자 수. 시리즈별 고유 색상(파란/초록/보라) |
| **Prompt Length Distribution** | Bar Chart (연속 블루 그라디언트) | 프롬프트 길이 구간별 분포. 길이에 따라 연속 블루 그라디언트 색상 |
| **Session Complexity Overview** | Table (셀 컬러링) | 세션별 비용/지속시간에 배경 그라디언트. 비용>$0.5 노란, >$2 빨간. 지속시간>10분 노란, >1시간 빨간 |
| **Session Activity Summary** | Table (셀 컬러링) | 최근 50개 세션 상세. 비용/지속시간/토큰에 배경 그라디언트, 토큰 포맷팅 |
| **Session Flow Pattern** | Time Series (우측 축 에러 바) | 시간대별 이벤트 흐름. 에러를 우측 축 바 차트로 분리하여 소량 에러도 가시화 |
| **Prompt Complexity vs Cost** | Table (게이지 셀) | 프롬프트 복잡도별 비용. `avg_session_cost` 컬럼에 게이지 셀로 비용 스케일링 시각화 |
| **Terminal & OS Distribution** | Pie Chart | 터미널 타입(ghostty, vscode 등)과 OS 분포 |
| **Version Distribution** | Bar Chart | Claude Code 서비스 버전별 세션 수 분포 |
| **Top 10 Users by Sessions** | Bar Chart | 세션 수 기준 상위 10명 사용자 |

### 5.2 주요 사용 시나리오

- **세션 복잡도 분석**: Session Complexity에서 세션별 프롬프트-API-도구 체인 분석
- **모델 역할 이해**: Model Role Pattern에서 Sonnet(라우터)과 Opus(생성자)의 I/O 비율 확인
- **프롬프트 최적화**: Prompt Length Distribution에서 프롬프트 길이 분포 확인
- **사용 패턴 분석**: Hourly Activity Pattern으로 팀의 시간대별 사용 패턴 파악
- **버전 현황 추적**: Version Distribution에서 팀 전체의 Claude Code 버전 분포 확인

---

## 6. Tool Analytics (Athena)

**목적**: Claude Code가 사용하는 도구(Bash, Read, Write, Edit, Grep 등)의 사용 패턴과 성능 분석. 이벤트 레벨에서 도구 실행 시간, 결과 크기, 에러 상세를 분석.

**UID**: `claude-code-tools` | **패널 수**: 12 | **필터**: team, user, tool

### 6.1 패널 구성

#### Row 1: 도구 KPI (Stat 2개 + Gauge 2개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Total Tool Executions** | Stat (파란색, 스파크라인) | 총 도구 실행 횟수. 드릴다운 링크 포함 |
| **Success Rate** | Gauge (임계값) | 전체 도구 성공률. 게이지 아크로 시각화. 95% 이상 초록, 80-95% 노란색, 80% 미만 빨간색 |
| **Accept Rate** | Gauge (임계값) | 도구 승인률. 게이지 아크로 시각화. 90% 이상 초록, 70-90% 노란색, 70% 미만 빨간색 |
| **Unique Tools** | Stat (보라색) | 사용된 고유 도구 종류 수 |

#### Row 2: 도구 사용 빈도 (2개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Tool Usage Frequency** | Bar Chart (수평, Stacked, 그라디언트) | 도구별 성공/실패 횟수. 초록(성공), 빨간(실패). 그라디언트 채움, 퍼센트 툴팁 |
| **Decision by Tool and Source** | Bar Chart (Stacked, 그라디언트) | 도구별 의사결정(accept/reject) 및 소스(config/user/auto) 분포. 그라디언트 채움 |

#### Row 3: 도구 성능 (2개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Tool Performance Summary** | Table (셀 컬러링) | 도구별 실행 수, 성공률, 평균/p95/최대 실행 시간, 평균 결과 크기. `success_rate_pct`에 게이지 셀 컬러링 |
| **Decision Source Distribution** | Pie Chart (의미적 색상) | 의사결정 소스별 비율. config=파란, user=초록, auto=주황, unknown=회색 |

#### Row 4: 시계열 및 크기 분석 (2개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Tool Execution Time Trend** | Time Series (그라디언트 채움) | 시간대별 도구 실행 시간 추이. 그라디언트 채움, 임계값 기반 영역 스타일 |
| **Tool Result Size by Tool** | Bar Chart (그라디언트) | 도구별 평균 결과 크기(bytes). 그라디언트 채움, avg=파란/max=주황 |

#### Row 5: 추이 및 에러 (2개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Success Rate Trend** | Time Series (임계값 영역) | 시간대별 성공률 추이. 임계값 영역 스타일로 95% 기준선 표시. **신규 패널** |
| **Recent Tool Errors** | Table | 최근 도구 에러 목록. 타임스탬프, 사용자, 도구명, 실행시간, 에러 메시지 |

### 6.2 주요 사용 시나리오

- **도구 안정성 모니터링**: Success Rate과 Tool Usage Frequency에서 실패 비율 확인
- **보안 감사**: Decision by Tool and Source에서 자동 승인 vs 수동 승인 비율 확인
- **성능 병목 식별**: Tool Performance Summary에서 실행 시간이 긴 도구 확인
- **에러 원인 분석**: Recent Tool Errors에서 실패한 도구의 에러 메시지 확인

---

## 7. API Performance (Athena)

**목적**: Claude API 호출 성능, 오류율, 캐시 효과의 이벤트 레벨 분석. 레이턴시 백분위수, 에러 상세 등 Prometheus에서는 제공할 수 없는 심층 성능 데이터를 분석.

**UID**: `claude-code-api` | **패널 수**: 13 | **필터**: model, team, user

### 7.1 패널 구성

#### Row 1: 성능 KPI (Stat 3개 + Gauge 1개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Avg Latency** | Stat (임계값 색상) | 평균 API 응답 시간(초). 2초 이하 초록, 2-5초 노란색, 5초 이상 빨간색. 임계값 기반 배경색 |
| **Error Rate** | Gauge (임계값) | API 에러율. 게이지 아크로 시각화. 1% 이하 초록, 1-5% 노란색, 5% 이상 빨간색 |
| **Total API Calls** | Stat (파란색) | 총 API 호출 수 |
| **Avg Output Tokens / Call** | Stat (주황색) | 호출당 평균 출력 토큰 수 |

#### Row 2: 레이턴시 분석 (2개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **API Latency Percentiles (p50/p90/p99)** | Time Series (그라디언트 채움, 임계값 라인) | 시간대별 레이턴시 백분위수 추이. 초록(p50), 노란(p90), 빨간(p99). 그라디언트 채움, SLA 임계값 라인 표시 |
| **Throughput Trend (API Calls/Hour)** | Time Series (Stacked, 그라디언트) | 시간대별 API 호출 처리량 추이. 모델별 스택, 그라디언트 채움 |

#### Row 3: 모델별 성능 (3개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Latency by Model (p50/p90/p99)** | Table (셀 컬러링) | 모델별 레이턴시 백분위수 상세. `p99_ms`에 셀 배경색(초록<3s/노란<10s/빨간) |
| **Speed Mode Performance** | Table (셀 컬러링) | 속도 모드별 성능. `avg_ms`에 셀 배경색(초록<2s/노란<5s/빨간), `avg_cost_per_call`에 그라디언트 |
| **Cache Effect on Performance** | Table (셀 컬러링) | 캐시 활용도별 성능 비교. `cache_status`에 의미적 색상(히트=초록/미스=회색) |

#### Row 4: 에러 분석 (3개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Errors by Status Code** | Bar Chart (연속-빨강 그라디언트) | HTTP 상태 코드별 에러 횟수. continuous-reds 색상 모드, 그라디언트 채움 |
| **Errors by Model** | Bar Chart (연속-빨강 그라디언트) | 모델별 에러 횟수. continuous-reds 색상 모드, 그라디언트 채움 |
| **Error Trend** | Time Series (임계값 영역) | 시간대별 에러 발생 추이. 임계값 영역 스타일로 기준선 표시 |

#### Row 5: 에러 상세 및 상관관계 (2개)

| 패널 | 시각화 | 설명 |
|------|--------|------|
| **Recent API Errors** | Table (셀 컬러링) | 최근 API 에러 목록. `status_code`에 의미적 셀 배경색(4xx=노란/5xx=빨간) |
| **Latency vs Throughput Correlation** | Time Series | 레이턴시와 처리량 상관관계 시계열. 성능 병목 식별용. **신규 패널** |

### 7.2 주요 사용 시나리오

- **레이턴시 모니터링**: p50/p90/p99 시계열로 응답 시간 변화 추적
- **모델 성능 비교**: Latency by Model에서 Opus와 Sonnet의 성능 차이 확인
- **캐시 효과 측정**: Cache Effect on Performance에서 캐시가 레이턴시/비용에 미치는 영향 확인
- **에러 대응**: Error Rate 임계값 및 Recent API Errors에서 에러 상황 즉시 파악

---

## 8. Extensions & Agents (Prometheus + Athena)

**목적**: Claude Code 2.x에서 신규 도입된 Hooks, Plugins, MCP, Skills, Subagent(Agent), Effort 텔레메트리를 시각화합니다. Subagent/Effort/Skill 비용·토큰 귀속은 AMP 라벨로 즉시(과거 데이터 포함) 분석하고, MCP 연결·Plugin/Skill 채택 상세는 Athena 이벤트로 분석합니다.

**UID**: `claude-code-extensions` | **패널 수**: 10 (+ 3개 행 헤더) | **데이터 소스**: Prometheus (AMP) + Athena | **필터**: model, user

> **데이터 가용성**: AMP 기반 패널(Subagent/Effort/Skill 비용·토큰, MCP Tool Usage)은 라벨이 이미 수집되어 있으므로 **전체 기간(과거 포함)** 에 대해 즉시 렌더링됩니다. Athena 기반 패널(MCP 연결, Plugin 채택/매트릭스, Subagent Activity Detail)은 2.x 스키마 배포 시점 이후 데이터부터 채워지며, 그 이전 시간 범위는 비어 있을 수 있습니다(정상).

### 8.1 패널 구성

대시보드는 3개 섹션(행)으로 구성됩니다.

#### Section 1: Subagent & Effort (Prometheus)

| 패널 | 시각화 | 데이터 소스 | 설명 |
|------|--------|-------------|------|
| **Cost by Subagent** | Bar Chart (수평) | Prometheus | `agent_name` 라벨별 누적 비용. 빈 라벨 = 메인 스레드 |
| **Cost by Effort Mode** | Pie Chart (도넛) | Prometheus | Effort 모드(`high`, `xhigh`)별 비용 분포 |
| **Token Rate by Effort Mode** | Time Series (그라디언트) | Prometheus | Effort 모드별 토큰 소비율(tokens/min) 추이 |
| **Subagent Activity Detail** | Table (셀 컬러링) | Athena | Subagent별 API 호출 수, 비용, 평균 지연 시간. `total_cost`에 배경 그라디언트 |
| **Subagent Completion Summary** | Table (셀 컬러링) | Athena | `subagent_completed` 이벤트 기반 Subagent 유형별 실행 횟수, 평균 소요 시간, 평균 토큰, 평균 도구 사용 수. `avg_tokens`에 배경 그라디언트 |

#### Section 2: MCP (Athena)

| 패널 | 시각화 | 데이터 소스 | 설명 |
|------|--------|-------------|------|
| **MCP Connection Success Rate** | Gauge (임계값) | Athena | `status='connected'` 비율. 빨간(<80%)/노란/초록(>95%) |
| **MCP Server Connections** | Table | Athena | MCP 서버별 연결 시도/성공/평균 연결 시간/전송 방식 |
| **MCP Tool Usage (cost)** | Bar Chart (수평) | Prometheus | `mcp_tool_name` 라벨별 비용 귀속 |

#### Section 3: Plugins & Skills

| 패널 | 시각화 | 데이터 소스 | 설명 |
|------|--------|-------------|------|
| **Skill Activation Frequency** | Bar Chart (수평) | Prometheus | `skill_name` 라벨별 활성 빈도. 빈 라벨(메인 스레드) 제외 |
| **Plugins Loaded by Marketplace** | Bar Chart (수평) | Athena | 마켓플레이스별 로드된 distinct 플러그인 수 |
| **Plugin Capability Matrix** | Table | Athena | 플러그인별 스코프/버전/MCP·Hook 제공 여부/세션 수 |

### 8.2 템플릿 변수

| 변수 | 데이터 소스 | 설명 |
|------|-------------|------|
| `$model` | Prometheus | 모델 필터 (AMP 비용/토큰 패널에 적용) |
| `$user` | Athena | 사용자 필터 (`user_name` 우선, 없으면 `user_id`). Athena 패널에 적용 |

### 8.3 주요 사용 시나리오

- **Subagent 비용 귀속**: Cost by Subagent에서 Explore/Plan 등 Subagent가 차지하는 비용을 확인하고, Overview의 Subagent Cost Share KPI와 함께 메인 스레드 대비 비중을 파악
- **Effort 모드 비용 관리**: Cost by Effort Mode / Token Rate by Effort에서 `xhigh` 모드의 비용·토큰 영향을 모니터링
- **MCP 안정성 점검**: MCP Connection Success Rate와 MCP Server Connections에서 연결 실패가 잦은 서버 식별
- **Plugin/Skill 채택 분석**: Plugin Capability Matrix와 Skill Activation Frequency로 어떤 확장이 실제로 사용되는지 파악

---

## 9. 템플릿 변수 사용법

모든 대시보드는 상단에 드롭다운 필터를 제공하여 데이터를 세분화할 수 있습니다.

### 9.1 변수 목록

**Athena 대시보드 (4종)**:

| 변수 | 라벨 | 대시보드 | 설명 |
|------|------|----------|------|
| `$team` | Team | 전체 | 팀 ID 필터. OTEL_RESOURCE_ATTRIBUTES에 `team_id` 설정 시 활성화 |
| `$user` | User | Cost, Usage, Tools, API | 사용자 필터. `user_name`이 있으면 이름, 없으면 `user_id` 표시 |
| `$model` | Model | Cost, Usage, API | 모델 필터 (claude-opus-4-6, claude-sonnet-4-6 등) |
| `$tool` | Tool | Tool Analytics | 도구 필터 (Bash, Read, Write, Edit, Grep, Glob 등) |

**Prometheus 대시보드 (Real-Time Metrics)**:

| 변수 | 라벨 | 설명 |
|------|------|------|
| `$organization_id` | Organization | 조직 ID 필터. PromQL `label_values()`로 자동 조회 |
| `$user_id` | User | 사용자 ID 필터. 선택한 조직 내 사용자 자동 조회 |
| `$model` | Model | 모델 필터. 비용/토큰 관련 패널에 적용 |

### 9.2 사용 방법

1. **전체 보기**: 기본값 "All"을 선택하면 모든 데이터를 표시합니다
2. **특정 항목 필터**: 드롭다운에서 특정 값을 선택하면 해당 항목만 표시됩니다
3. **복합 필터**: 여러 변수를 조합하여 사용할 수 있습니다 (예: team=backend, model=claude-opus-4-6)
4. **시간 범위**: Grafana 기본 시간 범위 선택기(우측 상단)로 분석 기간을 설정합니다

### 9.3 Team 변수 활성화

`team_id` 변수는 Claude Code 클라이언트에서 OTel 리소스 속성을 설정해야 활성화됩니다.

```bash
export OTEL_RESOURCE_ATTRIBUTES="team_id=backend,department=engineering,cost_center=CC-001"
```

설정하지 않으면 `team_id`는 NULL이 되어 Team 드롭다운에 값이 표시되지 않습니다.

---

## 10. 핵심 인사이트 해석 가이드

### 10.1 Opus vs Sonnet 비용 구조

분석 데이터에서 확인된 모델별 비용 패턴:

| 지표 | Opus | Sonnet |
|------|------|--------|
| API 호출 비중 | 27.3% | 72.7% |
| 비용 비중 | **90.2%** | 9.8% |
| 호출당 평균 비용 | $0.1176 | $0.0048 |
| 비용 배수 | 24.5x | 1x (기준) |

**해석**: Opus는 호출 수의 1/4에 불과하지만 비용의 90%를 차지합니다. 이는 Opus가 복잡한 코드 생성 작업에 사용되어 많은 출력 토큰을 소비하기 때문입니다. 비용 최적화의 핵심은 **Opus 사용 비율 모니터링**입니다.

**대시보드 확인 위치**:
- Real-Time Metrics > Cost by Model (실시간 비용 비중)
- Cost Deep Analysis > Model Cost Efficiency Comparison (요청 레벨 상세)

### 10.2 모델 역할 패턴

Claude Code는 두 모델을 서로 다른 역할로 사용합니다:

| 모델 | 입력 토큰 | 출력 토큰 | I/O 비율 | 역할 |
|------|-----------|-----------|----------|------|
| **Sonnet** | 17,884 | 380 | **47:1** | 라우터/판단 (도구 선택, 짧은 응답) |
| **Opus** | 601 | 2,200 | **1:3.66** | 생성자 (코드 생성, 복잡한 추론) |

**해석**:
- **Sonnet (47:1 입출력 비율)**: 대량의 컨텍스트를 읽고 짧은 판단을 내립니다. "어떤 도구를 쓸지", "다음 단계는 무엇인지" 등의 라우팅 역할입니다.
- **Opus (1:3.66 입출력 비율)**: 적은 입력으로 많은 출력을 생성합니다. 실제 코드 작성, 복잡한 분석 등 생성 작업을 담당합니다.

**대시보드 확인 위치**:
- Usage & Session Insights > Model Role Pattern (I/O Ratio) (막대 차트)

### 10.3 캐시 최적화 기회

| 모델 | 캐시 재사용률 | 캐시 읽기 토큰 비중 |
|------|---------------|---------------------|
| **Opus** | **54.57%** | 54.47% |
| **Sonnet** | **0%** | 0% |

**해석**:
- Opus의 캐시 재사용률 54.57%는 양호한 수준이나, 여전히 45%의 개선 여지가 있습니다
- Sonnet은 캐시를 전혀 활용하지 못하고 있어, 캐시 전략 개선 시 비용 절감 가능성이 큽니다
- 캐시가 활성화되면 캐시 토큰 가격은 일반 입력 토큰 대비 90% 저렴하므로, 재사용률 향상이 직접적인 비용 절감으로 이어집니다

**대시보드 확인 위치**:
- Cost Deep Analysis > Cache Reuse Rate by Model (게이지)
- API Performance > Cache Effect on Performance (테이블)

### 10.4 세션 복잡도 해석

Usage & Session Insights의 Session Complexity 테이블에서 세션별 주요 지표를 확인할 수 있습니다:

| 지표 | 의미 | 기준값 (참고) |
|------|------|--------------|
| 세션 지속 시간 | 작업 시간 | 평균 ~18분 |
| 프롬프트 수 | 사용자 요청 횟수 | 세션당 ~2회 |
| API 호출 수 | AI 추론 횟수 | 프롬프트당 ~16.5회 |
| 도구 사용 수 | 도구 실행 횟수 | 세션당 ~9회 |
| 세션 비용 | 세션 총 비용 | ~$1.17 |

프롬프트당 API 호출 수가 높다는 것은 Claude Code의 멀티턴 에이전트 특성(도구 호출 -> 결과 확인 -> 후속 API 호출)을 반영합니다.

---

## 11. FAQ

### Q1. 대시보드에 데이터가 표시되지 않습니다 (No Data).

**A**: 다음 사항을 확인하세요:
1. **시간 범위**: Grafana 우측 상단 시간 범위가 데이터가 존재하는 기간을 포함하는지 확인
2. **필터 변수**: Team, User, Model 필터가 "All"로 설정되어 있는지 확인
3. **데이터 소스**: Grafana > Configuration > Data Sources에서 Athena/Prometheus 데이터 소스가 정상 연결되어 있는지 확인
4. **파티션 등록** (Athena만): 새로 수집된 데이터의 파티션이 등록되어 있는지 확인

### Q2. Team 드롭다운이 비어 있습니다.

**A**: `team_id`는 OTel 리소스 속성으로 클라이언트에서 설정해야 합니다:
```bash
export OTEL_RESOURCE_ATTRIBUTES="team_id=your-team-id"
```
설정하지 않으면 `team_id` 컬럼이 NULL이므로 드롭다운에 값이 나타나지 않습니다. 기존에 team_id 없이 수집된 데이터는 Team 필터를 "All"로 두면 정상 조회됩니다.

### Q3. Opus와 Sonnet의 비용 차이가 큰 이유는 무엇인가요?

**A**: Opus는 Sonnet 대비 토큰당 단가가 높고, 출력 토큰을 많이 생성하는 코드 작성/복잡한 추론 작업에 사용됩니다. Sonnet은 짧은 판단(도구 선택, 라우팅)에 사용되어 출력 토큰이 적습니다. Opus 호출 비율(27%)이 비용의 90%를 차지하는 것은 정상적인 패턴입니다.

### Q4. 캐시 재사용률은 얼마가 적정한가요?

**A**: 일반적으로 50% 이상이면 양호, 70% 이상이면 우수한 수준입니다. 캐시 재사용률이 낮다면 동일 세션 내에서 컨텍스트가 자주 변경되고 있다는 의미일 수 있습니다. Sonnet의 캐시 재사용률이 0%라면, Sonnet 호출 패턴이 캐시 활용에 적합하지 않은 구조일 수 있습니다.

### Q5. 도구 성공률이 낮은 경우 어떻게 대응해야 하나요?

**A**: Tool Analytics > Recent Tool Errors 테이블에서 실패 원인을 확인하세요. 일반적인 원인:
- **Bash 도구 실패**: 명령어 실행 오류, 타임아웃, 권한 부족
- **Read 도구 실패**: 파일 미존재, 경로 오류
- **Write/Edit 도구 실패**: 파일 권한, 디스크 공간 부족

### Q6. 대시보드 간 이동은 어떻게 하나요?

**A**: 모든 대시보드 상단에 네비게이션 링크가 있습니다. 링크를 클릭하면 다른 대시보드로 이동합니다. Real-Time Metrics에서 이상을 감지하면 Athena 대시보드로 드릴다운하여 원인을 분석하는 것이 일반적인 워크플로우입니다.

### Q7. 시간 범위는 어떻게 설정하는 것이 좋나요?

**A**: 사용 목적에 따라:
- **실시간 모니터링** (Real-Time Metrics): Last 1 hour ~ Last 6 hours
- **일일 리뷰** (Athena 대시보드): Today 또는 Last 24 hours
- **주간 리뷰**: Last 7 days
- **월간 리뷰**: Last 30 days

시간 범위가 길어질수록 Athena 쿼리 비용이 증가할 수 있으므로, 필요한 범위만 선택하세요.

### Q8. Real-Time Metrics 대시보드와 Athena 대시보드의 차이점은 무엇인가요?

**A**: 두 유형은 서로 다른 데이터와 목적을 가지며, 중복되지 않습니다:
- **Real-Time Metrics**: AMP(Prometheus)에서 8종 집계 카운터 메트릭을 PromQL로 조회합니다. 30초 단위 자동 새로고침. **"지금 무슨 일이 일어나는가"**에 답합니다.
- **Athena 대시보드 (4종)**: S3에 저장된 개별 이벤트 레코드를 SQL로 쿼리합니다. 이벤트별 상세 필드(레이턴시 백분위수, 에러 상세, 도구 결과 크기 등)를 분석. **"왜 그런 일이 일어났는가"**에 답합니다.

### Q9. Real-Time Metrics에 데이터가 없는데, Athena 대시보드에는 데이터가 있습니다.

**A**: 메트릭 파이프라인과 이벤트 파이프라인은 독립적으로 동작합니다. 이 증상은 클라이언트에서 `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta`가 설정되어 있을 때 발생합니다. `prometheusremotewrite` exporter가 delta 메트릭을 경고 없이 삭제하기 때문입니다. `cumulative`로 변경하거나 이 환경변수를 제거하세요.

---

## 12. 트러블슈팅

### 12.1 "No Data" 표시

| 증상 | 원인 | 해결 방법 |
|------|------|----------|
| 모든 패널이 No Data | 시간 범위에 데이터 없음 | 시간 범위 확장 또는 데이터 존재 기간 확인 |
| 모든 패널이 No Data | Athena 데이터 소스 미연결 | Grafana > Data Sources에서 Athena 연결 확인 |
| 모든 패널이 No Data | 파티션 미등록 | Athena에서 `MSCK REPAIR TABLE claude_code_telemetry.events` 실행 |
| 특정 패널만 No Data | 필터 조건에 해당 데이터 없음 | 필터를 "All"로 변경하여 확인 |
| api_error 관련 패널 No Data | API 에러 이벤트가 아직 없음 | 정상 상황 (에러가 없으면 No Data가 맞음) |

### 12.2 쿼리 느림

| 증상 | 원인 | 해결 방법 |
|------|------|----------|
| 전체적으로 쿼리 느림 | 시간 범위가 너무 넓음 | 시간 범위를 좁혀서 스캔 데이터 감소 |
| 전체적으로 쿼리 느림 | 파티션 프루닝 미적용 | 쿼리에 `timestamp` 필터가 포함되어 있는지 확인 |
| 특정 패널만 느림 | cross-event 조인 쿼리 | Session Complexity, Cost per Prompt 등 조인 쿼리는 원래 시간이 더 걸림 |
| Athena 타임아웃 | 데이터량 과다 | Athena 워크그룹 타임아웃 설정 확인, 시간 범위 축소 |

### 12.3 데이터 정확성

| 증상 | 원인 | 해결 방법 |
|------|------|----------|
| 비용이 0으로 표시 | `cost_usd`가 NULL | api_request 이벤트에서 cost_usd가 수집되는지 확인 |
| 세션 수가 과다 | 짧은 세션이 많음 | 정상적인 사용 패턴 (IDE 재시작 시 새 세션 생성) |
| 캐시 토큰이 0 | 캐시 미활성화 | Claude API 캐시 설정 확인 |
| 도구 결정 건수와 실행 건수 불일치 | tool_decision과 tool_result는 별도 이벤트 | 정상 (결정 후 실행 거부 가능) |

### 12.4 Grafana 연결 문제

| 증상 | 원인 | 해결 방법 |
|------|------|----------|
| Athena 데이터 소스 오류 | IAM 권한 부족 | Grafana 서비스 역할에 `athena:StartQueryExecution`, `athena:GetQueryResults`, `athena:ListDatabases`, `glue:GetDatabase`, `glue:GetTable` 등 필요 |
| "Access Denied" 에러 | S3 버킷 접근 권한 부족 | Grafana 서비스 역할에 S3 읽기 권한 추가 |
| 쿼리 결과가 나오지 않음 | Athena 워크그룹 설정 | Athena 워크그룹의 결과 S3 위치가 설정되어 있는지 확인 |

### 12.5 데이터 수집 문제

| 증상 | 원인 | 해결 방법 |
|------|------|----------|
| 새 데이터가 보이지 않음 | 파티션 미등록 | EventBridge + Lambda 자동 등록이 정상 동작하는지 확인 |
| 이벤트 지연 | Firehose 버퍼링 | Firehose는 최대 5분(또는 5MB) 버퍼링 후 S3에 저장. 실시간이 아님 |
| 특정 필드가 NULL | 클라이언트 설정 미비 | Claude Code에서 해당 필드를 전송하는지 확인 |

### 12.6 메트릭 파이프라인 문제 (Real-Time Metrics 대시보드)

| 증상 | 원인 | 해결 방법 |
|------|------|----------|
| Real-Time Metrics 전체 No Data | 클라이언트 delta temporality 설정 | `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative`로 변경 또는 환경변수 제거 (기본값 cumulative) |
| Real-Time Metrics 전체 No Data | Prometheus 데이터 소스 미연결 | Grafana > Data Sources에서 Prometheus (AMP) 연결 확인. UID가 `prometheus`인지 확인 |
| 일부 메트릭만 표시 | 필터 변수 불일치 | Organization, User, Model 필터를 "All"로 설정 |
| Athena 대시보드는 정상, Prometheus만 No Data | 메트릭 파이프라인만 이상 | ADOT 로그에서 prometheusremotewrite exporter 에러 확인. 클라이언트 temporality 설정 확인 |
| 30초 이상 데이터 지연 | ADOT batch 프로세서 버퍼링 | 정상 동작 (batch/metrics timeout 60초). 최대 60초 지연 가능 |

---

## 부록: 대시보드 JSON 파일 목록

| 파일 | 경로 | 데이터 소스 UID |
|------|------|----------------|
| Overview | `grafana/dashboards/overview.json` | `prometheus` + `athena` |
| Real-Time Metrics | `grafana/dashboards/realtime-metrics.json` | `prometheus` |
| Cost Deep Analysis | `grafana/dashboards/cost-analysis.json` | `athena` |
| Usage & Session Insights | `grafana/dashboards/usage-insights.json` | `athena` |
| Tool Analytics | `grafana/dashboards/tool-analytics.json` | `athena` |
| API Performance | `grafana/dashboards/api-performance.json` | `athena` |
| Extensions & Agents | `grafana/dashboards/extensions-agents.json` | `amp` + `athena` |

> **참고**: Extensions & Agents 대시보드의 Prometheus 패널은 데이터 소스 UID로 `amp`를, 그 외 Prometheus 대시보드는 `prometheus`를 사용합니다. 임포트 시 환경에 맞는 데이터 소스 UID가 존재하는지 확인하세요.

대시보드를 Grafana에 임포트하려면: Grafana > Dashboards > Import > Upload JSON file에서 위 파일을 업로드하세요.

**임포트 순서 참고**: 데이터 소스를 먼저 설정한 후 대시보드를 임포트하세요.
1. Athena 데이터 소스 설정 (UID: `athena`)
2. Prometheus (AMP) 데이터 소스 설정 (UID: `prometheus` 및 Extensions & Agents용 `amp`)
3. 대시보드 JSON 파일 7종 임포트

---

> **참고**: 이 대시보드 구조의 초기 설계는 `dashboard-overlap-analysis.md` 및 `dashboard-design-spec.md`라는 분석 산출물을 바탕으로 했으나, 이 두 문서는 **과거 설계 산출물로 현재 리포지토리에는 포함되어 있지 않습니다**. 최신 스키마/대시보드 정의는 본 가이드와 `docs/otel-schema.md`, `docs/data-schema.md`를 정본으로 참고하세요.
