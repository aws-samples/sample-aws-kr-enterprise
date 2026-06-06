# Part 2 초안 — Living Outline (Deep Dive + 관리형 전환 병합본)

> 초안(`part2-draft-ko.md`)과 sync를 맞추는 살아있는 아웃라인.
> 2부작 재편: 기존 Deep Dive(독립편)를 Part 2에 흡수 → "해부 → 전환" 1편으로 통합.

**Thesis**: 직접 지어봐야 가치를 안다. agent_runner.py 베이스 이미지를 충실히 *해부*해
"신뢰할 수 있는 에이전트"의 기계장치를 보여준 뒤, 그중 *차별화되지 않는* 부분을
AgentCore **관리형 서비스**로 갈아 끼운다. 직접 본 노력의 무게가 전환의 가치를 키운다.
핵심 통찰: 코드 한 덩어리로 짠 안전 로직이 관리형에서는 **Policy(경계) + Harness(루프)** 둘로 갈라진다.

**골격**: 해부(1부) → 경첩 → 전환 before/after(2부) · 한국어 · Part 1과 균형 볼륨(2부작)

## Status Legend
`TODO` 미작성 · `DRAFT` 초안작성됨 · `REVIEW` 검토필요 · `DONE` 확정

> 초안의 각 제목에 `[S0]`~`[S14]` 태그. 아래 # 열과 1:1 대조.

## 섹션 트래커

| # | 섹션 | 한 줄 요지 | 부 | Status |
|---|------|-----------|----|--------|
| 0 | 들어가며 | 조립된 카드는 무슨 코드 위에서 도는가 → 1부 해부 + 2부 전환 예고 | — | DRAFT |
| 1 | 코드 아닌 설정으로 구분 | 단일 이미지 × AGENT_ID Config = 서로 다른 에이전트 | 1부 | DRAFT |
| 2 | Lazy Init | 5초 기동 + double-checked lock + wake-up 워밍업 (코드 스니펫) | 1부 | DRAFT |
| 3 | 조립 5단계 | Config로드(백오프)→위임도구→내부도구→MCP→Strands Agent | 1부 | DRAFT |
| 4 | persona-injection | contextBoundary → SCOPE ENFORCEMENT 프롬프트 주입 (코드 스니펫). "기억해두라" 복선 | 1부 | DRAFT |
| 5 | depth 가드 | delegationDepth 전파 + max_depth=2 차단 | 1부 | DRAFT |
| 6 | 3중 관측 | OTEL SpanProcessor + Hook + Side-Channel(TTL7, fire-forget) 표 | 1부 | DRAFT |
| 7 | Graceful Degradation | 부품 하나 죽어도 전체는 선다 (1부 요약) | 1부 | DRAFT |
| 8 | 경첩 | "이 400줄이 한 일을 다시 보자" + 매핑 표 + Policy/Harness 분기 통찰 | 전환 | DRAFT |
| 9 | 대체① Registry | 카드 CRUD → Registry(Preview). publish→review→approve, hybrid search, MCP-native | 2부 | DRAFT |
| 10 | 대체② Policy+Harness | persona/depth → Policy(Cedar, Gateway 강제) · 조립루프 → Harness. S4 복선 회수 | 2부 | DRAFT |
| 11 | 대체③ Obs+Eval | 3중 관측 → Observability(통합 뷰) + Evaluations(신규 증강) | 2부 | DRAFT |
| 12 | 무엇이 남는가 | Builder UX·Control Plane·Presentation·도구 구현은 우리 몫 | 2부 | DRAFT |
| 13 | before/after 한눈에 | ASCII 대비도 + 단계적 전환 권장 순서 | 2부 | DRAFT |
| 14 | 마무리 | 직접 지어 증명 → 관리형 위에 다시 세운다. GitHub + GA 추적 브랜치 | — | DRAFT |

## 서사 흐름 (한눈에)

```
[1부 해부]                                    [경첩]   [2부 전환]
설정구분(1)→LazyInit(2)→조립(3)→경계(4)→depth(5)→관측(6)→GD(7) → (8) → Registry(9)→Policy+Harness(10)→Obs+Eval(11)→남는것(12)→종합도(13)→마무리(14)
└── "직접 지은 기계장치를 본다" ──┘            └ 다시보자 ┘ └──── "차별화 안되는 건 관리형으로" ────┘
```

## 출처/사실 검증 (소스 + 공식 문서, 2026-06-06 확인)
- 해부부: `code/agent-runtime/agent_runner.py`(약 400줄)·`harness.py`·`side_channel.py` 직접 대조
  - lazy init `_init_lock` double-check, Config 백오프 `2**attempt`, `delegate_{target}`/`scoped_agent_invoke`,
    `BedrockModel`(기본 `apac.anthropic.claude-sonnet-4-...`, max_tokens=32768), SCOPE ENFORCEMENT 실제 문구,
    `max_depth=2` check_depth, Side-Channel TTL 7일/fire-and-forget
- 전환부: AgentCore 정식 서비스 — Runtime·Harness·Memory·Gateway·Identity·Code Interpreter·Browser·Observability·Payments·Evaluations·Policy·Registry
  - **Registry = Preview**(문서 제목 명시). 나머지 별도 preview 표기 없음
  - Policy: Cedar + 자연어, Gateway에서 모든 tool call 실행 전 결정적 차단, CloudWatch 로깅
  - Harness: 단일 API model/prompt/tools 인라인, 격리 microVM, BYO 컨테이너
  - Evaluations: 세션/트레이스/스팬 단위 자동 평가, 결과 Observability 통합

## 시리즈 연결 (self-built → Managed)
- Part 1 [S3] Registry forward-note → 본편 [S9]에서 회수
- Part 1 [S5] Harness forward-note → 본편 [S10]에서 회수 (Policy+Harness로 분기)
- Part 1 마무리: "베이스 이미지 해부 후 Registry·Policy·Harness·Obs/Eval 전환" 으로 문구 조정됨
- 기존 독립 Deep Dive(`deepdive-agent-runtime-ko.md`)는 본편 1부로 흡수, 파일 삭제. 라인별 분석은 `docs/agent-runner-analysis.md` 유지

## 다음 작업
- Part 1 + Part 2 한글 **확정(DONE)** 후 → 영문화 일괄 진행 (사용자 결정)
- 볼륨 균형 확인: Part 1 ~1,350 어절 / Part 2(2부작) 목표 ~비슷~1.4배 (해부 충실 반영)
- Registry 콘솔 스크린샷 확보 시 [S9]에 삽입 검토
