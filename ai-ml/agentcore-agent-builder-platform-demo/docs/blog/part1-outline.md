# Part 1 초안 — Living Outline

> 초안(`part1-draft-ko.md`)과 sync를 맞추는 살아있는 아웃라인.
> 섹션을 채울 때마다 상태(Status)와 실제 단어수(Actual)를 갱신한다.

**Thesis**: 운영 클라우드 환경에서 AI 에이전트를 *개발하지 않고 조립*해 자산으로
공유·운영한다. 두 축 — **조립한다(70%)** + **Harness가 보증한다(30%)**.

**골격**: A (문제 → 전환 → 조립 → 보증) · 단편 ~2550단어 · 한국어

## Status Legend
`TODO` 미작성 · `DRAFT` 초안작성됨 · `REVIEW` 검토필요 · `DONE` 확정

> 실제(Actual) 단위 = 한국어 어절(띄어쓰기 토큰). 영문화 시 영어 ~2550단어 목표에 대응.
> 초안(`part1-draft-ko.md`)의 각 제목에 `[S0]`~`[S7]` 태그가 붙어 있어 아래 # 열과 1:1 대조됩니다.

## 섹션 트래커

| # | 섹션 | 한 줄 요지 | 목표 | 실제 | 비중 | 시각자료 | Status |
|---|------|-----------|------|------|------|---------|--------|
| 0 | 도입(Hook) | "한 명이 자기 도구 만드는" 시대 → 운영 조직 10명+의 벽(중복개발·신뢰불가·거버넌스 부재) | 250 | 151 | 문제 | — | DONE |
| 1 | 왜 '개발'이 아니라 '조립'인가 | 에이전트를 코드 아닌 자산(Card)으로. Context Boundary·Gateway·Delegation을 부품으로 | 350 | 166 | 조립 | demo-02-builder | DONE |
| 2 | 전체 아키텍처 | 4레이어. CloudFront VPC Origin + ECS Fargate + AgentCore + Gateway(Lambda→MCP) | 350 | 147 | — | architecture.png | DONE |
| 3 | 조립의 핵심 ① Agent Builder | 요구사항 입력 → 단계별 조립 → Card 등록. (+Managed Registry forward-note) | 450 | 170 | 조립 | demo-03-agents | DONE |
| 4 | 조립의 핵심 ② Gateway 도구 | 구현(Lambda boto3/CLI) ↔ 인터페이스(MCP) 분리. Gateway=MCP 프로토콜로 변환·합성. 재사용으로 중복개발 해소 | 400 | 236 | 조립 | — | DONE |
| 5 | 신뢰하기 — 관측성/Harness | Supervisor→Domain→MCP 스팬 워터폴, Trace Viewer. (+Managed Harness forward-note) | 350 | 171 | 보증 | demo-04-traces | DONE |
| 6 | 배포 — 15분 one-shot | deploy-all.sh 6 Phase, Terraform 8모듈, zero-prereq | 250 | 165 | — | Phase 표 | DONE |
| 7 | 마무리 + CTA + Part 2 예고 | 조직 자산화 의미 + GitHub/배포 링크 + 다음 편(Managed 전환) 예고 | 150 | 125 | — | — | DONE |

**목표 합계**: ~2550(영문 기준) / **실제 합계**: ~1,350 어절(한국어 초안)

## 서사 흐름 (한눈에)

```
문제(0) → 전환선언(1) → 큰그림(2) → 조립①(3) → 조립②(4) → 신뢰(5) → 배포(6) → CTA(7)
└─ 왜 ─┘  └──────── 무엇을/어떻게 (조립 70%) ────────┘  └ 보증30% ┘ └ 실행 ┘
```

## 시리즈 연결 (self-built → Managed)
- 1편 = 현재 빌딩블록으로 **직접 조립한** 버전
- 섹션 3·5에 forward-note 1문장씩 → 섹션 7에서 Part 2(Managed 전환) 예고
- Part 2 상세: `../superpowers/specs/2026-05-29-tech-blog-part1-design.md` 의 "Planned follow-up"
