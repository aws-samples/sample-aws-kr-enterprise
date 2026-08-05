# Agent 생성·배포 가이드 / Agent Build & Deploy Guide

> 웹 UI에서 자연어로 Agent를 조립하고 AgentCore Runtime으로 배포한 뒤 Playground에서 테스트하는 단계별 가이드입니다.
> A step-by-step guide to assembling an agent in natural language, deploying it to an AgentCore Runtime, and testing it in the Playground — all from the web UI.

[한국어](#한국어) | [English](#english)

---

## 한국어

### 사전 준비물

- 플랫폼이 배포되어 있어야 합니다 (`./scripts/deploy-all.sh` 완료). 특히 **Phase 2(이미지 빌드)**가 끝나 ECR에 `base-image`가 있어야 "Deploy" 기능이 동작합니다.
- **Cognito 계정** — 로그인에 필요합니다.
- **Bedrock 모델 액세스** (`AWS_REGION`에서 활성화):
  - 에이전트 실행용 **Claude Sonnet** (`global.anthropic.claude-sonnet-4-6`)
  - Agent Builder가 사용하는 **Claude Opus** (`global.anthropic.claude-opus-4-6-v1`)
  - 모델 액세스가 없으면 배포는 성공해도 Builder 대화와 Playground 호출이 **호출 시점에 실패**합니다.
- **플랫폼 URL** — custom domain(`DOMAIN_NAME`)을 설정하지 않았다면 **CloudFront 기본 도메인**입니다. `deploy-all.sh` 출력 또는 CloudFront 콘솔에서 배포 도메인을 확인하세요.

### 1. 로그인 (Cognito)

1. 브라우저에서 플랫폼 URL(CloudFront 기본 도메인)로 접속합니다.
2. 로그인 화면(**"AIOps Platform / Sign in to continue"**)이 나타나면 Cognito **Email**과 **Password**를 입력하고 **Sign in**을 클릭합니다.
3. 인증에 성공하면 액세스 토큰이 브라우저에 저장되고 대시보드로 진입합니다. 이후 모든 API 호출에 이 토큰이 사용됩니다.
4. 왼쪽 사이드바에서 **Agent Builder**, **Agent Registry**, **Trace Viewer** 등으로 이동할 수 있습니다.

### 2. Agent Builder — 자연어로 Agent 조립

1. 사이드바에서 **Agent Builder**(`/builder`)를 엽니다.
2. 하단 입력창("*Agent에 대한 요구사항을 설명하세요...*")에 만들고 싶은 Agent를 **자연어로 설명**합니다. 예시 프롬프트가 제공됩니다:
   - "EKS 클러스터의 Pod 상태를 모니터링하는 Agent를 만들어줘"
   - "CloudWatch 알람 기반으로 인시던트를 자동 생성하는 Agent"
   - "보안 감사 리포트를 매주 생성하는 Agent를 설계해줘"
3. 어시스턴트가 응답을 **스트리밍**하며, 화면 하단의 단계 표시줄이 `Start › Intent › Boundary › Tools › Delegation › Config › Complete` 순으로 진행됩니다. 어시스턴트가 추가 질문을 하면 답하며 대화를 이어갑니다.
4. Config가 생성되면 오른쪽에 **"Generated Agent Config"** 패널이 나타납니다. 여기에는:
   - **유효성 경고** (예: "systemPrompt가 비어있거나 너무 짧습니다", "Gateway가 설정되지 않았습니다") — 있으면 대화로 보완하세요.
   - **Model** 드롭다운 — `Claude Sonnet 4.6` / `Claude Opus 4.6` / `Claude Haiku 4.5` 중 선택 (기본값 Sonnet 4.6).
   - 생성된 **config JSON** 미리보기.
5. 모델을 선택한 뒤 **[Save Agent]** 버튼을 클릭합니다. 저장이 완료되면 버튼이 **"Saved (agentId)"** 배지로 바뀌고, 그 옆에 **[Deploy Runtime]** 버튼이 나타납니다.

> **중요 — Save와 Deploy는 별개의 단계입니다.** `Save Agent`는 config를 Registry에 등록만 할 뿐 Runtime을 만들지 않습니다. 실제로 Agent를 사용하려면 다음 단계(Deploy)를 반드시 수행해야 합니다.

### 3. Deploy — AgentCore Runtime 배포

Deploy 진입점은 **두 곳**입니다. 어느 쪽이든 동일한 배포 API를 호출합니다.

**A. Builder에서 바로 배포 (Save 직후)**

1. `Save Agent` 후 나타난 **[Deploy Runtime]** 버튼을 클릭합니다.
2. 성공하면 자동으로 해당 Agent의 **Design 페이지**(`/agents/{id}/design`)로 이동합니다.
3. 배포가 즉시 되지 않으면(권한/타이밍 등) `"Agent가 저장되었습니다. Runtime 배포는 Agent Registry에서 진행해주세요."` 알림이 뜨고 **Agent Registry**로 이동합니다 — 이 경우 아래 B 방법으로 배포를 마무리하면 됩니다.

**B. Registry → Detail/Design 페이지에서 배포**

1. 사이드바에서 **Agent Registry**(`/agents`)를 엽니다. 방금 저장한 Agent가 카드로 보입니다 (상태 배지: **Not Deployed**).
2. 카드의 **Detail** 또는 **Design** 버튼을 클릭합니다.
3. **Design 페이지**에서 워크플로우 다이어그램과 config JSON을 확인한 뒤 우측 하단의 **[Deploy Agent]** 버튼을 클릭합니다.
4. 배포가 진행되는 동안 *"Runtime provisioning in progress... Quality Gate passed. Creating AgentCore Runtime..."* 안내가 표시됩니다. 완료되면 자동으로 **Detail 페이지**(`/agents/{id}`)로 이동합니다.

### 4. 배포 대기 (중요)

- Deploy는 새로운 **AgentCore Runtime을 프로비저닝**합니다. Runtime이 **READY** 상태에 도달하기까지 보통 **약 1~2분**이 걸립니다 (백엔드는 최대 120초까지 READY를 기다립니다). 그래서 **[Deploy Agent]** 버튼이 한동안 "Deploying..." 상태로 머무를 수 있습니다 — 정상입니다.
- **Agent Registry**의 상태 배지가 **Deploying**(노란색) → **Running**(초록색)으로 바뀝니다.
- **Runtime이 READY(Running)가 되기 전에는 Playground를 사용하지 마세요.** 준비 전 호출하면 `"Agent is not deployed or its runtime is not ready"` 오류가 발생합니다.

> **상태 배지 의미** (Registry / Detail 공통):
> | 내부 상태 | 배지 표기 | 의미 |
> |-----------|-----------|------|
> | `READY` | **Running** (초록) | 사용 가능 |
> | `CREATING` | **Deploying** (노랑) | 배포 진행 중 — 대기 |
> | `CREATE_FAILED` / `UPDATE_FAILED` | **Unhealthy** (빨강) | 배포 실패 — 재배포 필요 |
> | `NOT_DEPLOYED` | **Not Deployed** (회색) | 아직 배포 안 됨 |

### 5. 검증 & Playground 테스트

1. **Agent Registry**에서 대상 Agent의 배지가 **Running**인지 확인합니다. (Detail 페이지의 **Runtime** 섹션에서 Status/ARN도 확인 가능)
2. 카드 또는 Detail 페이지의 **[Playground]** 버튼을 클릭해 `/agents/{id}/playground`로 이동합니다.
3. 하단 입력창에 테스트 메시지를 입력해 전송합니다.
4. 응답 토큰이 **스트리밍**되며, 오른쪽 패널에서 진행 단계(Routing → Gathering → Analyzing → Reporting)와 **SSE 이벤트**가 실시간으로 표시됩니다.
5. Agent에 **Gateway(도구)**가 연결되어 있으면 도구 호출(`tool_call` / `action` 등) 이벤트도 함께 나타납니다. (실제 도구 호출을 보려면 배포 시 Phase 7~8까지 완료되어 있어야 합니다.)
6. Agent가 사람 승인을 요구하도록 설계된 경우, **Human Approval Required** 모달이 뜨며 **Approve / Reject**로 응답할 수 있습니다.

### 6. 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| Builder에서 Deploy를 눌렀는데 `"Agent가 저장되었습니다. Runtime 배포는 Agent Registry에서 진행해주세요."` 알림이 뜸 | Save와 Deploy는 2단계이며, Builder에서의 Deploy가 권한/타이밍 문제로 즉시 안 될 수 있음 | Agent는 이미 저장됨. **Agent Registry → 해당 Agent → Design(또는 Detail)** 로 가서 **[Deploy Agent]** 로 배포를 마무리하세요. |
| Playground에서 `"Agent is not deployed or its runtime is not ready"` | Runtime이 아직 READY가 아님 | Registry 배지가 **Running**이 될 때까지(보통 1~2분) 기다린 뒤 다시 시도하세요. Design 페이지에서 아직 배포하지 않았다면 먼저 **Deploy Agent**를 수행합니다. |
| Deploy 재시도 시 `"An agent with the specified name already exists (ConflictException)"` | (구버전 동작) 이전 배포가 Runtime을 만들었지만 DB 레코드가 유실되어 재생성 시 이름 충돌 | **현재 빌드에서 수정됨** — Deploy가 같은 이름의 기존 Runtime을 **자동으로 인수(adopt)**합니다. 구버전에서 이 오류를 봤다면 현재 빌드로 다시 배포하세요. |
| Registry는 **Running**인데 Playground는 "not ready"라고 함 | Runtime 레코드가 실제 Runtime과 어긋난(stale) 상태 | **다시 Deploy** 하세요. Deploy 시 AgentCore의 실제 Runtime을 조회해 레코드를 **자동으로 치유(heal)**합니다. |
| Deploy가 실패하며 `AGENTCORE_ROLE_ARN` / `BASE_IMAGE_URI` 관련 오류 | 플랫폼 배포가 불완전 (환경변수 미주입 또는 이미지 미빌드) | `deploy-all.sh`의 **Phase 2(이미지 빌드)**와 **Phase 4(ECS 재배포)**가 완료되었는지 확인하세요. ECR에 `base-image`가 없으면 AgentCore가 이미지를 pull할 수 없습니다. |
| Deploy가 `Runtime provisioning timed out (120s)` 로 실패 | Runtime이 120초 내 READY 도달 실패 | 잠시 후 **다시 Deploy** 하세요. 이미 뒤늦게 READY가 됐다면 Deploy가 기존 Runtime을 인수합니다. |
| Builder 대화나 Playground가 모델 호출에서 실패 | Bedrock 모델 액세스 미승인 | `AWS_REGION`에서 **Claude Sonnet 4.6**과 **Claude Opus 4.6** 모델 액세스를 활성화하세요. |

---

## English

### Prerequisites

- The platform must be deployed (`./scripts/deploy-all.sh` completed). In particular **Phase 2 (image build)** must finish so `base-image` exists in ECR — otherwise "Deploy" fails.
- A **Cognito account** to log in.
- **Bedrock model access** enabled in `AWS_REGION`:
  - **Claude Sonnet** (`global.anthropic.claude-sonnet-4-6`) for agent execution
  - **Claude Opus** (`global.anthropic.claude-opus-4-6-v1`) used by the Agent Builder
  - Without model access, deploy can succeed but the Builder chat and Playground calls **fail at invocation time**.
- **Platform URL** — if you did not set a custom domain (`DOMAIN_NAME`), this is the **CloudFront default domain**. Find it in the `deploy-all.sh` output or the CloudFront console.

### 1. Log in (Cognito)

1. Open the platform URL (CloudFront default domain) in your browser.
2. On the login screen (**"AIOps Platform / Sign in to continue"**), enter your Cognito **Email** and **Password**, then click **Sign in**.
3. On success the access token is stored in your browser and you land on the dashboard. This token is attached to every subsequent API call.
4. Use the left sidebar to navigate to **Agent Builder**, **Agent Registry**, **Trace Viewer**, etc.

### 2. Agent Builder — assemble an agent in natural language

1. Open **Agent Builder** (`/builder`) from the sidebar.
2. In the input box ("*Agent에 대한 요구사항을 설명하세요...*"), **describe the agent you want in natural language**. Example prompts are provided (in Korean):
   - "Build an agent that monitors EKS cluster Pod status"
   - "An agent that auto-creates incidents from CloudWatch alarms"
   - "Design an agent that generates a weekly security audit report"
3. The assistant **streams** its reply and the step bar at the bottom advances `Start › Intent › Boundary › Tools › Delegation › Config › Complete`. Answer any follow-up questions to continue the conversation.
4. Once a config is generated, a **"Generated Agent Config"** panel appears on the right, showing:
   - **Validation warnings** (e.g. "systemPrompt is empty or too short", "no Gateway configured") — refine via chat if present.
   - A **Model** dropdown — choose `Claude Sonnet 4.6` / `Claude Opus 4.6` / `Claude Haiku 4.5` (default Sonnet 4.6).
   - A preview of the generated **config JSON**.
5. Pick a model, then click **[Save Agent]**. When saved, the button becomes a **"Saved (agentId)"** badge and a **[Deploy Runtime]** button appears next to it.

> **Important — Save and Deploy are two separate steps.** `Save Agent` only registers the config in the Registry; it does not create a Runtime. To actually use the agent you must perform the Deploy step below.

### 3. Deploy — provision the AgentCore Runtime

There are **two entry points** to Deploy. Both call the same deploy API.

**A. Deploy directly from the Builder (right after Save)**

1. Click the **[Deploy Runtime]** button that appeared after `Save Agent`.
2. On success you are redirected to the agent's **Design page** (`/agents/{id}/design`).
3. If deploy can't complete immediately (permissions/timing), an alert says `"Agent가 저장되었습니다. Runtime 배포는 Agent Registry에서 진행해주세요."` ("Agent saved. Please complete Runtime deploy from the Agent Registry.") and you are sent to the **Agent Registry** — in that case finish the deploy via method B below.

**B. Deploy from the Registry → Detail/Design page**

1. Open **Agent Registry** (`/agents`) from the sidebar. Your just-saved agent appears as a card (status badge: **Not Deployed**).
2. Click **Detail** or **Design** on the card.
3. On the **Design page**, review the workflow diagram and config JSON, then click **[Deploy Agent]** at the bottom right.
4. While deploying, a *"Runtime provisioning in progress... Quality Gate passed. Creating AgentCore Runtime..."* banner is shown. When done you are redirected to the **Detail page** (`/agents/{id}`).

### 4. Wait for the deploy (important)

- Deploy **provisions a new AgentCore Runtime**. Reaching **READY** typically takes **~1–2 minutes** (the backend waits up to 120s for READY). The **[Deploy Agent]** button may stay in "Deploying..." for a while — this is expected.
- The **Agent Registry** status badge transitions **Deploying** (yellow) → **Running** (green).
- **Do NOT use the Playground until the runtime is READY (Running).** Calling it too early returns `"Agent is not deployed or its runtime is not ready"`.

> **Status badge meaning** (Registry / Detail):
> | Internal status | Badge | Meaning |
> |-----------------|-------|---------|
> | `READY` | **Running** (green) | Ready to use |
> | `CREATING` | **Deploying** (yellow) | Deploy in progress — wait |
> | `CREATE_FAILED` / `UPDATE_FAILED` | **Unhealthy** (red) | Deploy failed — redeploy |
> | `NOT_DEPLOYED` | **Not Deployed** (gray) | Not deployed yet |

### 5. Verify & test in the Playground

1. In **Agent Registry**, confirm the agent's badge is **Running**. (You can also check Status/ARN in the **Runtime** section of the Detail page.)
2. Click **[Playground]** on the card or Detail page to open `/agents/{id}/playground`.
3. Type a test message in the input box and send it.
4. Response tokens **stream** in, and the right panel shows progress (Routing → Gathering → Analyzing → Reporting) plus live **SSE events**.
5. If the agent has **Gateways (tools)** attached, you'll also see tool-call (`tool_call` / `action`) events. (To see real tool calls, the deploy must have completed Phases 7–8.)
6. If the agent is designed to require human approval, a **Human Approval Required** modal appears where you can **Approve / Reject**.

### 6. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Clicking Deploy in the Builder shows `"Agent가 저장되었습니다. Runtime 배포는 Agent Registry에서 진행해주세요."` | Save and Deploy are two steps; the Builder deploy may not complete immediately (permissions/timing) | The agent is already saved. Go to **Agent Registry → the agent → Design (or Detail)** and finish with **[Deploy Agent]**. |
| Playground shows `"Agent is not deployed or its runtime is not ready"` | The runtime isn't READY yet | Wait until the Registry badge is **Running** (usually 1–2 min), then retry. If you haven't deployed from the Design page yet, do **Deploy Agent** first. |
| Retry deploy errors with `"An agent with the specified name already exists (ConflictException)"` | (Old behavior) a prior deploy created the runtime but the DB record was lost, so recreation collides on name | **Fixed in the current build** — deploy now **adopts** the existing runtime of the same name. If you see this on an older build, redeploy on the current build. |
| Registry shows **Running** but Playground says "not ready" | The runtime record is stale vs. the real runtime | **Deploy again.** Deploy reconciles against the real AgentCore runtime and **heals** the record. |
| Deploy fails with an `AGENTCORE_ROLE_ARN` / `BASE_IMAGE_URI` error | The platform deploy is incomplete (env var not injected or image not built) | Confirm `deploy-all.sh` **Phase 2 (image build)** and **Phase 4 (ECS redeploy)** completed. If `base-image` is missing in ECR, AgentCore can't pull the image. |
| Deploy fails with `Runtime provisioning timed out (120s)` | Runtime didn't reach READY within 120s | **Deploy again** after a moment. If it became READY late, deploy adopts the existing runtime. |
| Builder chat or Playground fails on the model call | Bedrock model access not granted | Enable **Claude Sonnet 4.6** and **Claude Opus 4.6** model access in `AWS_REGION`. |
