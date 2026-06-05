<!--
블로그 심화 편 (Deep Dive) — 한국어 1차 초안
출처 분석: docs/agent-runner-analysis.md (code/agent-runtime/agent_runner.py)
연결: part1-draft-ko.md 의 [S3] Agent Builder · [S5] 신뢰/Harness 와 이어지는 기술 심화
용도: 별도 게시물 또는 Part 1의 부록/박스 글로 발췌 사용
-->

# 조립한 에이전트는 어떻게 '하나의 코드'로 실행되는가 — Agent Runtime 베이스 이미지 해부

> Part 1에서 우리는 에이전트를 *개발하지 않고 조립한다*고 했습니다. 그렇다면 조립이 끝난 카드는 도대체 어떤 코드 위에서 실행될까요? 놀랍게도, 모든 에이전트가 **단 하나의 동일한 컨테이너 이미지**를 공유합니다. 이 글은 그 베이스 이미지의 심장인 `agent_runner.py`(약 400줄)를 해부합니다.

## 핵심 발상 — 코드가 아니라 설정으로 에이전트를 구분한다

"EKS 헬스체크 에이전트"와 "비용 분석 에이전트"는 전혀 다른 일을 합니다. 보통이라면 각각 다른 코드를 작성하겠지만, 이 플랫폼은 그렇게 하지 않습니다.

```
하나의 베이스 이미지  ×  AGENT_ID별 Config(DynamoDB)  =  서로 다른 에이전트 인스턴스
```

컨테이너는 환경변수 `AGENT_ID` 하나로만 구분됩니다. 에이전트의 성격을 결정하는 모든 것 — 시스템 프롬프트, 사용할 도구, 위임 대상, 안전 정책 — 은 코드가 아니라 **DynamoDB에 저장된 Config**에 들어 있고, 컨테이너는 기동 시 이 Config를 읽어 자기 자신을 *조립*합니다. Part 1에서 Builder가 만들어 카드로 등록한 그 설정이, 바로 여기서 실행 가능한 에이전트로 살아납니다.

이 한 줄짜리 발상이 운영상 엄청난 차이를 만듭니다. 이미지를 한 번만 빌드해 ECR에 올려두면, 새 에이전트를 100개 추가해도 빌드도 배포 파이프라인도 늘어나지 않습니다. 새 에이전트 = 새 Config 한 건일 뿐입니다.

## 1. 5초 안에 깨어나되, 무거운 일은 미룬다 — Lazy Initialization

AgentCore Runtime은 컨테이너가 빠르게 헬스체크에 응답하기를 기대합니다. 그런데 LLM 모델 핸들 생성, MCP Gateway 연결, 도구 등록은 시간이 걸리는 작업입니다. 이 둘을 어떻게 양립시킬까요?

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

## 2. 조립의 실제 — Config 한 건이 살아 있는 에이전트가 되기까지

초기화 루틴은 Config를 받아 다음 순서로 에이전트를 짜 맞춥니다.

1. **Config 로드** — DynamoDB에서 `AGENT#{id}/CONFIG`를 읽습니다. 배포 직후엔 Config 쓰기와 컨테이너 기동이 경합할 수 있어, **지수 백오프로 3회 재시도**합니다. 이런 작은 내성이 "방금 만든 에이전트를 눌렀더니 실패"를 막습니다.
2. **위임을 도구로 변환** — Config의 `delegations[]`(다른 에이전트에게 일을 넘기는 관계)을 각각 `delegate_{대상}`이라는 **호출 가능한 도구**로 바꿉니다. Supervisor는 하위 에이전트를 "함수처럼" 부르게 됩니다.
3. **내부 도구 등록** — DynamoDB 조회/쓰기, 에이전트 호출(A2A), 파이썬 함수 등을 Strands `@tool`로 래핑합니다. 이때 **한 도구가 실패해도 그 도구만 건너뛰고** 나머지로 기동을 이어갑니다.
4. **MCP Gateway 연결** — Part 1의 Tool Layer에서 만든 Gateway에 SigV4 서명으로 접속합니다. `toolFilter`로 "이 에이전트는 이 Gateway의 이 도구들만" 골라 바인딩할 수 있습니다. Gateway가 죽어 있어도 에이전트 기동 자체는 막지 않습니다.
5. **Strands Agent 조립** — 모델(Bedrock) + 도구(내부 도구 + MCP) + 시스템 프롬프트 + 훅을 하나의 `Agent` 객체로 묶습니다.

모든 단계는 DynamoDB의 디버그 채널에 `init_start → init_tools → init_complete` 이벤트로 남아, "이 에이전트가 어떤 도구를 들고 깨어났는지"를 나중에 그대로 들여다볼 수 있습니다.

## 3. 경계를 코드가 아니라 훅으로 강제한다 — persona-injection

Part 1에서 강조한 **Context Boundary**(에이전트의 책임 경계)는 단순한 프롬프트 문구가 아닙니다. 런타임이 이를 **자동으로 강제**합니다.

Config에 `persona-injection` 훅이 켜져 있으면, 시스템 프롬프트 끝에 다음과 같은 *범위 강제 규칙*이 기계적으로 덧붙습니다.

> "당신의 Context Boundary는 [경계]입니다. 이 경계 밖의 요청은 다른 에이전트에 위임하지도, 도구를 쓰지도 말고, '제 담당 범위를 벗어납니다'라고 답하세요."

엔지니어가 프롬프트에 이 문장을 깜빡 빠뜨려도, 베이스 이미지가 빠짐없이 주입합니다. 경계는 사람의 성실함이 아니라 플랫폼의 기본 동작으로 보장됩니다. 이것이 Part 1에서 말한 *"신뢰할 수 있는"* 에이전트의 한 축입니다.

## 4. 무한 위임을 차단한다 — Depth 가드

에이전트가 다른 에이전트를 부르고, 그 에이전트가 또 다른 에이전트를 부르면 — 잘못하면 무한 루프입니다. 런타임은 모든 요청에 `delegationDepth`를 실어 전파하고, 위임할 때마다 1씩 올립니다. 정해진 최대 깊이(기본 2)를 넘으면 그 자리에서 거부합니다. A2A(Agent-to-Agent) 위임이 통제 불능으로 번지는 것을 구조적으로 막는 안전핀입니다.

## 5. 모든 행동을 세 겹으로 관측한다

Part 1의 Trace Viewer(스팬 워터폴)는 마법이 아닙니다. 베이스 이미지가 **세 가지 관측 계층**을 동시에 돌리기 때문에 가능합니다.

| 계층 | 무엇을 보는가 | 어떻게 |
|------|--------------|--------|
| 분산 추적(OTEL) | Supervisor→Domain→MCP 호출의 시간 워터폴 | 모든 스팬에 `agent.id`/`session.id`/`phase`를 자동 주입 |
| 실시간 이벤트(Hook) | 어떤 도구를 언제 호출/완료했는가 | Strands의 도구 호출 전/후 이벤트를 가로채 기록 |
| 세션 타임라인(Side-Channel) | 한 세션에서 벌어진 모든 일의 시간순 기록 | DynamoDB에 ULID 정렬로 이벤트 적재(7일 TTL) |

특히 세션 타임라인은 **fire-and-forget** 입니다. 기록에 실패해도 에이전트 실행은 멈추지 않습니다. 관측은 "있으면 좋은 것"이지 "실행을 인질로 잡는 것"이 아니라는 설계 철학이 드러나는 대목입니다.

## 관통하는 설계 원칙 — Graceful Degradation

이 베이스 이미지를 한 문장으로 요약하면 **"부품 하나가 고장 나도 전체는 선다"** 입니다. 도구 등록 실패, MCP 연결 실패, 관측 기록 실패 — 어느 것도 에이전트 기동이나 응답을 막지 못합니다. 각 실패는 격리되고, 건너뛰어지고, 흔적만 남습니다.

조직의 자산으로 운영되는 에이전트에게 이것은 사치가 아니라 필수입니다. Gateway 하나가 일시적으로 죽었다고 모든 에이전트가 함께 쓰러진다면, 누구도 이것을 프로덕션에 올리지 못할 테니까요.

---

### 더 깊이 — 모듈 지도

`agent_runner.py`는 직접 모든 일을 하지 않고, 전문 모듈을 *오케스트레이션*합니다.

```
agent_runner.py  (FastAPI 진입점 + 조립자)
├── config_loader.py        Config 로드(DynamoDB)
├── internal_tools/         도구 팩토리(DynamoDB · A2A invoke · python_function)
├── mcp_connector.py        MCP Gateway 연결 + SigV4 서명
├── harness.py              Tier2Harness — 범위/깊이/위임 검증
├── observability_hook.py   도구 호출 이벤트 → Side-Channel
├── side_channel.py         세션 이벤트 기록(DynamoDB, TTL 7일)
└── span_filter.py          OTEL 스팬 context enrichment
```

> 전체 라인별 상세 분석은 레퍼런스 문서 [`docs/agent-runner-analysis.md`](../agent-runner-analysis.md) 참고.
