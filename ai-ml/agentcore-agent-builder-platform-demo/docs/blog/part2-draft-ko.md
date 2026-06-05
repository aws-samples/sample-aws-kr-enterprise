<!--
초안 상태: DRAFT (한국어 1차)
시리즈: Part 2 — self-built → Managed 전환
출처: AgentCore 공식 문서(Registry/Policy/Harness/Observability/Evaluations) + Part 1 자체 구현 분석
동기화 대상: part2-outline.md
연결: part1-draft-ko.md 의 [S3] Builder/Registry · [S5] 신뢰/Harness 시리즈 노트와 직접 이어짐
이미지 경로는 게재 채널에 맞게 후처리. 단어수는 outline 트래커에서 관리.
-->

# 직접 지은 것을 관리형으로 갈아 끼우기 — AgentCore 관리형 Registry·Policy·Harness로 플랫폼을 단순화하기

## [S0] 들어가며: Part 1에서 우리가 '직접 지은' 것들

[Part 1](./part1-draft-ko.md)에서 우리는 에이전트를 *개발하지 않고 조립하는* 엔터프라이즈 플랫폼을 만들었습니다. Agent Builder로 부품을 조립하고, MCP Gateway로 도구를 재사용하고, 관측성/Harness로 조립한 것을 신뢰했습니다. 데모는 `deploy-all.sh` 한 번으로 잘 돌아갑니다.

하지만 솔직해질 필요가 있습니다. 그 플랫폼의 상당 부분은 **AgentCore가 당시 제공하지 않던 기능을 우리가 직접 메운** 것이었습니다. 구체적으로 세 군데입니다.

- **카드 레지스트리** — 에이전트를 조회·공유·승인하는 카탈로그를 DynamoDB 테이블과 Control Plane API로 직접 구현했습니다.
- **Harness** — 에이전트의 책임 경계를 강제하고(persona-injection), 무한 위임을 막는(depth guard) 안전 로직을 베이스 이미지 코드(`agent_runner.py`, `harness.py`) 안에 직접 짰습니다.
- **관측성** — OTEL 스팬 가공, Strands 훅 이벤트, DynamoDB Side-Channel을 묶은 3중 관측 계층을 직접 운영했습니다.

이 글은 그 자체 구현 세 가지를, AgentCore에 **새로 추가된 관리형 서비스**로 어떻게 갈아 끼우는지를 before/after로 보여줍니다. 결론을 먼저 말하면 — *직접 지을 필요가 없어진 부분이 꽤 많아졌습니다.*

## [S1] 전환의 지도 — 무엇이 무엇으로 대체되는가

AgentCore는 이제 에이전트 플랫폼을 구성하는 횡단 기능들을 모듈형 관리 서비스로 제공합니다. Part 1의 자체 구현을 거기에 포개면 다음과 같이 정리됩니다.

| Part 1에서 직접 지은 것 | 대체할 AgentCore 관리형 서비스 | 핵심 변화 |
|---|---|---|
| 카드 레지스트리 (DynamoDB + Control Plane API) | **AgentCore Registry** *(Preview)* | 조회·승인 카탈로그를 관리형으로. semantic + keyword 하이브리드 검색, MCP-native 엔드포인트 |
| Harness — 경계 강제(persona-injection) | **AgentCore Policy** | 경계 규칙을 코드 밖 Cedar 정책으로. Gateway에서 tool call을 결정적으로 가로채 검증 |
| Harness — 에이전트 조립/실행 루프 | **AgentCore Harness** | 모델·프롬프트·도구를 인라인으로 선언하면 오케스트레이션·도구실행을 관리형이 담당 |
| 관측성 — OTEL + Hook + Side-Channel 3중 | **AgentCore Observability** + **Evaluations** | OTEL 표준 트레이스 통합 뷰 + 자동화된 품질 평가 |

여기서 가장 흥미로운 변화는 **자체 Harness 하나가 두 개의 다른 관리형 서비스로 갈라진다**는 점입니다. 우리가 "Harness"라는 한 단어로 묶었던 책임이, 사실은 *경계 강제(Policy)* 와 *실행 루프(Harness)* 라는 성격이 다른 두 일이었음이 관리형 서비스의 경계선을 통해 드러납니다.

## [S2] 대체 ① — 카드 레지스트리 → AgentCore Registry

### Before — 우리가 직접 지은 카탈로그

Part 1에서 에이전트는 "카드"로 등록됩니다. 그 카드의 실체는 DynamoDB 항목(`PK=AGENT#{id}`)이고, 조회·공유·수정은 모두 Control Plane의 FastAPI가 직접 구현한 CRUD입니다. 검색은 테이블 스캔에 가깝고, "누가 이 에이전트를 프로덕션에 써도 되는가"라는 승인 절차는 별도 장치 없이 운영 규율에 맡겨졌습니다. 조직이 커지면 두 가지가 아픕니다 — **발견성**(원하는 에이전트를 못 찾아 또 만듦)과 **거버넌스**(검증되지 않은 카드가 그대로 노출됨).

### After — AgentCore Registry (Preview)

AgentCore Registry는 바로 이 문제를 정조준한 **완전관리형 디스커버리 서비스**입니다. 에이전트뿐 아니라 MCP 서버·도구·스킬·커스텀 리소스를 하나의 검색 가능한 카탈로그에 등록합니다. 우리가 직접 구현했던 카드 레지스트리가 그대로 관리형 기능으로 올라옵니다.

- **거버넌스 워크플로** — `publish → review → approve`가 서비스에 내장됩니다. 퍼블리셔가 레코드를 제출하면 큐레이터가 승인/반려하고, 승인된 것만 발견 대상이 됩니다. 우리가 "운영 규율"에 맡겼던 부분이 플랫폼의 기본 동작이 됩니다.
- **하이브리드 검색** — 시맨틱 이해와 키워드 매칭을 결합해, 자연어 질의("EKS 상태 점검 에이전트")와 정확한 이름 조회를 모두 처리합니다. 테이블 스캔이 검색 엔진으로 바뀝니다.
- **MCP-native 엔드포인트** — Registry 자체가 원격 MCP 엔드포인트로 노출됩니다. 즉 **사람만이 아니라 에이전트가 직접 레지스트리를 검색**해 적절한 도구·하위 에이전트를 찾아 쓸 수 있습니다. Part 1의 Supervisor가 "어떤 Domain 에이전트에게 위임할지"를 코드로 알고 있어야 했다면, 이제는 레지스트리에 물어볼 수 있습니다.
- **유연한 구성** — 조직 전체 단일 레지스트리든, 리소스 타입별(에이전트/MCP/스킬)이든, 개발 단계별(prod/QA/dev)이든 나눠 만들 수 있고, 인가는 IAM 또는 IdP의 JWT 중 선택합니다.

> **마이그레이션 메모** — Control Plane의 카드 CRUD를 걷어내는 대신, 에이전트 메타데이터를 Registry 레코드로 게시하도록 바꿉니다. UI의 "레지스트리" 화면은 Registry의 검색 API(또는 MCP 엔드포인트)를 호출하는 얇은 클라이언트가 됩니다. *(Preview 서비스이므로 GA 전까지는 자체 구현과 병행하거나 단계적으로 전환하는 것을 권장합니다.)*

## [S3] 대체 ② — 자체 Harness → Policy + Harness

Part 1의 베이스 이미지가 직접 짠 "Harness"는 두 가지 일을 했습니다. (a) 에이전트가 자기 책임 경계를 벗어나지 못하게 막고(persona-injection으로 시스템 프롬프트에 SCOPE ENFORCEMENT 주입, 위임 깊이 `max_depth=2` 가드), (b) Config를 읽어 모델·도구·프롬프트를 조립해 실행 루프를 돌렸습니다. 관리형으로 옮기면 이 둘이 갈라집니다.

### ②-a 경계 강제 → AgentCore Policy

**Before**: 경계는 *프롬프트 문구*와 *코드 분기*로 강제됐습니다. persona-injection 훅이 프롬프트 끝에 "범위 밖 요청은 거부하라"를 붙이고, depth 가드가 요청마다 `delegationDepth`를 검사했습니다. 문제는 이것이 **에이전트 코드 안에** 있다는 점입니다 — LLM이 프롬프트 지시를 무시하거나, 프롬프트 주입(prompt injection)으로 우회되면 경계가 무너질 수 있습니다.

**After**: AgentCore Policy는 경계를 **에이전트 코드 밖, Gateway 경계선에서 결정적으로 강제**합니다. 정책 엔진을 만들어 Gateway에 연결하면, **모든 tool call이 실행 전에 정책에 의해 평가**됩니다. 규칙은 [Cedar](https://www.cedarpolicy.com/en)(AWS 오픈소스 정책 언어)로 쓰거나 자연어로 기술하면 Cedar로 변환·검증됩니다. 자연어 작성 시에는 자동 추론으로 "지나치게 허용적/제한적이거나 영원히 만족 불가능한 조건"까지 사전에 잡아줍니다.

이 차이가 본질적입니다. persona-injection은 "에이전트에게 경계를 지켜달라고 *부탁*"하는 것이고, Policy는 "경계를 지키지 않으면 *애초에 도구 호출이 차단*"되는 것입니다. 사용자 신원과 도구 입력 파라미터 단위의 fine-grained 제어가 가능하고, 모든 판정은 CloudWatch에 로깅되어 보안·컴플라이언스 팀이 감사할 수 있습니다. Part 1에서 *"신뢰할 수 있는 에이전트"* 의 한 축이라 불렀던 경계가, 사람의 성실함이나 프롬프트의 견고함이 아니라 **정책 엔진의 결정적 보증**으로 올라섭니다.

### ②-b 실행 루프 → AgentCore Harness

**Before**: `agent_runner.py`(약 400줄)가 Config 로드 → 위임을 도구로 변환 → 내부 도구 등록 → MCP 연결 → Strands Agent 조립 → 스트리밍 응답까지 직접 오케스트레이션했습니다. (이 코드의 해부는 [심화편](./deepdive-agent-runtime-ko.md)에서 다뤘습니다.)

**After**: AgentCore Harness는 **모델·시스템 프롬프트·도구를 인라인으로 선언하면 단일 API 호출로 에이전트를 정의·실행**해주는 관리형 에이전트 루프입니다. 오케스트레이션, 도구 실행, 메모리 관리, 응답 생성을 서비스가 담당합니다. 각 세션은 파일시스템·셸 접근이 가능한 격리 microVM에서 돌고, 필요하면 커스텀 컨테이너 이미지를 가져올 수도 있습니다. AgentCore Memory·Gateway·Browser·Code Interpreter·Observability와 통합됩니다.

다만 여기엔 트레이드오프가 있습니다. Part 1의 베이스 이미지는 "단일 이미지 × Config" 라는 우리 고유의 조립 모델을 코드로 구현한 것이라, 그 *동적 조립 UX*(UI에서 부품을 고르면 즉시 카드가 됨)를 그대로 유지하려면 Harness 위에 여전히 얇은 어댑터가 필요할 수 있습니다. Harness는 "실행 루프를 직접 짜지 않아도 된다"를 해결하지, "우리 플랫폼의 조립 경험"까지 대체하지는 않습니다. **무엇이 남는지는 [S5]에서 다룹니다.**

## [S4] 대체 ③ — 자체 3중 관측성 → Observability + Evaluations

**Before**: Part 1은 관측을 세 겹으로 직접 돌렸습니다 — OTEL 스팬에 `agent.id/session.id/phase`를 주입하는 커스텀 SpanProcessor, Strands 도구 훅 이벤트, DynamoDB Side-Channel 세션 타임라인(TTL 7일, fire-and-forget). Trace Viewer도 이 데이터를 직접 워터폴로 렌더링했습니다.

**After**: 두 관리형 서비스가 이를 받습니다.

- **AgentCore Observability** — OTEL 호환 텔레메트리를 표준 형식으로 받아 에이전트 워크플로의 각 단계를 추적·디버그·모니터링하는 통합 뷰를 제공합니다. 우리가 직접 만든 SpanProcessor·Side-Channel·Trace Viewer가 하던 일을, 표준 OTEL 파이프라인과 CloudWatch 기반 통합 뷰로 대체합니다. 자체 계측을 유지하더라도 *백엔드와 뷰어를 직접 운영할 필요*가 사라집니다.
- **AgentCore Evaluations** — 단순 추적을 넘어, 에이전트가 작업을 얼마나 잘 수행하고 엣지 케이스를 어떻게 다루는지를 **자동·일관·데이터 기반으로 평가**합니다. 세션·트레이스·스팬 단위 평가를 지원하며 결과는 Observability에 통합됩니다. Part 1에는 *없던* 계층입니다 — "조립한 에이전트를 배포 전후로 검증"하는 일을 운영 규율이 아니라 서비스로 수행하게 됩니다.

즉 관측성은 *대체*(Observability)에 더해 *증강*(Evaluations)까지 일어납니다. "추적 가능하게 만들어 신뢰한다"는 Part 1의 명제가, "추적 + 정량 평가로 신뢰한다"로 한 단계 올라갑니다.

## [S5] 그래서 무엇이 남는가 — 여전히 직접 지어야 하는 것

관리형으로 갈아 끼운 뒤에도 플랫폼이 통째로 사라지지는 않습니다. AgentCore는 이런 플랫폼을 위한 **"paved path"** 를 제공하지만, *조직 고유의 경험*은 여전히 우리 몫입니다.

- **Control Plane / Builder UX** — "자연어 요구 → 세 부품(Context Boundary·Gateway·Delegation)으로 분해 → 카드 조립"이라는 Part 1의 핵심 경험은 우리 제품의 정체성입니다. 이제 그 백엔드가 Registry·Policy·Harness를 *호출*하도록 바뀔 뿐, 조립 UX 자체는 직접 만듭니다.
- **Presentation Layer** — CloudFront + VPC Origin + 내부 ALB + Cognito 인증 구성은 그대로 우리가 운영합니다.
- **도구의 구현** — Gateway에 등록할 Lambda(boto3/CLI 기반 운영 도구)는 여전히 조직이 작성합니다. 관리형이 표준화·노출·거버넌스를 맡고, *무엇을 하는 도구인지*는 우리가 만듭니다.

정리하면, 관리형 전환의 효과는 **"차별화되지 않는 무거운 일(undifferentiated heavy lifting)을 걷어내는 것"** 입니다. 레지스트리 백엔드, 정책 엔진, 실행 루프, 관측 백엔드 — 누가 만들어도 비슷하고 직접 운영하면 부담스러운 것들을 AgentCore에 넘기고, 우리는 조직 고유의 조립 경험과 도구 자산에 집중합니다.

## [S6] before / after 한눈에

```
[ Part 1 — Self-Built ]                    [ Part 2 — Managed ]

Card Registry (DynamoDB + API)    ──►   AgentCore Registry (Preview)
persona-injection + depth guard   ──►   AgentCore Policy (Cedar, Gateway 강제)
agent_runner.py 조립 루프          ──►   AgentCore Harness (관리형 agent loop)
OTEL+Hook+Side-Channel 3중 관측    ──►   AgentCore Observability
(없음)                            ──►   AgentCore Evaluations (신규 증강)

남는 것: Builder UX · Control Plane · Presentation · 도구 구현(Lambda)
```

전환은 한 번에 다 할 필요가 없습니다. Registry가 Preview인 점을 감안하면, 위험이 낮고 효과가 큰 것부터 단계적으로 옮기는 편이 현실적입니다 — 예를 들어 **Observability/Evaluations(표준 OTEL이라 결합이 느슨)** → **Policy(경계 강제를 코드 밖으로)** → **Registry(GA 추적하며 병행)** → **Harness(조립 UX 어댑터 설계 후)** 순서가 한 가지 합리적인 경로입니다.

## [S7] 마무리

Part 1은 *현재의 빌딩블록으로 직접 조립한* 엔터프라이즈 에이전트 플랫폼이었습니다. Part 2는 그중 **차별화되지 않는 부분을 AgentCore 관리형 서비스로 갈아 끼워** 더 적은 코드로 같은 — 더 강한 — 보증을 얻는 길을 보여줬습니다. 카탈로그는 Registry로, 경계는 Policy로, 실행 루프는 Harness로, 관측은 Observability와 Evaluations로 내려갑니다. 우리에게 남는 것은 조직 고유의 조립 경험과 도구 자산 — 정확히 *차별화되는* 부분입니다.

개인의 도구가 조직의 자산이 되는 여정에서, 직접 지어 증명한 패턴을 관리형 기반 위에 다시 세우는 것이 다음 한 걸음입니다. 전체 코드는 [aws-samples GitHub](https://github.com/aws-samples/sample-aws-kr-enterprise)에 있으며, 관리형 전환 버전은 각 서비스의 GA 진행에 맞춰 별도 브랜치로 업데이트할 예정입니다.

---

## 참고

- [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/)
- [AgentCore Registry (Preview)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry.html)
- [Policy in AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html)
- [AgentCore Harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness.html)
- [AgentCore Observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html)
- [AgentCore Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations.html)
- [Cedar policy language](https://www.cedarpolicy.com/en)
- [Part 1 — AI 에이전트를 개발하지 않고 조립한다](./part1-draft-ko.md)
- [심화편 — Agent Runtime 베이스 이미지 해부](./deepdive-agent-runtime-ko.md)
