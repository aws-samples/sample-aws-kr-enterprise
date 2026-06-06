<!--
초안 상태: DRAFT (한국어 1차) — Deep Dive + 관리형 전환 병합본
시리즈: Part 2 (2부작) — 직접 지어보고, 관리형으로 넘긴다
구조: 해부(agent_runner.py 충실 해부) → 경첩 → 전환(managed 1:1 대응)
출처: code/agent-runtime/agent_runner.py·harness.py·side_channel.py (소스 대조) + AgentCore 공식 문서(Registry/Policy/Harness/Observability/Evaluations, 2026-06-06 확인)
동기화 대상: part2-outline.md
연결: part1-draft-ko.md 의 [S3] Builder/Registry · [S5] 신뢰/Harness 시리즈 노트와 직접 이어짐
이미지 경로는 게재 채널에 맞게 후처리. 단어수는 outline 트래커에서 관리.
-->

# 직접 지어보고, 관리형으로 넘긴다 — Agent Runtime 베이스 이미지 해부에서 AgentCore 관리형 서비스 전환까지

## [S0] 들어가며: 조립된 카드는 무슨 코드 위에서 도는가

[Part 1](./part1-draft-ko.md)에서 우리는 에이전트를 *개발하지 않고 조립한다*고 했습니다. Agent Builder에서 Context Boundary·Gateway·Delegation 세 부품을 골라 카드로 등록하면 에이전트가 만들어집니다. 그런데 조립이 끝난 그 카드는 — 도대체 *어떤 코드 위에서* 실행될까요?

놀랍게도, 모든 에이전트가 **단 하나의 동일한 컨테이너 이미지**를 공유합니다. EKS 헬스체크 에이전트도, 비용 분석 에이전트도, 인시던트 RCA Supervisor도 전부 같은 이미지입니다. 이 글은 두 부분으로 나뉩니다. 먼저 그 베이스 이미지의 심장인 `agent_runner.py`(약 400줄)를 **해부**해, Part 1이 "신뢰할 수 있다"고 말한 그 기계장치를 우리가 어떻게 직접 지었는지 봅니다. 그다음, 그렇게 직접 지은 것들이 이제 AgentCore의 **관리형 서비스로 어떻게 대체되는지**를 before/after로 짚습니다.

결론을 먼저 말하면 — *직접 지어보면 그 가치를 알게 되고, 관리형으로 넘기면 직접 지을 필요가 없어진 부분이 꽤 많아졌습니다.*

---

# 1부 · 해부 — 하나의 코드가 서로 다른 에이전트가 되기까지

## [S1] 핵심 발상 — 코드가 아니라 설정으로 에이전트를 구분한다

"EKS 헬스체크 에이전트"와 "비용 분석 에이전트"는 전혀 다른 일을 합니다. 보통이라면 각각 다른 코드를 작성하겠지만, 이 플랫폼은 그렇게 하지 않습니다.

```
하나의 베이스 이미지  ×  AGENT_ID별 Config(DynamoDB)  =  서로 다른 에이전트 인스턴스
```

컨테이너는 환경변수 `AGENT_ID` 하나로만 구분됩니다. 에이전트의 성격을 결정하는 모든 것 — 시스템 프롬프트, 사용할 도구, 위임 대상, 안전 정책 — 은 코드가 아니라 **DynamoDB에 저장된 Config**에 들어 있고, 컨테이너는 기동 시 이 Config(`PK=AGENT#{id}, SK=CONFIG`)를 읽어 자기 자신을 *조립*합니다. Part 1에서 Builder가 만들어 카드로 등록한 그 설정이, 바로 여기서 실행 가능한 에이전트로 살아납니다.

이 한 줄짜리 발상이 운영상 엄청난 차이를 만듭니다. 이미지를 한 번만 빌드해 ECR에 올려두면, 새 에이전트를 100개 추가해도 빌드도 배포 파이프라인도 늘어나지 않습니다. 새 에이전트 = 새 Config 한 건일 뿐입니다.

## [S2] 5초 안에 깨어나되, 무거운 일은 미룬다 — Lazy Initialization

AgentCore Runtime은 컨테이너가 빠르게 헬스체크에 응답하기를 기대합니다(5초 내 기동). 그런데 LLM 모델 핸들 생성, MCP Gateway 연결, 도구 등록은 시간이 걸리는 작업입니다. 이 둘을 어떻게 양립시킬까요?

답은 **지연 초기화(Lazy Init)** 입니다. FastAPI 서버는 즉시 떠서 `/ping`에 응답하고, 무거운 조립은 **첫 번째 실제 요청이 들어온 순간 딱 한 번** 수행합니다. 동시에 여러 요청이 몰려도 조립이 두 번 일어나지 않도록 락(double-checked locking)으로 보호합니다.

```python
def _initialize_agent():
    global _initialized
    if _initialized:
        return
    with _init_lock:          # 동시 요청이 몰려도 조립은 1회만
        if _initialized:
            return
        ...
```

여기에 워밍업 장치도 있습니다. `type: "wake-up"` 요청이 오면 LLM을 호출하지 않고 초기화만 마친 뒤 즉시 `ready`를 반환합니다. 사용자가 첫 질문을 던지기 전에 미리 깨워둘 수 있어, 콜드 스타트 체감을 없앱니다.

## [S3] 조립의 실제 — Config 한 건이 살아 있는 에이전트가 되기까지

초기화 루틴은 Config를 받아 다음 순서로 에이전트를 짜 맞춥니다.

1. **Config 로드** — DynamoDB에서 `AGENT#{id}/CONFIG`를 읽습니다. 배포 직후엔 Config 쓰기와 컨테이너 기동이 경합할 수 있어, **지수 백오프로 3회 재시도**(`2**attempt`초)합니다. 이런 작은 내성이 "방금 만든 에이전트를 눌렀더니 실패"를 막습니다. Config는 immutable — 시작 시 1회만 로드합니다.
2. **위임을 도구로 변환** — Config의 `delegations[]`(다른 에이전트에게 일을 넘기는 관계)을 각각 `delegate_{대상}`이라는 **호출 가능한 도구**(`scoped_agent_invoke` 타입)로 바꿉니다. Supervisor는 하위 에이전트를 "함수처럼" 부르게 됩니다.
3. **내부 도구 등록** — DynamoDB 조회/쓰기, 에이전트 호출(A2A), 파이썬 함수 등을 Strands `@tool`로 래핑합니다. 이때 **한 도구가 실패해도 그 도구만 건너뛰고**(`tool_errors`에 기록) 나머지로 기동을 이어갑니다.
4. **MCP Gateway 연결** — Part 1의 Tool Layer에서 만든 Gateway에 SigV4 서명 transport로 접속합니다. `toolFilter`로 "이 에이전트는 이 Gateway의 이 도구들만" 골라 바인딩할 수 있습니다. 연결 실패는 `BaseException`까지 잡아 skip — Gateway가 죽어 있어도 에이전트 기동 자체는 막지 않습니다.
5. **Strands Agent 조립** — 모델(`BedrockModel`, 기본 `apac.anthropic.claude-sonnet-4-...`, `max_tokens=32768`) + 도구(내부 도구 + MCP) + 시스템 프롬프트 + 훅을 하나의 `Agent` 객체로 묶습니다.

모든 단계는 DynamoDB의 디버그 채널에 `init_start → init_tools → init_complete` 이벤트로 남아, "이 에이전트가 어떤 도구를 들고 깨어났는지"를 나중에 그대로 들여다볼 수 있습니다.

## [S4] 경계를 코드가 아니라 훅으로 강제한다 — persona-injection

Part 1에서 강조한 **Context Boundary**(에이전트의 책임 경계)는 단순한 프롬프트 문구가 아닙니다. 런타임이 이를 **자동으로 강제**합니다.

Config에 `persona-injection` pre-hook이 켜져 있으면, 시스템 프롬프트 끝에 `contextBoundary` 기반 *범위 강제 규칙*이 기계적으로 덧붙습니다. 실제 주입되는 문구는 이런 식입니다.

```python
scope_rule = (
    f"\n\n[SCOPE ENFORCEMENT] Your Context Boundary is: {boundary}. "
    f"You MUST only handle requests within this boundary. "
    f"For any request outside this boundary, do NOT delegate to other agents "
    f"and do NOT use any tools. Instead, respond: "
    f"'This request is outside my scope ({boundary}). ...'"
)
system_prompt = system_prompt + scope_rule
```

엔지니어가 프롬프트에 이 문장을 깜빡 빠뜨려도, 베이스 이미지가 빠짐없이 주입합니다. 경계는 사람의 성실함이 아니라 플랫폼의 기본 동작으로 보장됩니다. 이것이 Part 1에서 말한 *"신뢰할 수 있는"* 에이전트의 한 축입니다. **(이 "프롬프트에 부탁하는" 방식이 2부에서 어떻게 바뀌는지 기억해 두세요.)**

## [S5] 무한 위임을 차단한다 — Depth 가드

에이전트가 다른 에이전트를 부르고, 그 에이전트가 또 다른 에이전트를 부르면 — 잘못하면 무한 루프입니다. 런타임은 모든 요청에 `delegationDepth`를 실어 전파하고, 위임할 때마다 1씩 올립니다. 요청 처리 초입에서 `harness.check_depth(depth)`가 정해진 최대 깊이(기본 `max_depth=2`)를 넘는지 검사해, 초과하면 그 자리에서 거부합니다. A2A(Agent-to-Agent) 위임이 통제 불능으로 번지는 것을 구조적으로 막는 안전핀입니다.

## [S6] 모든 행동을 세 겹으로 관측한다

Part 1의 Trace Viewer(스팬 워터폴)는 마법이 아닙니다. 베이스 이미지가 **세 가지 관측 계층**을 동시에 돌리기 때문에 가능합니다.

| 계층 | 무엇을 보는가 | 어떻게 |
|------|--------------|--------|
| 분산 추적(OTEL) | Supervisor→Domain→MCP 호출의 시간 워터폴 | 커스텀 `AgentSpanProcessor`가 모든 스팬에 `agent.id`/`session.id`/`phase`를 자동 주입 |
| 실시간 이벤트(Hook) | 어떤 도구를 언제 호출/완료했는가 | Strands의 도구 호출 전/후 이벤트를 `ObservabilityHook`이 가로채 기록 |
| 세션 타임라인(Side-Channel) | 한 세션에서 벌어진 모든 일의 시간순 기록 | DynamoDB(`PK=SESSION#{id}`)에 ULID 정렬로 이벤트 적재(TTL 7일) |

특히 세션 타임라인은 **fire-and-forget** 입니다. 기록에 실패해도 에이전트 실행은 멈추지 않습니다. 관측은 "있으면 좋은 것"이지 "실행을 인질로 잡는 것"이 아니라는 설계 철학이 드러나는 대목입니다.

## [S7] 관통하는 설계 원칙 — Graceful Degradation

1부를 한 문장으로 요약하면 **"부품 하나가 고장 나도 전체는 선다"** 입니다. 도구 등록 실패, MCP 연결 실패, 관측 기록 실패 — 어느 것도 에이전트 기동이나 응답을 막지 못합니다. 각 실패는 격리되고, 건너뛰어지고, 흔적만 남습니다.

조직의 자산으로 운영되는 에이전트에게 이것은 사치가 아니라 필수입니다. Gateway 하나가 일시적으로 죽었다고 모든 에이전트가 함께 쓰러진다면, 누구도 이것을 프로덕션에 올리지 못할 테니까요.

---

# 2부 · 전환 — 직접 지은 것을 관리형으로 갈아 끼우기

## [S8] 경첩: 이 400줄이 한 일을 다시 보자

방금 해부한 `agent_runner.py`는 작지만 많은 일을 합니다. 단일 이미지를 Config로 차별화하고(조립 루프), 경계를 프롬프트에 주입하고(persona-injection), 위임 깊이를 검사하고(depth 가드), 모든 행동을 세 겹으로 관측합니다. 그리고 Part 1의 Control Plane은 이 에이전트들을 **카드 레지스트리**(DynamoDB + FastAPI CRUD)로 조회·공유·승인했습니다.

여기서 솔직해질 필요가 있습니다. 이 기계장치의 상당 부분은 **AgentCore가 당시 제공하지 않던 기능을 우리가 직접 메운** 것이었습니다. 직접 지어봤기에 각 부품이 무슨 문제를 푸는지 정확히 압니다 — 그리고 바로 그렇기 때문에, 이제 AgentCore가 같은 일을 관리형으로 해줄 때 무엇을 넘길 수 있는지도 분명합니다.

AgentCore는 이제 에이전트 플랫폼의 횡단 기능을 모듈형 관리 서비스로 제공합니다. 1부에서 해부한 자체 구현을 거기에 포개면 이렇게 정리됩니다.

| 1부에서 직접 지은 것 | 대체할 AgentCore 관리형 서비스 | 핵심 변화 |
|---|---|---|
| 카드 레지스트리 (DynamoDB + Control Plane API) | **AgentCore Registry** *(Preview)* | 조회·승인 카탈로그를 관리형으로. semantic + keyword 하이브리드 검색, MCP-native 엔드포인트 |
| persona-injection + depth 가드 (경계 강제) | **AgentCore Policy** | 경계 규칙을 코드 밖 Cedar 정책으로. Gateway에서 tool call을 결정적으로 가로채 검증 |
| `agent_runner.py` 조립/실행 루프 | **AgentCore Harness** | 모델·프롬프트·도구를 인라인 선언하면 오케스트레이션·도구실행을 관리형이 담당 |
| OTEL + Hook + Side-Channel 3중 관측 | **AgentCore Observability** + **Evaluations** | OTEL 표준 트레이스 통합 뷰 + 자동화된 품질 평가 |

가장 흥미로운 변화는 **우리가 코드 안에 한 덩어리로 짠 안전 로직이, 관리형에서는 성격이 다른 두 서비스로 갈라진다**는 점입니다. persona-injection·depth 가드(경계 강제)는 **Policy**로, 조립·실행 루프는 **Harness**로 내려갑니다. 한 파일에 섞여 있던 두 가지 책임이, 관리형 서비스의 경계선을 통해 비로소 분리되어 보입니다.

## [S9] 대체 ① — 카드 레지스트리 → AgentCore Registry

**Before** — 1부의 에이전트는 "카드"로 등록됩니다. 카드의 실체는 DynamoDB 항목(`PK=AGENT#{id}`)이고, 조회·공유·수정은 Control Plane FastAPI가 직접 구현한 CRUD입니다. 검색은 테이블 스캔에 가깝고, "이 에이전트를 프로덕션에 써도 되는가"라는 승인 절차는 별도 장치 없이 운영 규율에 맡겨졌습니다. 조직이 커지면 두 가지가 아픕니다 — **발견성**(원하는 에이전트를 못 찾아 또 만듦)과 **거버넌스**(검증되지 않은 카드가 그대로 노출됨).

**After** — AgentCore Registry는 바로 이 문제를 정조준한 **완전관리형 디스커버리 서비스**입니다. 에이전트뿐 아니라 MCP 서버·도구·스킬·커스텀 리소스를 하나의 검색 가능한 카탈로그에 등록합니다.

- **거버넌스 워크플로** — `publish → review → approve`가 서비스에 내장됩니다. 퍼블리셔가 레코드를 제출하면 큐레이터가 승인/반려하고, 승인된 것만 발견 대상이 됩니다. 우리가 "운영 규율"에 맡겼던 부분이 플랫폼의 기본 동작이 됩니다.
- **하이브리드 검색** — 시맨틱 이해와 키워드 매칭을 결합해, 자연어 질의("EKS 상태 점검 에이전트")와 정확한 이름 조회를 모두 처리합니다. 테이블 스캔이 검색 엔진으로 바뀝니다.
- **MCP-native 엔드포인트** — Registry 자체가 원격 MCP 엔드포인트로 노출됩니다. 즉 **사람만이 아니라 에이전트가 직접 레지스트리를 검색**해 적절한 도구·하위 에이전트를 찾아 쓸 수 있습니다. 1부의 Supervisor가 `delegations[]`로 위임 대상을 *미리* 알고 있어야 했다면, 이제는 레지스트리에 물어볼 수 있습니다.
- **유연한 구성** — 조직 전체 단일 레지스트리든, 리소스 타입별/개발 단계별로 나누든 자유롭고, 인가는 IAM 또는 IdP의 JWT 중 선택합니다.

> **마이그레이션 메모** — Control Plane의 카드 CRUD를 걷어내는 대신, 에이전트 메타데이터를 Registry 레코드로 게시하도록 바꿉니다. UI의 "레지스트리" 화면은 Registry 검색 API(또는 MCP 엔드포인트)를 호출하는 얇은 클라이언트가 됩니다. *Registry는 현재 Preview이므로, GA 전까지는 자체 구현과 병행하거나 단계적으로 전환하는 것을 권장합니다.*

## [S10] 대체 ② — persona-injection·depth 가드 → Policy, 조립 루프 → Harness

1부에서 본 베이스 이미지의 "Harness" 격 로직은 두 가지 일을 했습니다. (a) 에이전트가 자기 책임 경계를 벗어나지 못하게 막고([S4]의 SCOPE ENFORCEMENT 주입, [S5]의 depth 가드), (b) Config를 읽어 모델·도구·프롬프트를 조립해 실행 루프를 돌렸습니다([S2]~[S3]). 관리형으로 옮기면 이 둘이 갈라집니다.

### ②-a 경계 강제 → AgentCore Policy

**Before** — 경계는 *프롬프트 문구*와 *코드 분기*로 강제됐습니다. persona-injection 훅이 프롬프트 끝에 "범위 밖 요청은 거부하라"를 붙이고, depth 가드가 요청마다 `delegationDepth`를 검사했습니다. 문제는 이것이 **에이전트 코드 안에** 있다는 점입니다 — LLM이 프롬프트 지시를 무시하거나, 프롬프트 주입(prompt injection)으로 우회되면 경계가 무너질 수 있습니다.

**After** — AgentCore Policy는 경계를 **에이전트 코드 밖, Gateway 경계선에서 결정적으로 강제**합니다. 정책 엔진을 만들어 Gateway에 연결하면, **모든 tool call이 실행 전에 정책에 의해 평가**됩니다. 규칙은 [Cedar](https://www.cedarpolicy.com/en)(AWS 오픈소스 정책 언어)로 쓰거나 자연어로 기술하면 Cedar로 변환·검증됩니다. 자연어 작성 시에는 자동 추론으로 "지나치게 허용적/제한적이거나 영원히 만족 불가능한 조건"까지 사전에 잡아줍니다.

이 차이가 본질적입니다. persona-injection은 에이전트에게 경계를 지켜달라고 *부탁*하는 것이고([S4]에서 기억해 두라던 바로 그 방식), Policy는 경계를 지키지 않으면 *애초에 도구 호출이 차단*되는 것입니다. 사용자 신원과 도구 입력 파라미터 단위의 fine-grained 제어가 가능하고, 모든 판정은 CloudWatch에 로깅되어 보안·컴플라이언스 팀이 감사할 수 있습니다. *"신뢰할 수 있는 에이전트"* 의 경계가, 사람의 성실함이나 프롬프트의 견고함이 아니라 **정책 엔진의 결정적 보증**으로 올라섭니다.

### ②-b 실행 루프 → AgentCore Harness

**Before** — `agent_runner.py`가 Config 로드 → 위임을 도구로 변환 → 내부 도구 등록 → MCP 연결 → Strands Agent 조립 → 스트리밍 응답까지 직접 오케스트레이션했습니다([S2]~[S3]에서 해부한 바로 그 400줄).

**After** — AgentCore Harness는 **모델·시스템 프롬프트·도구를 인라인으로 선언하면 단일 API 호출로 에이전트를 정의·실행**해주는 관리형 에이전트 루프입니다. 오케스트레이션, 도구 실행, 메모리 관리, 응답 생성을 서비스가 담당합니다. 각 세션은 파일시스템·셸 접근이 가능한 격리 microVM에서 돌고, 필요하면 커스텀 컨테이너 이미지를 가져올 수도 있습니다. AgentCore Memory·Gateway·Browser·Code Interpreter·Observability와 통합됩니다.

다만 트레이드오프가 있습니다. 1부의 베이스 이미지는 "단일 이미지 × Config"라는 우리 고유의 조립 모델을 코드로 구현한 것이라, 그 *동적 조립 UX*(UI에서 부품을 고르면 즉시 카드가 됨)를 그대로 유지하려면 Harness 위에 여전히 얇은 어댑터가 필요할 수 있습니다. Harness는 "실행 루프를 직접 짜지 않아도 된다"를 해결하지, "우리 플랫폼의 조립 경험"까지 대체하지는 않습니다. **무엇이 남는지는 [S12]에서 다룹니다.**

## [S11] 대체 ③ — 자체 3중 관측성 → Observability + Evaluations

**Before** — 1부는 관측을 세 겹으로 직접 돌렸습니다([S6]) — OTEL 스팬에 `agent.id/session.id/phase`를 주입하는 커스텀 SpanProcessor, Strands 도구 훅 이벤트, DynamoDB Side-Channel 세션 타임라인. Trace Viewer도 이 데이터를 직접 워터폴로 렌더링했습니다.

**After** — 두 관리형 서비스가 이를 받습니다.

- **AgentCore Observability** — OTEL 호환 텔레메트리를 표준 형식으로 받아 에이전트 워크플로의 각 단계를 추적·디버그·모니터링하는 통합 뷰를 제공합니다. 우리가 직접 만든 SpanProcessor·Side-Channel·Trace Viewer가 하던 일을, 표준 OTEL 파이프라인과 CloudWatch 기반 통합 뷰로 대체합니다. 자체 계측을 유지하더라도 *백엔드와 뷰어를 직접 운영할 필요*가 사라집니다.
- **AgentCore Evaluations** — 단순 추적을 넘어, 에이전트가 작업을 얼마나 잘 수행하고 엣지 케이스를 어떻게 다루는지를 **자동·일관·데이터 기반으로 평가**합니다. 세션·트레이스·스팬 단위 평가를 지원하며 결과는 Observability에 통합됩니다. 1부에는 *없던* 계층입니다 — "조립한 에이전트를 배포 전후로 검증"하는 일을 운영 규율이 아니라 서비스로 수행하게 됩니다.

즉 관측성은 *대체*(Observability)에 더해 *증강*(Evaluations)까지 일어납니다. "추적 가능하게 만들어 신뢰한다"는 명제가, "추적 + 정량 평가로 신뢰한다"로 한 단계 올라갑니다.

## [S12] 그래서 무엇이 남는가 — 여전히 직접 지어야 하는 것

관리형으로 갈아 끼운 뒤에도 플랫폼이 통째로 사라지지는 않습니다. AgentCore는 이런 플랫폼을 위한 **"paved path"** 를 제공하지만, *조직 고유의 경험*은 여전히 우리 몫입니다.

- **Control Plane / Builder UX** — "자연어 요구 → 세 부품(Context Boundary·Gateway·Delegation)으로 분해 → 카드 조립"이라는 Part 1의 핵심 경험은 우리 제품의 정체성입니다. 이제 그 백엔드가 Registry·Policy·Harness를 *호출*하도록 바뀔 뿐, 조립 UX 자체는 직접 만듭니다.
- **Presentation Layer** — CloudFront + VPC Origin + 내부 ALB + Cognito 인증 구성은 그대로 우리가 운영합니다.
- **도구의 구현** — Gateway에 등록할 Lambda(boto3/CLI 기반 운영 도구)는 여전히 조직이 작성합니다. 관리형이 표준화·노출·거버넌스를 맡고, *무엇을 하는 도구인지*는 우리가 만듭니다.

정리하면, 관리형 전환의 효과는 **"차별화되지 않는 무거운 일(undifferentiated heavy lifting)을 걷어내는 것"** 입니다. 레지스트리 백엔드, 정책 엔진, 실행 루프, 관측 백엔드 — 누가 만들어도 비슷하고 직접 운영하면 부담스러운 것들을 AgentCore에 넘기고, 우리는 조직 고유의 조립 경험과 도구 자산에 집중합니다.

## [S13] before / after 한눈에

```
[ 1부 — Self-Built ]                       [ 2부 — Managed ]

Card Registry (DynamoDB + API)     ──►   AgentCore Registry (Preview)
persona-injection + depth 가드      ──►   AgentCore Policy (Cedar, Gateway 강제)
agent_runner.py 조립 루프           ──►   AgentCore Harness (관리형 agent loop)
OTEL+Hook+Side-Channel 3중 관측      ──►   AgentCore Observability
(없음)                             ──►   AgentCore Evaluations (신규 증강)

남는 것: Builder UX · Control Plane · Presentation · 도구 구현(Lambda)
```

전환은 한 번에 다 할 필요가 없습니다. 위험이 낮고 효과가 큰 것부터 단계적으로 옮기는 편이 현실적입니다 — 예를 들어 **Observability/Evaluations(표준 OTEL이라 결합이 느슨)** → **Policy(경계 강제를 코드 밖으로)** → **Registry(GA 추적하며 병행)** → **Harness(조립 UX 어댑터 설계 후)** 순서가 한 가지 합리적인 경로입니다.

## [S14] 마무리

1부에서 우리는 "조립한 카드가 무슨 코드 위에서 도는가"를 따라 베이스 이미지를 해부했습니다 — 단일 이미지×Config, 지연 초기화, 경계 강제, depth 가드, 3중 관측, Graceful Degradation. 직접 지어봤기에 각 부품이 무슨 문제를 푸는지 알게 됐습니다.

2부에서는 그중 **차별화되지 않는 부분을 AgentCore 관리형 서비스로 갈아 끼워** 더 적은 코드로 같은 — 더 강한 — 보증을 얻는 길을 봤습니다. 카탈로그는 Registry로, 경계는 Policy로, 실행 루프는 Harness로, 관측은 Observability와 Evaluations로 내려갑니다. 우리에게 남는 것은 조직 고유의 조립 경험과 도구 자산 — 정확히 *차별화되는* 부분입니다.

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
- 라인별 상세 분석: `docs/agent-runner-analysis.md`
