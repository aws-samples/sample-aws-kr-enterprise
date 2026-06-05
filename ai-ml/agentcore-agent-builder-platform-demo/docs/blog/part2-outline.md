# Part 2 초안 — Living Outline

> 초안(`part2-draft-ko.md`)과 sync를 맞추는 살아있는 아웃라인.
> 섹션을 채울 때마다 상태(Status)와 실제 단어수(Actual)를 갱신한다.

**Thesis**: Part 1에서 *직접 지은* 횡단 기능(레지스트리·Harness·관측)을 AgentCore
**관리형 서비스**로 갈아 끼워, 더 적은 코드로 더 강한 보증을 얻는다.
핵심 통찰: 자체 "Harness" 한 덩어리가 관리형에서는 **Policy(경계 강제) + Harness(실행 루프)** 둘로 갈라진다.

**골격**: before/after 대비 · 한국어 · Part 1과 1:1 매핑

## Status Legend
`TODO` 미작성 · `DRAFT` 초안작성됨 · `REVIEW` 검토필요 · `DONE` 확정

> 실제(Actual) 단위 = 한국어 어절(띄어쓰기 토큰).
> 초안(`part2-draft-ko.md`)의 각 제목에 `[S0]`~`[S7]` 태그가 붙어 있어 아래 # 열과 1:1 대조.

## 섹션 트래커

| # | 섹션 | 한 줄 요지 | 시각자료 | Status |
|---|------|-----------|---------|--------|
| 0 | 들어가며 — 직접 지은 것들 | Part 1에서 자체 구현했던 3가지(카드 레지스트리·Harness·3중 관측) 회수 | — | DRAFT |
| 1 | 전환의 지도 | 매핑 표. 핵심: 자체 Harness → Policy + Harness 분기 | 매핑 표 | DRAFT |
| 2 | 대체① Registry | 카드 CRUD(DynamoDB) → AgentCore Registry(Preview). publish→review→approve, hybrid search, MCP-native | — | DRAFT |
| 3 | 대체② Policy + Harness | persona-injection/depth → Policy(Cedar, Gateway 강제) · agent_runner 루프 → Harness(관리형 loop) | — | DRAFT |
| 4 | 대체③ Observability + Evaluations | OTEL+Hook+SideChannel 3중 → Observability(통합 뷰) + Evaluations(신규 증강) | — | DRAFT |
| 5 | 무엇이 남는가 | Builder UX·Control Plane·Presentation·도구 구현(Lambda)은 우리 몫. undifferentiated heavy lifting만 위임 | — | DRAFT |
| 6 | before/after 한눈에 | ASCII 대비도 + 단계적 전환 권장 순서 | ASCII 도식 | DRAFT |
| 7 | 마무리 | 차별화 vs 비차별화. GitHub 링크 + GA 추적 브랜치 예고 | — | DRAFT |

## 서사 흐름 (한눈에)

```
회수(0) → 매핑(1) → 대체①Registry(2) → 대체②Policy+Harness(3) → 대체③Obs+Eval(4) → 남는것(5) → 종합도(6) → 마무리(7)
└ 무엇을 직접 지었나 ┘ └────────── 무엇으로 대체되는가 (3대 대체) ──────────┘ └ 우리 몫 ┘ └ 정리 ┘
```

## 출처/사실 검증 (공식 문서 기준, 2026-06-05 확인)
- AgentCore 정식 서비스 목록: Runtime·Harness·Memory·Gateway·Identity·Code Interpreter·Browser·Observability·**Payments**·**Evaluations**·**Policy**·**Registry**
- **Registry = Preview** (문서 제목에 "(Preview)" 명시). 나머지는 별도 preview 표기 없음
- Policy: Cedar 기반 + 자연어 작성, Gateway에서 모든 tool call을 실행 전 결정적 차단, CloudWatch 로깅
- Harness: 단일 API로 model/prompt/tools 인라인 선언, 격리 microVM, BYO 컨테이너, Memory/Gateway/Browser/Code Interpreter/Observability 통합
- Evaluations: 세션/트레이스/스팬 단위 자동 평가, 결과 Observability 통합

## 시리즈 연결 (self-built → Managed)
- Part 1 [S3] Registry forward-note → 본편 [S2]에서 회수
- Part 1 [S5] Harness forward-note → 본편 [S3]에서 회수 (Policy+Harness로 분기)
- 심화편(`deepdive-agent-runtime-ko.md`) agent_runner 해부 → 본편 [S3] ②-b에서 "관리형 Harness가 이 루프를 대체" 로 연결

## 다음 작업
- Part 1 + Part 2 한글 **확정(DONE)** 후 → 영문화 일괄 진행 (사용자 결정)
- Registry 콘솔 스크린샷 확보 시 [S2]에 삽입 검토
