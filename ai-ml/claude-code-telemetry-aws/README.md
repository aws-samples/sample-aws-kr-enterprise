# Claude Code Observability Platform on AWS

Claude Code의 OpenTelemetry 텔레메트리 데이터를 수집, 저장, 분석하기 위한 AWS 관측성(Observability) 플랫폼입니다. AWS CDK(TypeScript)로 전체 인프라를 정의하며, **이중 파이프라인**으로 운영됩니다: 메트릭은 Amazon Managed Prometheus(AMP)에 저장하여 실시간 모니터링(30초 갱신)을, 이벤트는 S3(Parquet) + Athena에 저장하여 심층 분석을 제공합니다. Amazon Managed Grafana에서 7개 프로덕션 수준 대시보드로 통합 시각화합니다. 게이지 패널, 스파크라인, 그라디언트 채움, 임계값 기반 색상, 테이블 셀 컬러링, 드릴다운 데이터 링크 등 운영 환경에 적합한 시각화를 제공합니다. 설계 철학: Prometheus는 실시간 집계 메트릭 및 정확한 비용 합산에, Athena는 이벤트 레벨 심층 분석에 특화합니다. 비용 관련 패널은 Prometheus 기반으로 안정적인 값을 제공하고, Athena는 유저명 매핑과 요청 단위 상세 분석을 담당하는 하이브리드 구조입니다.

> **Claude Code 2.x 텔레메트리 지원**: Claude Code 2.x에서 추가된 신규 텔레메트리 — **Hooks, Plugins, MCP 서버/도구, Skills, Subagent, Effort 모드** — 를 수집·분석합니다. 신규 이벤트 타입(`hook_execution_*`, `hook_registered`, `plugin_loaded`, `mcp_server_connection`, `skill_activated`, `subagent_completed`)과 비용/토큰 메트릭의 신규 귀속 라벨(`agent_name`, `effort`, `skill_name`, `mcp_server_name`, `mcp_tool_name`, `plugin_name`, `marketplace_name`)을 파이프라인과 전용 **Extensions & Agents** 대시보드에서 다룹니다. 자세한 변경 내역은 [docs/dashboard-guide.md](docs/dashboard-guide.md), [docs/otel-schema.md](docs/otel-schema.md), [docs/data-schema.md](docs/data-schema.md)를 참조하세요.

---

## 아키텍처

```
Developer PCs (Claude Code + OTel SDK)
    |
    | OTLP gRPC (:4317) / HTTP (:4318)
    v
NLB (Network Load Balancer, Internet-facing)
    |
    v
ADOT Collector (ECS Fargate, Private Subnet)
    |
    +-- Metrics Pipeline --> Prometheus Remote Write --> AMP
    |
    +-- Logs Pipeline ----> CloudWatch Logs --> Kinesis Data Firehose
                                                    |
                                                    v (Lambda 변환)
                                               S3 (Parquet, Snappy)
                                                    |
                                          +---------+---------+
                                          |                   |
                                          v                   v
                                   S3 Event →          Glue Catalog
                                   EventBridge →         + Athena
                                   Lambda →                  |
                                   Glue BatchCreate          v
                                   Partition          Amazon Managed Grafana
                                   (실시간 파티션 등록)  (7개 대시보드)
```

## 주요 기능

이 플랫폼은 7개의 Grafana 대시보드를 통해 Claude Code 사용 현황을 전방위로 모니터링합니다. 게이지 패널, 스파크라인, 그라디언트 채움, 임계값 기반 색상, 테이블 셀 컬러링, 드릴다운 데이터 링크 등 프로덕션 수준의 시각화를 제공합니다. Prometheus는 실시간 집계 메트릭 및 정확한 비용 합산에, Athena는 이벤트 레벨 심층 분석에 특화합니다. 비용 관련 패널은 Prometheus+Athena 하이브리드로 안정적인 값을 제공합니다.

### 실시간 메트릭 대시보드 (Prometheus/AMP)

| 대시보드 | 패널 | 설명 | 데이터 소스 |
|----------|------|------|-------------|
| **Overview** | 18 | Prometheus + Athena 통합 요약. 핵심 KPI(세션/비용/토큰/활성시간/커밋/PR), Subagent 비용 비중, Error Rate(Athena), 스파크라인, 임계값 색상, 비용 추이, 레이턴시, 이벤트 분포, 도구 사용, 최근 세션 | Prometheus (AMP) + Athena |
| **Real-Time Metrics** | 18 | 8종 Prometheus 메트릭 실시간 모니터링. 게이지(캐시 히트율/수락률), 스파크라인, 그라디언트 채움, 드릴다운 링크. 30초 자동 새로고침 | Prometheus (AMP) |

### 비용 심층 분석 대시보드 (Prometheus + Athena 하이브리드)

| 대시보드 | 패널 | 설명 | 데이터 소스 |
|----------|------|------|-------------|
| **Cost Deep Analysis** | 10 | 모델별 비용 트렌드, 비용 분포 파이차트, 유저별 비용 귀속. 집계 비용은 Prometheus(정확), 요청 단위 상세는 Athena | Prometheus + Athena |

### 이벤트 심층 분석 대시보드 (Athena)

| 대시보드 | 패널 | 설명 | 데이터 소스 |
|----------|------|------|-------------|
| **Usage & Session Insights** | 10 | 세션 흐름, 프롬프트 복잡도, 모델 역할 패턴, 테이블 셀 컬러링, 버전 분포 | Athena |
| **Tool Analytics** | 12 | 게이지(성공률/수락률), 그라디언트 바 차트, 테이블 셀 컬러링, 성공률 추이, 에러 패턴 | Athena |
| **API Performance** | 13 | 게이지(에러율), 그라디언트 채움, 임계값 라인, 테이블 셀 컬러링, 레이턴시-처리량 상관관계 | Athena |

### 확장 기능 분석 대시보드 (Prometheus + Athena 하이브리드, 2.x 신규)

| 대시보드 | 패널 | 설명 | 데이터 소스 |
|----------|------|------|-------------|
| **Extensions & Agents** | 12 | Claude Code 2.x 확장 기능 분석. Subagent/Effort 비용 귀속(AMP 라벨 기반, 과거 데이터 포함 즉시 렌더링), MCP 서버 연결/도구 사용, Plugin 마켓플레이스별 채택, Skill 활성화 빈도, Subagent 실행 요약(토큰/도구/소요시간) | Prometheus (AMP) + Athena |

> **데이터 가용성 참고**: AMP 라벨 기반 패널(Subagent/Effort/Skill 비용·토큰)은 라벨이 이미 수집되어 있어 **과거 데이터 포함 즉시** 렌더링됩니다. Athena 기반 패널(MCP 연결, Plugin 채택, Subagent 실행 요약)은 2.x 스키마 배포 시점 이후 데이터부터 채워집니다.

주요 분석 기능:
- **실시간 메트릭 모니터링**: AMP 기반 8종 Prometheus 카운터 메트릭을 PromQL로 실시간 조회 (30초 갱신)
- **OTel 기반 관측가능성**: Claude Code 1.x 5종 이벤트(api_request, api_error, tool_result, tool_decision, user_prompt) + 2.x 신규 7종(hook_execution_start/complete, hook_registered, plugin_loaded, mcp_server_connection, skill_activated, subagent_completed) 수집 및 분석
- **확장 기능 분석 (2.x)**: Subagent별/Effort 모드별 비용·토큰 귀속, MCP 서버 연결 성공률 및 도구 사용, Plugin/Skill 채택 현황
- **비용 심층 분석**: 모델별 비용 트렌드(Prometheus), 유저별 비용 귀속(하이브리드), 요청 단위 비용 효율(Athena). Prometheus 기반 비용 합산으로 새로고침 시 값 변동 없음
- **세션 인사이트**: 세션 복잡도, 모델 역할 패턴(라우터 vs 생성자), 프롬프트 길이 분포
- **도구 분석**: 도구별 성공률/실행 시간, 자동/수동 승인 비율, 결과 크기 모니터링
- **성능 모니터링**: API 레이턴시 백분위수, 속도 모드별 성능, 캐시가 성능에 미치는 영향

> 대시보드 상세 가이드는 [docs/dashboard-guide.md](docs/dashboard-guide.md)를 참조하세요.

## CDK 스택 구성

| 스택 | 설명 | 주요 리소스 |
|------|------|-------------|
| **NetworkStack** | VPC, 서브넷 (2 AZ), 보안 그룹 | VPC, NAT GW, Security Group |
| **MetricsStack** | Prometheus 워크스페이스 | AMP Workspace |
| **EventsStack** | 이벤트 파이프라인 | S3, Firehose, Glue DB/Table, 파티션 자동 등록 Lambda |
| **CollectorStack** | 텔레메트리 수집기 | ECS Fargate, ADOT Container, NLB |
| **DashboardStack** | 시각화 | Amazon Managed Grafana Workspace |

## 사전 요구사항

- **Node.js** >= 18
- **AWS CDK CLI**: `npm install -g aws-cdk`
- **AWS CLI**: 자격증명 구성 완료 (`aws configure`)
- **AWS 계정**: CDK 부트스트랩 완료 (`cdk bootstrap`)
- **AWS IAM Identity Center (SSO)**: Grafana 인증에 필요

## 빠른 시작

```bash
# 1. 의존성 설치
npm install

# 2. 설정 커스터마이즈 (리전, 환경, 태그 등)
#    lib/config/app-config.ts 편집

# 3. 빌드
npm run build

# 4. CloudFormation 템플릿 합성 (선택)
npx cdk synth

# 5. 전체 스택 배포 (루트 스택 1개로 5개 Nested Stack 자동 배포)
npx cdk deploy TelemetryStack

# 6. Claude Code 환경변수 설정 (개발자 PC)
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_EXPORTER_OTLP_ENDPOINT=http://<NLB_DNS>:4317
# 메트릭 temporality: cumulative 필수 (delta 사용 시 AMP에 메트릭 미수신)
export OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative
```

> 상세 배포 가이드는 [docs/deployment-guide.md](docs/deployment-guide.md)를 참조하세요.
> Claude Code 개발자 설정은 [docs/claude-code-setup-guide.md](docs/claude-code-setup-guide.md)를 참조하세요.

## 설정 (Configuration)

`lib/config/app-config.ts`를 편집하여 커스터마이즈합니다.

| 설정 | 기본값 | 설명 |
|------|--------|------|
| `projectName` | `claude-code-telemetry` | 리소스 명명 접두사 |
| `environment` | `prod` | 환경 (`prod` / `dev`) |
| `region` | `us-east-1` | AWS 배포 리전 |
| `collectorPort` | `4317` | OTLP gRPC 포트 |
| `collectorHttpPort` | `4318` | OTLP HTTP 포트 |

## 프로젝트 구조

```
claude-code-telemetry-aws/
├── bin/
│   └── app.ts                          # CDK 앱 진입점
├── lib/
│   ├── config/
│   │   └── app-config.ts               # 공유 설정 (리전, 포트, 태그)
│   ├── telemetry-stack.ts              # 루트 스택 (Nested Stack 오케스트레이션)
│   └── nested-stacks/
│       ├── network-stack.ts            # VPC, 서브넷, NAT GW, 보안 그룹
│       ├── metrics-stack.ts            # AMP 워크스페이스
│       ├── events-stack.ts             # S3, Firehose, Glue, Lambda, CW Logs
│       ├── collector-stack.ts          # ECS Fargate, ADOT, NLB
│       └── dashboard-stack.ts          # Managed Grafana
├── config/
│   └── adot-collector-config.yaml      # ADOT Collector 파이프라인 설정
├── lambda/
│   └── firehose-transformer/
│       ├── index.py                    # Firehose Lambda Transformer (Python 3.12, 12종 이벤트 매핑)
│       └── test_transformer.py         # Transformer 단위 테스트 (stdlib unittest)
├── grafana/
│   ├── dashboards/                     # Grafana 대시보드 JSON (7종)
│   │   ├── overview.json               # Prometheus+Athena 하이브리드 - Overview (18패널)
│   │   ├── realtime-metrics.json       # Prometheus(AMP) - Real-Time Metrics (18패널)
│   │   ├── cost-analysis.json          # Prometheus+Athena 하이브리드 - Cost Deep Analysis (10패널)
│   │   ├── usage-insights.json         # Athena - Usage & Session Insights (10패널)
│   │   ├── tool-analytics.json         # Athena - Tool Analytics (12패널)
│   │   ├── api-performance.json        # Athena - API Performance (13패널)
│   │   └── extensions-agents.json      # Prometheus+Athena 하이브리드 - Extensions & Agents (12패널, 2.x 신규)
│   └── provisioning/                   # 데이터 소스 설정 참조
│       └── datasources/
│           └── datasources.yaml
├── docs/
│   ├── architecture.md                 # 상세 아키텍처 문서
│   ├── claude-code-setup-guide.md      # 개발자 환경 구성 가이드
│   ├── dashboard-guide.md              # 대시보드 사용 가이드 (7종)
│   ├── data-schema.md                  # S3/Glue/Athena 데이터 스키마
│   ├── deployment-guide.md             # 배포 가이드
│   ├── development.md                  # 개발 가이드
│   └── otel-schema.md                  # OTel 메트릭/이벤트 스키마
├── test/
│   └── app.test.ts                     # CDK 스냅샷 테스트
├── package.json
├── tsconfig.json
└── cdk.json
```

## 비용 예상

월간 예상 비용 (us-east-1 리전 기준):

| 팀 규모 | 예상 월 비용 (USD) |
|----------|-------------------|
| 10명 | $170 ~ $250 |
| 50명 | $350 ~ $550 |
| 200명 | $900 ~ $1,500 |

## 유용한 명령어

| 명령어 | 설명 |
|--------|------|
| `npm run build` | TypeScript 컴파일 |
| `npm run watch` | 감시 모드 컴파일 |
| `npm test` | Jest 테스트 실행 |
| `npx cdk synth` | CloudFormation 합성 |
| `npx cdk diff` | 배포 상태와 비교 |
| `npx cdk deploy TelemetryStack` | 전체 스택 배포 |
| `npx cdk destroy TelemetryStack` | 전체 스택 삭제 |

## 문서

- [아키텍처](docs/architecture.md) - 이중 파이프라인 상세 시스템 아키텍처
- [배포 가이드](docs/deployment-guide.md) - CDK 단계별 배포 절차
- [Claude Code 설정](docs/claude-code-setup-guide.md) - 개발자 환경 구성 (환경변수, 인증)
- [대시보드 가이드](docs/dashboard-guide.md) - 7종 대시보드 상세 사용 가이드 (2.x Extensions & Agents 포함)
- [개발 가이드](docs/development.md) - 개발 환경 설정, 코드 구조, 빌드/배포
- [OTel 스키마](docs/otel-schema.md) - Claude Code 메트릭/이벤트 스키마
- [데이터 스키마](docs/data-schema.md) - S3 파티셔닝, Glue 카탈로그, Athena 쿼리

## 라이선스

MIT License

---

# Claude Code Observability Platform on AWS (English)

An AWS observability platform for collecting, storing, and analyzing OpenTelemetry telemetry data from Claude Code. The entire infrastructure is defined using AWS CDK (TypeScript). It operates a **dual pipeline**: metrics are stored in Amazon Managed Prometheus (AMP) for real-time monitoring (30s refresh), while events are stored in S3 (Parquet) + Athena for detailed analysis. Unified visualization through Amazon Managed Grafana with 7 production-ready dashboards featuring gauge panels, sparklines, gradient fills, threshold-based coloring, table cell coloring, and drill-down data links. Design philosophy: Prometheus for real-time aggregated metrics and accurate cost totals, Athena for event-level deep analysis. Cost panels use a Prometheus+Athena hybrid approach for stable values — Prometheus provides accurate aggregate costs while Athena supplies user name mapping and per-request details.

> **Claude Code 2.x telemetry support**: Captures and analyzes the new telemetry introduced in Claude Code 2.x — **Hooks, Plugins, MCP servers/tools, Skills, Subagents, and Effort modes**. New event types (`hook_execution_*`, `hook_registered`, `plugin_loaded`, `mcp_server_connection`, `skill_activated`, `subagent_completed`) and new cost/token attribution labels (`agent_name`, `effort`, `skill_name`, `mcp_server_name`, `mcp_tool_name`, `plugin_name`, `marketplace_name`) are handled across the pipeline and a dedicated **Extensions & Agents** dashboard. See [docs/dashboard-guide.md](docs/dashboard-guide.md), [docs/otel-schema.md](docs/otel-schema.md), and [docs/data-schema.md](docs/data-schema.md) for details.

## Architecture

```
Developer PCs (Claude Code + OTel SDK)
    |
    | OTLP gRPC (:4317) / HTTP (:4318)
    v
NLB (Network Load Balancer, Internet-facing)
    |
    v
ADOT Collector (ECS Fargate, Private Subnet)
    |
    +-- Metrics Pipeline --> Prometheus Remote Write --> AMP
    |
    +-- Logs Pipeline ----> CloudWatch Logs --> Kinesis Data Firehose
                                                    |
                                                    v (Lambda Transform)
                                               S3 (Parquet, Snappy)
                                                    |
                                          +---------+---------+
                                          |                   |
                                          v                   v
                                   S3 Event →          Glue Catalog
                                   EventBridge →         + Athena
                                   Lambda →                  |
                                   Glue BatchCreate          v
                                   Partition          Amazon Managed Grafana
                                   (real-time          (7 dashboards)
                                    partition reg.)
```

## Key Features

The platform provides comprehensive Claude Code monitoring through 7 production-ready Grafana dashboards, featuring gauge panels, sparklines, gradient fills, threshold-based coloring, table cell coloring, and drill-down data links. Cost-related panels use a Prometheus+Athena hybrid for accuracy and stability.

### Real-Time Metrics Dashboard (Prometheus/AMP)

| Dashboard | Panels | Description | Data Source |
|-----------|--------|-------------|-------------|
| **Overview** | 18 | Prometheus + Athena integrated summary. Key KPIs (sessions/cost/tokens/active time/commits/PRs), Subagent cost share, Error Rate (Athena), sparklines, threshold coloring, cost trends, latency, event distribution, tool usage, recent sessions | Prometheus (AMP) + Athena |
| **Real-Time Metrics** | 18 | 8 Prometheus counter metrics with gauges (cache hit ratio/acceptance rate), sparklines, gradient fills, drill-down links. 30s auto-refresh | Prometheus (AMP) |

### Cost Deep Analysis Dashboard (Prometheus + Athena Hybrid)

| Dashboard | Panels | Description | Data Source |
|-----------|--------|-------------|-------------|
| **Cost Deep Analysis** | 10 | Model cost trends, cost distribution pie chart, per-user cost attribution. Aggregate costs from Prometheus (stable), per-request details from Athena | Prometheus + Athena |

### Event Deep Analysis Dashboards (Athena)

| Dashboard | Panels | Description | Data Source |
|-----------|--------|-------------|-------------|
| **Usage & Session Insights** | 10 | Session flow, prompt complexity, model role patterns, table cell coloring, version distribution | Athena |
| **Tool Analytics** | 12 | Gauges (success/accept rate), gradient bar charts, table cell coloring, success rate trend, error patterns | Athena |
| **API Performance** | 13 | Gauge (error rate), gradient fills, threshold lines, table cell coloring, latency-throughput correlation | Athena |

### Extensions & Agents Dashboard (Prometheus + Athena Hybrid, new in 2.x)

| Dashboard | Panels | Description | Data Source |
|-----------|--------|-------------|-------------|
| **Extensions & Agents** | 12 | Claude Code 2.x extension analytics. Subagent/Effort cost attribution (AMP label-based, renders immediately over historical data), MCP server connections/tool usage, plugin adoption by marketplace, skill activation frequency, subagent completion summary (tokens/tools/duration) | Prometheus (AMP) + Athena |

> **Data availability note**: AMP label-based panels (Subagent/Effort/Skill cost & tokens) render **immediately over historical data** since the labels are already collected. Athena-based panels (MCP connections, plugin adoption, subagent completion summary) populate from the 2.x schema deployment onward.

Key analysis capabilities:
- **Real-Time Metrics Monitoring**: 8 Prometheus counter metrics via AMP with PromQL queries (30s refresh)
- **OTel-based Observability**: Collects and analyzes Claude Code 1.x 5 event types (api_request, api_error, tool_result, tool_decision, user_prompt) plus 2.x 7 new types (hook_execution_start/complete, hook_registered, plugin_loaded, mcp_server_connection, skill_activated, subagent_completed)
- **Extension Analytics (2.x)**: Per-subagent / per-effort-mode cost & token attribution, MCP server connection success rate & tool usage, plugin/skill adoption
- **Cost Deep Analysis**: Model cost trends (Prometheus), per-user cost attribution (hybrid), per-request cost efficiency (Athena). Prometheus-based cost aggregation ensures stable values across refreshes
- **Session Insights**: Session complexity, model role patterns (router vs generator), prompt length distribution
- **Tool Analytics**: Per-tool success rate/execution time, auto/manual approval ratios, result size monitoring
- **Performance Monitoring**: API latency percentiles, speed mode performance, cache impact on latency

> For detailed dashboard guide, see [docs/dashboard-guide.md](docs/dashboard-guide.md).

## CDK Stacks

| Stack | Description | Key Resources |
|-------|-------------|---------------|
| **NetworkStack** | VPC, subnets (2 AZs), security groups | VPC, NAT GW, Security Group |
| **MetricsStack** | Prometheus workspace | AMP Workspace |
| **EventsStack** | Event pipeline | S3, Firehose, Glue DB/Table, Partition Register Lambda |
| **CollectorStack** | Telemetry collector | ECS Fargate, ADOT Container, NLB |
| **DashboardStack** | Visualization | Amazon Managed Grafana Workspace |

## Prerequisites

- **Node.js** >= 18
- **AWS CDK CLI**: `npm install -g aws-cdk`
- **AWS CLI**: Configured with credentials (`aws configure`)
- **AWS Account**: CDK bootstrapped (`cdk bootstrap`)
- **AWS IAM Identity Center (SSO)**: Required for Grafana authentication

## Quick Start

```bash
# 1. Install dependencies
npm install

# 2. Customize settings (region, environment, tags)
#    Edit lib/config/app-config.ts

# 3. Build
npm run build

# 4. Synthesize CloudFormation templates (optional)
npx cdk synth

# 5. Deploy all stacks (single root stack deploys 5 nested stacks)
npx cdk deploy TelemetryStack

# 6. Configure Claude Code environment variables (developer PC)
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_EXPORTER_OTLP_ENDPOINT=http://<NLB_DNS>:4317
# Metrics temporality: cumulative required (delta metrics are silently dropped by AMP)
export OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative
```

> For detailed deployment instructions, see [docs/deployment-guide.md](docs/deployment-guide.md).
> For Claude Code developer setup, see [docs/claude-code-setup-guide.md](docs/claude-code-setup-guide.md).

## Estimated Costs

Monthly estimated costs (Seoul region):

| Team Size | Estimated Monthly Cost (USD) |
|-----------|------------------------------|
| 10 developers | $170 ~ $250 |
| 50 developers | $350 ~ $550 |
| 200 developers | $900 ~ $1,500 |

## Documentation

- [Architecture](docs/architecture.md) - Dual pipeline system architecture
- [Deployment Guide](docs/deployment-guide.md) - CDK step-by-step deployment
- [Claude Code Setup](docs/claude-code-setup-guide.md) - Developer environment configuration
- [Dashboard Guide](docs/dashboard-guide.md) - Detailed guide for 7 dashboards (incl. 2.x Extensions & Agents)
- [Development Guide](docs/development.md) - Dev environment, code structure, build/deploy
- [OTel Schema](docs/otel-schema.md) - Claude Code metrics and events schema
- [Data Schema](docs/data-schema.md) - S3 partitioning, Glue catalog, Athena queries

## License

MIT License
